# translation_service.py
import re
import html
import httpx
import logging
from datetime import datetime
from collections import defaultdict
from typing import Optional, Callable

from config import OPENAI_API_KEY, GOOGLE_API_KEY
from constants import GOOGLE_TRANSLATE_DAILY_LIMIT
from lang_config import LANG_NAMES
from tts_lang_resolver import clean_translation, safe_detect
from app_redis import check_and_increment_gtranslate_quota, get_gtrans_used_today
from tone_translate import smart_translate  # ต้องมีไฟล์ tone_translate.py ตามที่คุยกัน

logger = logging.getLogger(__name__)

# server-level translation provider
# values: "gpt4omini" | "gpt5nano" | "google"
translator_server_engine = defaultdict(lambda: "gpt4omini")

# Google lang normalize
GOOGLE_LANG_MAP = {"zh": "zh-CN", "zh-CN": "zh-CN", "jp": "ja"}
def gcode(lang: str) -> str:
    return GOOGLE_LANG_MAP.get(lang or "en", lang or "en")

def get_translator_engine(guild_id: int) -> str:
    return translator_server_engine.get(guild_id) or "gpt4omini"

def engine_label_for_message(message) -> str:
    gid = getattr(getattr(message, "guild", None), "id", 0)
    provider = (get_translator_engine(gid) or "").lower()
    mapping = {
        "gpt4omini": "GPT-4o mini",
        "gpt5nano": "GPT-5 nano",
        "google": "Google Translate",
        "gpt": "GPT-4o mini",
    }
    return mapping.get(provider, provider or "unknown")

# ---------------- Google Translate ----------------
def chunk_text(text: str, max_len: int = 4500) -> list[str]:
    lines, acc, buf = text.splitlines(), [], ""
    for line in lines:
        cand = (buf + ("\n" if buf else "") + line) if buf else line
        if len(cand) <= max_len:
            buf = cand
        else:
            if buf:
                acc.append(buf)
            while len(line) > max_len:
                acc.append(line[:max_len]); line = line[max_len:]
            buf = line
    if buf:
        acc.append(buf)
    if not acc:
        for i in range(0, len(text), max_len):
            acc.append(text[i:i + max_len])
    return acc

async def translate_via_google(text: str, target_code: str, source_code: str | None = None) -> str:
    api_key = GOOGLE_API_KEY
    if not api_key:
        logger.error("❌ ไม่มี GOOGLE_API_KEY")
        return "⚠️ ไม่มี GOOGLE_API_KEY"
    text = (text or "").strip()
    if not text:
        return ""

    url = f"https://translation.googleapis.com/language/translate/v2?key={api_key}"
    tcode = gcode(target_code)
    scode = gcode(source_code) if source_code else None
    if scode and scode.split("-")[0].lower() == tcode.split("-")[0].lower():
        scode = None

    chunks = chunk_text(text, 4500)
    outs: list[str] = []
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for part in chunks:
                payload = {"q": part, "target": tcode, "format": "text"}
                if scode:
                    payload["source"] = scode
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and "error" in data:
                    code = data["error"].get("code")
                    msg = data["error"].get("message", "Unknown error")
                    logger.error(f"Google Translate error {code}: {msg}")
                    return "⚠️ Google Translate ใช้งานไม่ได้ชั่วคราว"
                translations = data.get("data", {}).get("translations", [])
                if not translations:
                    continue
                raw = translations[0].get("translatedText", "")
                outs.append(html.unescape(raw))
        return "\n".join(outs).strip()
    except httpx.TimeoutException:
        logger.error("⏰ Google Translate timeout")
        return "⏳ การแปลใช้เวลานานเกินไป กรุณาลองใหม่"
    except httpx.HTTPStatusError as e:
        logger.error(f"❌ Google Translate HTTP {e.response.status_code}: {e.response.text[:200]}")
        return "⚠️ Google Translate ใช้งานไม่ได้ชั่วคราว"
    except httpx.RequestError as e:
        logger.exception(f"❌ Google Translate request error: {e}")
        return "⚠️ ไม่สามารถเชื่อมต่อบริการแปลได้"
    except Exception as e:
        logger.exception(f"❌ Google Translate error: {type(e).__name__}: {e}")
        return "⚠️ เกิดข้อผิดพลาดกับ Google Translate"

# ---------------- OpenAI Responses API ----------------
async def get_translation(prompt: str, model: str) -> str:
    api_key = OPENAI_API_KEY
    if not api_key:
        logger.error("❌ ไม่มี OPENAI_API_KEY")
        return "⚠️ ไม่มี OPENAI_API_KEY"

    prompt = (prompt or "").strip()
    if not prompt:
        return ""

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": prompt, "max_output_tokens": 1500}
    timeout = httpx.Timeout(connect=10.0, read=50.0, write=10.0, pool=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            ot = data.get("output_text")
            if isinstance(ot, str) and ot.strip():
                return ot.strip()
        for item in (data.get("output") or []):
            if item.get("type") == "message" and "content" in item:
                for c in item["content"]:
                    if c.get("type") == "output_text":
                        txt = (c.get("text") or "").strip()
                        if txt:
                            return txt
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            msg = (choices[0].get("message") or {})
            txt = (msg.get("content") or "").strip()
            if txt:
                return txt
        logger.error(f"⚠️ Unexpected OpenAI response format: {data}")
        return "⚠️ ไม่สามารถอ่านผลลัพธ์จาก GPT ได้"
    except httpx.TimeoutException:
        logger.error("⏰ Timeout: OpenAI API ไม่ตอบกลับตามเวลา")
        return "⏰ เกิด Timeout ระหว่างรอคำตอบจาก GPT"
    except httpx.RequestError as e:
        logger.exception(f"❌ Request error while calling OpenAI: {e}")
        return "❌ ไม่สามารถเชื่อมต่อบริการ GPT ได้"
    except Exception as e:
        logger.exception(f"⚠️ Unexpected error while calling OpenAI API: {type(e).__name__}: {e}")
        return "⚠️ เกิดข้อผิดพลาดที่ไม่คาดคิด"

# ---- Wrapper สำหรับ smart_translate ให้เรียก LLM ของเรา ----
async def llm_translate_wrapper(
    text: str,
    src: str,
    tgt: str,
    engine: str,
    *,
    system_prompt: str | None = None,
) -> str:
    """
    smart_translate จะส่ง annotated text + system_prompt มาให้
    รวมเป็นข้อความเดียว แล้วยิง /v1/responses
    คืนผลลัพธ์เป็น "ข้อความล้วน" (ไม่ต้อง <T>...</T>)
    """
    sp = (system_prompt or "").strip()
    full = (
        (sp + "\n\n") if sp else ""
    ) + (
        "Translate the following text according to the rules above.\n"
        "Return ONLY the translated sentence (no quotes, no language labels, no extra text).\n\n"
        f"Text:\n{text}"
    )
    return await get_translation(full, engine)

# ---------------- Provider selection wrapper ----------------
async def translate_with_provider(
    message, src_text: str, target_code: str, target_lang_name: str, source_code: str | None = None,
) -> str:
    def _extract_tagged(text: str) -> str:
        m = re.search(r"<T>(.*?)</T>", text or "", flags=re.DOTALL)
        return (m.group(1) if m else (text or "")).strip()

    def _is_lang(s: str, tgt: str) -> bool:
        if not s or len(s.strip()) < 2:
            return False
        t = (tgt or "").lower()
        try:
            d = safe_detect(s)
        except Exception:
            return True
        if t.startswith("zh"):
            return d.startswith("zh")
        return d.split("-")[0] == t.split("-")[0]

    def _final_clean(src: str, out_text: str, tgt_code: str) -> str:
        t = _extract_tagged(out_text)
        t = clean_translation(src, t).strip()
        if not t:
            return ""
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        drop_re = re.compile(r"^(?:translation|result|thai|target|source|คำแปล|แปลว่า)\b[:：-]?\s*", re.I)
        filtered = [ln for ln in lines if not ("->" in ln or "—" in ln or drop_re.match(ln))]
        if not filtered:
            filtered = lines
        lang_ok = [ln for ln in filtered if _is_lang(ln, tgt_code)]
        if lang_ok:
            filtered = lang_ok
        return "\n".join(filtered).strip()

    def _final_clean_plain(src: str, out_text: str, tgt_code: str) -> str:
        """
        ใช้เมื่อผลลัพธ์ไม่ถูกห่อ <T>...</T> (กรณี smart_translate)
        """
        t = clean_translation(src, (out_text or "")).strip()
        if not t:
            return ""
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        drop_re = re.compile(r"^(?:translation|result|thai|target|source|คำแปล|แปลว่า)\b[:：-]?\s*", re.I)
        filtered = [ln for ln in lines if not ("->" in ln or "—" in ln or drop_re.match(ln))]
        if not filtered:
            filtered = lines
        lang_ok = [ln for ln in filtered if _is_lang(ln, tgt_code)]
        if lang_ok:
            filtered = lang_ok
        return "\n".join(filtered).strip()

    guild_id = getattr(getattr(message, "guild", None), "id", 0)
    provider = (get_translator_engine(guild_id) or "").lower()

    # Google path (with global quota)
    async def _google_translate_and_clean() -> str:
        n_chars = len(src_text or "")
        today = datetime.now().strftime("%Y-%m-%d")
        ok, reason = await check_and_increment_gtranslate_quota(
            n_chars=n_chars,
            date_str=today,
            daily_limit=GOOGLE_TRANSLATE_DAILY_LIMIT,
            user_id=getattr(getattr(message, "author", None), "id", 0),
        )
        if not ok:
            if reason == "exceeded":
                return f"❌ เกินโควตา Google Translate {GOOGLE_TRANSLATE_DAILY_LIMIT} ตัวอักษร/วันแล้ว (ทั้งบอท)"
            if reason == "redis":
                return "⚠️ ตรวจสอบโควต้า Google Translate ไม่ได้ (Redis)"
            return "⚠️ ไม่สามารถตรวจสอบโควต้า Google Translate ได้"
        g = await translate_via_google(src_text, target_code, source_code)
        return _final_clean(src_text, g, target_code)

    if provider == "google":
        return (await _google_translate_and_clean()) or "⚠️ แปลไม่สำเร็จ"

    # GPT providers
    model = "gpt-5-nano" if provider == "gpt5nano" else "gpt-4o-mini"
    tgt_name = target_lang_name
    src_key = (source_code or "").split("-")[0] if source_code else None
    src_name = LANG_NAMES.get(src_key, "auto-detected") if source_code else "auto-detected"

    # 1) โหมดฉลาด: คงโทน/สแลง + ทำให้ธรรมชาติ
    try:
        smart_out = await smart_translate(
            src_text,
            src=(source_code or "auto"),
            tgt=target_code,
            engine=model,
            style="preserve",           # หรือ "neutralize" ถ้าต้องการลดคำหยาบ
            natural_pass=True,
            llm_translate_callable=llm_translate_wrapper,
        )
        smart_out = _final_clean_plain(src_text, smart_out, target_code)
    except Exception:
        logger.exception("smart_translate failed; fallback to legacy prompt")
        smart_out = ""

    if smart_out:
        return smart_out

    # 2) Fallback legacy prompt (<T>...</T>)
    direction = f"from {src_name} to {tgt_name}" if source_code else f"into {tgt_name}"
    prompt = (
        f"Translate the following text {direction}.\n"
        "- Make it natural and idiomatic (spoken style if casual, formal if formal).\n"
        "- Output ONLY the translation wrapped in <T>...</T>.\n\n"
        f"Text:\n{src_text}"
    )
    raw = await get_translation(prompt, model)
    out = _final_clean(src_text, raw, target_code)

    # 3) Retry once (simple)
    if not out:
        retry_prompt = (
            f"Translate into {tgt_name}.\n"
            "Output ONLY the translation wrapped in <T>...</T>.\n\n"
            f"Text:\n{src_text}"
        )
        raw2 = await get_translation(retry_prompt, model)
        out = _final_clean(src_text, raw2, target_code)

    # 4) Last resort: Google
    if not out:
        return await _google_translate_and_clean()

    return out or "⚠️ แปลไม่สำเร็จ"

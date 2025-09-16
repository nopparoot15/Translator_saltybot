# translation_service.py
import re
import html
import httpx
import logging
from datetime import datetime
from collections import defaultdict
from typing import Optional

from config import OPENAI_API_KEY, GOOGLE_API_KEY
from constants import GOOGLE_TRANSLATE_DAILY_LIMIT
from lang_config import LANG_NAMES
from tts_lang_resolver import clean_translation, safe_detect
from app_redis import check_and_increment_gtranslate_quota, get_gtrans_used_today

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

# ---------------- Helpers ----------------

def _coverage_ratio(src: str, out: str) -> float:
    s = re.sub(r"\W+", "", src or "")
    o = re.sub(r"\W+", "", out or "")
    if len(s) < 20:
        return 1.0
    return (len(o) / max(1, len(s)))

def _line_mismatch(src: str, out: str) -> bool:
    s_lines = [l for l in (src or "").splitlines() if l.strip()]
    o_lines = [l for l in (out or "").splitlines() if l.strip()]
    return len(o_lines) + 1 < len(s_lines)

_SPEAKER_RE = re.compile(r"^\s*([A-Za-z0-9_@.\-]{1,24}):\s*(.*)$")

# ---- Google Translate ----
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
    if buf: acc.append(buf)
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

# ---- OpenAI Responses API ----
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

# ---- Provider selection wrapper ----
async def translate_with_provider(
    message, src_text: str, target_code: str, target_lang_name: str, source_code: str | None = None,
) -> str:
    def _extract_tagged(text: str) -> str:
        m = re.search(r"<T>(.*?)</T>", text or "", flags=re.DOTALL)
        return (m.group(1) if m else (text or "")).strip()

    def _is_lang(s: str, tgt: str) -> bool:
        if not s or len(s.strip()) < 2: return False
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

    async def _call_model(text: str, model: str, tgt_name: str) -> str:
        # พรอมป์สั้นเพื่อความเร็วและประหยัด
        prompt = (
            f"Translate into {tgt_name}.\n"
            "Output ONLY the translation wrapped in <T>...</T>.\n\n"
            f"Text:\n{text}"
        )
        raw = await get_translation(prompt, model)
        return raw

    async def _translate_line_by_line(text: str, model: str, tgt_name: str, tgt_code: str) -> str:
        outs = []
        for ln in (text or "").splitlines():
            if not ln.strip():
                outs.append("")
                continue
            # รองรับ speaker tag "xxx: ..."
            tag = ""
            m = _SPEAKER_RE.match(ln)
            body = ln
            if m:
                tag = m.group(1) + ": "
                body = m.group(2)

            raw = await _call_model(body, model, tgt_name)
            out = _final_clean(body, raw, tgt_code)
            if tag:
                out = tag + out
            outs.append(out)
        return "\n".join(outs).strip()

    guild_id = getattr(getattr(message, "guild", None), "id", 0)
    provider = get_translator_engine(guild_id)

    # Google path (with global quota)
    async def _google_translate_and_clean() -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        ok, reason = await check_and_increment_gtranslate_quota(
            n_chars=len(src_text or ""),
            date_str=today,
            daily_limit=GOOGLE_TRANSLATE_DAILY_LIMIT,
            user_id=message.author.id,
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

    model = "gpt-5-nano" if provider == "gpt5nano" else "gpt-4o-mini"
    tgt_name = target_lang_name

    # รอบแรก: แปลทั้งก้อน
    raw = await _call_model(src_text, model, tgt_name)
    out = _final_clean(src_text, raw, target_code)

    # ถ้าขาด/สั้น/บรรทัดไม่ครบ → รีทรายแบบบรรทัดต่อบรรทัด
    if (not out) or _coverage_ratio(src_text, out) < 0.5 or _line_mismatch(src_text, out):
        out2 = await _translate_line_by_line(src_text, model, tgt_name, target_code)
        if out2:
            out = out2

    if not out:
        return await _google_translate_and_clean()
    return out or "⚠️ แปลไม่สำเร็จ"

from __future__ import annotations
import re
from typing import List, Tuple, Optional
from lang_config import LANG_NAMES

# ---------- Emoji patterns ----------
_CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_~]+:[0-9]+>")  # <:name:id> / <a:name:id>
# Unicode emoji blocks (ครอบคลุมทั่วไป: Symbols & Pictographs, Dingbats, Misc Symbols, Flags)
_UNICODE_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF\U0001F1E6-\U0001F1FF]+",
    flags=re.UNICODE,
)

def strip_emojis_for_tts(s: str) -> str:
    """ตัดทั้ง custom และ unicode emoji ออกจากข้อความสำหรับ TTS"""
    if not s:
        return ""
    s = _CUSTOM_EMOJI_RE.sub("", s)
    s = _UNICODE_EMOJI_RE.sub("", s)
    return s

def is_emoji_only(s: str) -> bool:
    """ตรวจว่าข้อความมีแต่อีโมจิ/ช่องว่าง/Zero-width เท่านั้น"""
    if not s:
        return False
    t = s.strip()
    if not t:
        return False
    no_emoji = strip_emojis_for_tts(t)
    no_emoji = re.sub(r"[\u200B-\u200D\uFEFF\s]+", "", no_emoji)
    return len(no_emoji) == 0


# ---------- Language helpers ----------
_LANG_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,3})?$")

def sanitize_requested_lang(req: str | None) -> str:
    """
    ให้แน่ใจว่าเป็นโค้ดภาษารูปแบบสั้น (xx หรือ xx-YY) เท่านั้น
    ถ้าไม่ใช่/ว่าง → คืน 'auto'
    """
    if not isinstance(req, str):
        return "auto"
    req = req.strip()
    if not req or not _LANG_PATTERN.fullmatch(req):
        return "auto"
    return req

# gTTS languages (บางอันต้อง map)
_GTTs_NORMALIZE = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_CN": "zh-CN",
    "zh-TW": "zh-TW",
    "jp": "ja",
    "pt-br": "pt",
    "pt_BR": "pt",
}

def normalize_gtts_lang(code: str) -> tuple[str, str]:
    """
    ทำให้โค้ดเข้ากับ gTTS ได้
    คืน (gtts_key, display_code)
    - display_code ใช้สำหรับ log ให้สวย
    """
    if not code:
        return "en", "en"
    key = code.strip()
    key_lower = key.lower()
    key = _GTTs_NORMALIZE.get(key_lower, key_lower)
    # บางกรณี gTTS ไม่มีตัวเลือกประเทศ → ลดรูปเหลือ 2 ตัวอักษร
    if "-" in key and key not in ("zh-CN", "zh-TW"):
        key = key.split("-")[0]
    return key, key


# ---------- Per-part shaping ----------
def normalize_parts_shape(parts: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    fixed: List[Tuple[str, str]] = []
    for a, b in parts:
        t, lg = a, b

        # เดิม: สลับถ้า b ไม่ใช่โค้ดภาษา แต่ a เป็นโค้ดภาษา
        # ปรับ: "ถ้า b เป็น 'auto' ห้ามสลับ" กันเคส text สั้น ๆ เช่น "no", "it", "es" ฯลฯ
        if (
            (not _LANG_PATTERN.fullmatch(b or "")) and
            _LANG_PATTERN.fullmatch((a or "").strip()) and
            (b or "").strip().lower() != "auto"         # << เพิ่ม guard ตรงนี้
        ):
            t, lg = b, a

        t = strip_emojis_for_tts(t or "").strip()
        lg = sanitize_requested_lang(lg or "auto")
        if t:
            fixed.append((t, lg))
    return fixed


# ---------- Language detection heuristics ----------
def _detect_script_fast_char(ch: str) -> str:
    """
    เดาสคริปต์ต่อ 1 ตัวอักษร: th/ja/ko/ru/en/number/other
    """
    cp = ord(ch)
    # Thai
    if 0x0E00 <= cp <= 0x0E7F:
        return "th"
    # Hiragana/Katakana/CJK
    if (0x3040 <= cp <= 0x30FF) or (0x4E00 <= cp <= 0x9FFF):
        return "ja"
    # Hangul
    if 0xAC00 <= cp <= 0xD7AF:
        return "ko"
    # Cyrillic
    if 0x0400 <= cp <= 0x04FF:
        return "ru"
    # Latin letters
    if (0x0041 <= cp <= 0x005A) or (0x0061 <= cp <= 0x007A):
        return "en"
    # Digits
    if 0x0030 <= cp <= 0x0039:
        return "number"
    return "other"

def _detect_script_fast(s: str) -> str:
    """เดาสคริปต์คร่าว ๆ ของสตริง: th/ja/ko/ru/en (fallback en)"""
    for ch in s:
        cat = _detect_script_fast_char(ch)
        if cat in {"th", "ja", "ko", "ru", "en"}:
            return cat
    return "en"

def resolve_tts_code(text: str, hint: str = "auto") -> str:
    """
    เดาภาษา TTS จากข้อความ (หลังตัด emoji แล้ว) + hint ('auto' หรือโค้ดภาษา)
    - ถ้า hint เป็นภาษาถูกต้อง → ใช้เลย
    - ถ้า 'auto' → เดาตามสคริปต์
    """
    h = sanitize_requested_lang(hint)
    if h != "auto":
        return h
    clean = strip_emojis_for_tts(text or "").strip()
    if not clean:
        return "en"
    script = _detect_script_fast(clean)
    if script == "ja":
        return "ja"
    if script == "th":
        return "th"
    if script == "ko":
        return "ko"
    if script == "ru":
        return "ru"
    return "en"

def _guess_latin_language_by_words(t: str) -> str | None:
    """
    heuristic ง่าย ๆ สำหรับตัวอักษรละติน:
    - ถ้าพบคำบางคำ → เดา de/fr/es/it/pt
    """
    s = t.lower()
    # เยอรมัน
    if re.search(r"\b(und|nicht|danke|nein|ja|ich|über|straße)\b", s):
        return "de"
    # ฝรั่งเศส
    if re.search(r"\b(et|merci|non|oui|je|vous|avec|être)\b", s):
        return "fr"
    # สเปน/โปรตุเกส/อิตาลี (อย่างคร่าว ๆ)
    if re.search(r"\b(gracias|hola|buenos|no|sí|por|favor)\b", s):
        return "es"
    if re.search(r"\b(obrigado|olá|não|sim|por|favor)\b", s):
        return "pt"
    if re.search(r"\b(grazie|ciao|non|si|per|favore)\b", s):
        return "it"
    return None

def resolve_parts_for_tts(
    parts: List[Tuple[str, str]],
    preferred_lang: Optional[str] = None,   # 🆕 เพิ่ม param
) -> List[Tuple[str, str]]:
    """
    เดาภาษารายท่อน + normalize โค้ดให้ gTTS ใช้ได้
    ถ้ามี preferred_lang ให้เชื่อก่อน (ชนะ heuristic)
    """
    # 🆕 short-circuit: ผู้ใช้ระบุภาษามา → ใช้ตามนั้นทุกท่อน
    if preferred_lang and preferred_lang.lower() != "auto":
        gtts_key, display = normalize_gtts_lang(preferred_lang)
        return [(text, display) for text, _ in normalize_parts_shape(parts)]

    out: List[Tuple[str, str]] = []
    for text, lg in normalize_parts_shape(parts):
        code = resolve_tts_code(text, lg)

        # fine tune ละตินหน่อย
        if code == "en":
            maybe = _guess_latin_language_by_words(text)
            if maybe:
                code = maybe

        # เดิม: CJK ไม่มีฮิระ/คะตะ → บังคับ zh-CN
        if code in ("ja", "en"):
            has_hira_kata = bool(re.search(r"[\u3040-\u30FF]", text))
            has_cjk = bool(re.search(r"[\u4E00-\u9FFF]", text))
            if has_cjk and not has_hira_kata:
                code = "zh-CN"

        gtts_key, display = normalize_gtts_lang(code)
        out.append((text, display))
    return out


# ---------- Text segmentation & merging (moved from bot.py) ----------
def split_text_by_script(text: str) -> List[Tuple[str, str]]:
    """
    แยกข้อความยาวเป็นชิ้น ๆ ตามชนิดสคริปต์ (ไทย/ญี่ปุ่น/ฯลฯ) เพื่อช่วยเลือกเสียงใน TTS
    - ตัวเลขที่ขึ้นต้นบล็อกใหม่จะถือเป็น 'th' เพื่ออ่านตัวเลขกับบริบทไทยได้ดีขึ้น
    """
    parts: List[Tuple[str, str]] = []
    current, current_lang = "", None

    for ch in text or "":
        ch_lang = _detect_script_fast_char(ch)

        if ch_lang == "number":
            if current_lang:
                # ถ้ามีบล็อกภาษาอยู่แล้ว ให้ตัวเลขตามบล็อกเดิม
                current += ch
            else:
                # ถ้ายังไม่มีภาษา ตีความเป็นไทยก่อน (อ่านเลขไทยเป็นธรรมชาติ)
                if current:
                    parts.append((current, current_lang or "th"))
                current, current_lang = ch, "th"
            continue

        if ch_lang == current_lang:
            current += ch
        else:
            if current:
                parts.append((current, current_lang or "th"))
            current, current_lang = ch, ch_lang

    if current:
        parts.append((current, current_lang or "th"))
    return parts

def merge_adjacent_parts(parts: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """
    รวมชิ้นที่ติดกันและเป็นภาษาชนิดเดียวกันเข้าด้วยกัน
    - กรณีพิเศษ: ญี่ปุ่น + ตัวอักษรสั้น ๆ อังกฤษ ให้รวมเข้า ja (เช่น 〜ですyo, かわE)
    """
    merged: List[Tuple[str, str]] = []
    for text, lang in parts:
        if merged:
            last_text, last_lang = merged[-1]
            if lang == last_lang:
                merged[-1] = (last_text + text, lang)
                continue
            if last_lang == "ja" and lang == "en" and re.fullmatch(r"[A-Za-z0-9]{1,3}", text):
                merged[-1] = (last_text + text, "ja")
                continue
        merged.append((text, lang))
    return merged


# ---------- Cleaning translated text (moved from bot.py) ----------
def clean_translation(src_text: str, translated: str) -> str:
    """
    ทำความสะอาดผลลัพธ์การแปลให้เหลือเฉพาะข้อความแปลล้วน ๆ
    - ลบป้ายกำกับ/echo/quote/วงเล็บ/โค้ดบล็อก ที่พบบ่อย
    """
    t = (translated or "").strip()
    # ตัดหัวป้ายบ่อย ๆ
    t = re.sub(r'^(แปลว่า|คำแปลคือ|หมายถึง|ความหมาย)\s*[:：-]?\s*', '', t, flags=re.I)
    t = re.sub(r'^(Thai|TH|English|EN|Japanese|JA|Chinese|ZH|Korean|KO|Russian|RU|Vietnamese|VI)\s*[:：-]\s*', '', t, flags=re.I)
    # ตัดการ echo ต้นฉบับ
    src = (src_text or "").strip()
    if src:
        t = re.sub(rf'^{re.escape(src)}\s*[:：-]?\s*', '', t, flags=re.I)
    # ลอก wrapper
    t = re.sub(r'^[\"\'`«\(\[]\s*', '', t)
    t = re.sub(r'\s*[\"\'`»\)\]]$', '', t)
    # ป้ายวงเล็บสั้น ๆ
    t = re.sub(r'^\((?:[^()]{1,60})\)\s*', '', t).strip()
    # ตัด code fences ที่หลงมา
    t = re.sub(r"^```.*?\n", "", t, flags=re.S).strip()
    t = re.sub(r"\n```$", "", t, flags=re.S).strip()
    return t

def safe_detect(text: str) -> str:
    """
    ตรวจภาษาแบบ hybrid:
    - พยายามใช้ langdetect ก่อน
    - ถ้า detect ไม่ได้/เป็นภาษาที่ไม่รองรับ → ใช้ heuristic จากสคริปต์
    - fallback อังกฤษเมื่อข้อความสั้นมาก
    """
    txt = (text or "").strip()
    if not txt:
        return "auto"

    if len(txt) <= 3 and re.fullmatch(r"[A-Za-z]+", txt):
        return "en"

    try:
        from langdetect import detect
        d = detect(txt)
    except Exception:
        d = "auto"

    if d not in LANG_NAMES:  # ไม่รองรับในระบบเรา
        # ใช้ fast script detect
        script = _detect_script_fast(txt)
        return script if script in LANG_NAMES else "en"

    return d


# ---------- Public API ----------
__all__ = [
    # emoji / guard
    "strip_emojis_for_tts", "is_emoji_only",
    # lang codes
    "sanitize_requested_lang", "normalize_gtts_lang",
    # TTS resolving
    "normalize_parts_shape", "resolve_tts_code", "resolve_parts_for_tts",
    # segmentation / merging
    "split_text_by_script", "merge_adjacent_parts",
    # cleaning
    "clean_translation", "safe_detect",
]

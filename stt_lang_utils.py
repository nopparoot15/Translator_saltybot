from __future__ import annotations
import re
from typing import Dict, List, Tuple, Optional

__all__ = [
    # script checks
    "has_thai", "has_japanese", "has_chinese", "has_korean", "has_cyrillic",
    "has_khmer", "has_myanmar", "has_devanagari", "has_arabic",
    # latin hints
    "looks_vietnamese", "looks_indonesian", "looks_filipino",
    "looks_french", "looks_german", "looks_spanish",
    "looks_italian", "looks_portuguese", "looks_polish", "looks_ukrainian_latin",
    # core
    "detect_lang_hints_from_context", "pick_alternative_langs",
    "detect_script_from_text", "choose_alts_strict_first",
]

# ---------- Unicode script ranges ----------
TH_RANGE  = re.compile(r'[\u0E00-\u0E7F]')                              # ไทย
JA_RANGE  = re.compile(r'[\u3040-\u30FF\u31F0-\u31FF\uFF66-\uFF9F]')     # ฮิระ/คาตะ/half-kana
CJK_RANGE = re.compile(r'[\u4E00-\u9FFF]')                               # จีน (CJK Unified Ideographs)
KO_RANGE  = re.compile(r'[\uAC00-\uD7AF]')                               # เกาหลี (Hangul)
CYR_RANGE = re.compile(r'[\u0400-\u04FF]')                               # ซีริลลิก (รัสเซีย/ยูเครน ฯลฯ)
KH_RANGE  = re.compile(r'[\u1780-\u17FF\u19E0-\u19FF]')                  # เขมร + Khmer Symbols
MY_RANGE  = re.compile(r'[\u1000-\u109F]')                               # พม่า (Myanmar)
DV_RANGE  = re.compile(r'[\u0900-\u097F]')                               # เทวนาครี (ฮินดี ฯลฯ)
AR_RANGE  = re.compile(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]')     # อาหรับ

# ---------- Quick script detectors ----------
def has_thai(s: str) -> bool:       return bool(TH_RANGE.search(s or ""))
def has_japanese(s: str) -> bool:   return bool(JA_RANGE.search(s or ""))
def has_chinese(s: str) -> bool:    return bool(CJK_RANGE.search(s or ""))
def has_korean(s: str) -> bool:     return bool(KO_RANGE.search(s or ""))
def has_cyrillic(s: str) -> bool:   return bool(CYR_RANGE.search(s or ""))
def has_khmer(s: str) -> bool:      return bool(KH_RANGE.search(s or ""))
def has_myanmar(s: str) -> bool:    return bool(MY_RANGE.search(s or ""))
def has_devanagari(s: str) -> bool: return bool(DV_RANGE.search(s or ""))
def has_arabic(s: str) -> bool:     return bool(AR_RANGE.search(s or ""))

# ---------- Latin-language word hints ----------
_VI_HINTS  = {"anh", "em", "và", "của", "không", "được", "cảm", "ơn", "tôi", "bạn"}
_ID_HINTS  = {"terima", "kasih", "apa", "kabar", "tidak", "ya", "saya", "kamu", "anda", "bagus"}
_FIL_HINTS = {"salamat", "maganda", "mahal", "kita", "bakit", "saan", "paano", "ito", "iyan", "iyon", "wala", "meron", "opo", "po", "oo", "hindi", "kami", "kayo", "sila", "ikaw", "ako", "mga", "ang", "ng", "sa"}
_FR_HINTS  = {"et", "merci", "non", "oui", "avec", "être", "c'est", "pas", "une", "des", "aux", "bonjour", "au revoir"}
_DE_HINTS  = {"und", "nicht", "danke", "nein", "ja", "ich", "über", "straße", "eine", "einen", "gibt", "bitte"}
_ES_HINTS  = {"gracias", "hola", "buenos", "no", "sí", "por", "favor", "porque", "pero", "muy", "adiós"}
_IT_HINTS  = {"grazie", "ciao", "non", "sì", "per", "favore", "sono", "sei", "bene"}
_PT_HINTS  = {"obrigado", "olá", "não", "sim", "por", "favor", "você", "está", "tudo", "bom"}
_PL_HINTS  = {"dziękuję", "cześć", "nie", "tak", "proszę", "bardzo", "dobrze", "jestem", "jesteś"}
# ยูเครนละตินใช้ยาก พบไม่บ่อยในการถอดเสียง → ใช้ cyrillic เป็นหลัก
_UK_CYRL_SPECIAL = set("ҐЄІЇґєії")

def looks_vietnamese(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _VI_HINTS)

def looks_indonesian(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _ID_HINTS)

def looks_filipino(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _FIL_HINTS)

def looks_french(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _FR_HINTS)

def looks_german(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _DE_HINTS)

def looks_spanish(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _ES_HINTS)

def looks_italian(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _IT_HINTS)

def looks_portuguese(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _PT_HINTS)

def looks_polish(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in _PL_HINTS)

def looks_ukrainian_latin(s: str) -> bool:
    # ส่วนใหญ่ UA จะมาเป็น Cyrillic ใน STT — ฟังก์ชันนี้ไว้กันกรณีละตินหายาก
    return False

# ---------- Default alternative pool (BCP-47) ----------
# ครอบคลุมภาษาตาม lang_config.py ของคุณ
FALLBACK_ALTS_ORDER: List[str] = [
    # อังกฤษเป็น fallback ทั่วไป
    "en-US",
    # เอเชียตะวันออก/ตะวันออกเฉียงใต้
    "th-TH", "ja-JP", "cmn-Hans-CN", "cmn-Hant-TW", "yue-Hant-HK", "ko-KR",
    "vi-VN", "id-ID", "tl-PH", "fil-PH",
    "km-KH", "my-MM",
    # เอเชียใต้/ตะวันออกกลาง
    "hi-IN", "ar-SA",
    # ยุโรป
    "ru-RU", "uk-UA",
    "fr-FR", "de-DE", "es-ES", "it-IT", "pt-PT",
    "pl-PL",
]

def _seed_scores() -> Dict[str, float]:
    # baseline ให้ภาษาใน pool มีโอกาสติดอันดับ, en-US สูงกว่าเพื่อกันพัง
    seeds = {lang: 0.1 for lang in FALLBACK_ALTS_ORDER}
    seeds["en-US"] = 0.4
    seeds["th-TH"] = 0.0  # base มักเป็นไทยในกิลด์ไทย
    return seeds

# ---------- Context-driven bias ----------
def detect_lang_hints_from_context(
    *,
    username: str = "",
    channel_name: str = "",
    caption_text: str = "",
    base_scores: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """
    ให้คะแนน bias จากบริบท (ชื่อห้อง/ผู้ใช้/คำบรรยายรอบไฟล์เสียง/แคปชัน)
    """
    score = dict(base_scores or _seed_scores())
    blob = " ".join([username or "", channel_name or "", caption_text or ""])

    # Scripts
    if has_thai(blob):          score["th-TH"]        += 2.0
    if has_japanese(blob):      score["ja-JP"]        += 2.0
    if has_chinese(blob):
        score["cmn-Hans-CN"]   += 1.4
        score["cmn-Hant-TW"]   += 1.0
        score["yue-Hant-HK"]   += 0.6
    if has_korean(blob):        score["ko-KR"]        += 2.0
    if has_cyrillic(blob):      score["ru-RU"]        += 2.0  # จะเปลี่ยนเป็น uk-UA ถ้าพบตัว ҐЄІЇ
    if any(ch in _UK_CYRL_SPECIAL for ch in blob):  # ยูเครนเฉพาะ
        score["uk-UA"]         += 2.2
        score["ru-RU"]         *= 0.6
    if has_khmer(blob):         score["km-KH"]        += 2.0
    if has_myanmar(blob):       score["my-MM"]        += 2.0
    if has_devanagari(blob):    score["hi-IN"]        += 2.0
    if has_arabic(blob):        score["ar-SA"]        += 2.0

    # Latin hints
    if looks_vietnamese(blob):  score["vi-VN"]        += 1.6
    if looks_indonesian(blob):  score["id-ID"]        += 1.4
    if looks_filipino(blob):    score["fil-PH"]       += 1.6
    if looks_french(blob):      score["fr-FR"]        += 1.2
    if looks_german(blob):      score["de-DE"]        += 1.2
    if looks_spanish(blob):     score["es-ES"]        += 1.2
    if looks_italian(blob):     score["it-IT"]        += 1.0
    if looks_portuguese(blob):  score["pt-PT"]        += 1.0
    if looks_polish(blob):      score["pl-PL"]        += 1.0

    return score

# ---------- Build alternativeLanguageCodes ----------
def pick_alternative_langs(
    *,
    base_lang: str = "th-TH",
    default_pool: List[str] = tuple(FALLBACK_ALTS_ORDER),
    max_alts: int = 3,  # STT ส่วนใหญ่รองรับ alt ~3 ภาษา
    channel_hist: Optional[Dict[str, int]] = None,
    user_hist: Optional[Dict[str, int]] = None,
    context_bias: Optional[Dict[str, float]] = None,
    damp_jp_when_uncertain: bool = False,
    jp_min_weight: float = 2.0,
) -> List[str]:
    """
    จัดอันดับและเลือก alternativeLanguageCodes:
      - รวมถ่วงน้ำหนักจากประวัติช่อง/ผู้ใช้ + บริบท
      - ตัด base_lang ออก
      - คืน top-N; ถ้าไม่พอ เติมจาก default_pool ตามลำดับ
    """
    weights: Dict[str, float] = {lang: 0.0 for lang in default_pool}

    if channel_hist:
        for k, v in channel_hist.items():
            if k in weights: weights[k] += 0.8 * float(v)
    if user_hist:
        for k, v in user_hist.items():
            if k in weights: weights[k] += 1.4 * float(v)
    if context_bias:
        for k, v in context_bias.items():
            if k in weights: weights[k] += 1.0 * float(v)

    if damp_jp_when_uncertain and "ja-JP" in weights:
        user_jp = (user_hist or {}).get("ja-JP", 0)
        if not (user_jp >= 2 or weights["ja-JP"] >= jp_min_weight):
            weights["ja-JP"] *= 0.4

    # ไม่เอา base_lang
    weights.pop(base_lang, None)

    ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    alts = [lang for lang, w in ranked if w > 0][:max_alts]

    # เติมให้ครบ N
    if len(alts) < max_alts:
        for lang in default_pool:
            if lang != base_lang and lang not in alts:
                alts.append(lang)
            if len(alts) >= max_alts:
                break

    return alts[:max_alts]

# ---------- Detect from recognized text ----------
def detect_script_from_text(s: str) -> str:
    """
    รับข้อความที่ "ถอดเสียงแล้ว" เพื่อประมาณภาษาหลัก (BCP-47)
    หมายเหตุ: จีนจะเดา Hans เป็นค่าเริ่มต้น; Cyrillic จะลอง bias ยูเครนก่อนถ้าพบ ҐЄІЇ
    """
    if has_thai(s):          return "th-TH"
    if has_japanese(s):      return "ja-JP"
    if has_korean(s):        return "ko-KR"
    if has_chinese(s):       return "cmn-Hans-CN"
    if has_khmer(s):         return "km-KH"
    if has_myanmar(s):       return "my-MM"
    if has_devanagari(s):    return "hi-IN"
    if has_arabic(s):        return "ar-SA"
    if has_cyrillic(s):
        if any(ch in _UK_CYRL_SPECIAL for ch in s or ""):
            return "uk-UA"
        return "ru-RU"

    # Latin-family: ใช้คำบอกใบ้
    s2 = (s or "").lower()
    if looks_vietnamese(s2):  return "vi-VN"
    if looks_indonesian(s2):  return "id-ID"
    if looks_filipino(s2):    return "fil-PH"  # หรือ "tl-PH" ตาม engine
    if looks_french(s2):      return "fr-FR"
    if looks_german(s2):      return "de-DE"
    if looks_spanish(s2):     return "es-ES"
    if looks_italian(s2):     return "it-IT"
    if looks_portuguese(s2):  return "pt-PT"   # เปลี่ยนเป็น pt-BR ได้ตามผู้ฟัง
    if looks_polish(s2):      return "pl-PL"

    return "en-US"

# ---------- Two-round strategy for alt languages ----------
def choose_alts_strict_first(
    *,
    base_lang: str = "th-TH",
    alt_smart: Optional[List[str]] = None,  # ลิสต์เรียงโอกาสสูง→ต่ำ (มากกว่า 3 ก็ได้)
    force_strict_if_confident: bool = True,
    context_bias: Optional[Dict[str, float]] = None,
    strict_confidence_threshold: float = 2.0,
    exclude_in_fallback: Optional[List[str]] = None,
    per_round_limit: int = 3,  # STT v1: alt ~3
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """
    คืนค่า (alts_round1, alts_round2)
      - รอบ 1: alt_smart[:per_round_limit] หรือ None ถ้ามั่นใจ base_lang
      - รอบ 2: ชุดถัดไปจาก alt_smart โดยกรอง exclude_in_fallback
    """
    seq = list(alt_smart or [])

    # strict ถ้ามั่นใจ base_lang มากพอ
    if force_strict_if_confident and context_bias:
        if context_bias.get(base_lang, 0.0) >= strict_confidence_threshold:
            alts_round1 = None
        else:
            alts_round1 = seq[:per_round_limit] or None
    else:
        alts_round1 = seq[:per_round_limit] or None

    # รอบ 2
    rest = seq[per_round_limit:]
    if exclude_in_fallback:
        exclude = set(exclude_in_fallback)
        rest = [x for x in rest if x not in exclude]
    alts_round2 = rest[:per_round_limit] or None

    # ถ้าไม่มี rest เลย กลับไปใช้รอบแรกซ้ำ (กัน edge case)
    if alts_round2 is None and not alts_round1 and seq:
        alts_round2 = seq[:per_round_limit]

    return alts_round1, alts_round2

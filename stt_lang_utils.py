# stt_lang_utils.py
# ------------------------------------------------------------
# Utilities สำหรับ "เลือกภาษา" ฝั่ง Speech-to-Text (STT) เท่านั้น
# - ช่วยเดาภาษาสำรอง (alternativeLanguageCodes) จากบริบท/ประวัติ
# - ตรวจสคริปต์จากข้อความที่ถอดเสียงแล้ว เพื่อนำไปอัปเดตสถิติภายหลัง
# ------------------------------------------------------------

from __future__ import annotations
import re
from typing import Dict, List, Tuple, Optional

__all__ = [
    "has_thai", "has_japanese", "has_chinese", "has_korean", "has_cyrillic",
    "looks_vietnamese",
    "detect_lang_hints_from_context", "pick_alternative_langs",
    "detect_script_from_text", "choose_alts_strict_first",
]

# ---------- เรกซ์ของสคริปต์ ----------
TH_RANGE = re.compile(r'[\u0E00-\u0E7F]')  # ไทย
JA_RANGE = re.compile(r'[\u3040-\u30FF\u31F0-\u31FF\uFF66-\uFF9F]')  # ฮิระ/คาตะ/คะตะคะนะครึ่ง
ZH_RANGE = re.compile(r'[\u4E00-\u9FFF]')  # จีน (CJK Unified)
KO_RANGE = re.compile(r'[\uAC00-\uD7AF]')  # เกาหลี
CYR_RANGE = re.compile(r'[\u0400-\u04FF]') # ซีริลลิก (รัสเซีย ฯลฯ)

VI_HINTS = {"anh", "em", "và", "của", "không", "được", "cảm", "ơn"}

def has_thai(s: str) -> bool: return bool(TH_RANGE.search(s or ""))
def has_japanese(s: str) -> bool: return bool(JA_RANGE.search(s or ""))
def has_chinese(s: str) -> bool: return bool(ZH_RANGE.search(s or ""))
def has_korean(s: str) -> bool: return bool(KO_RANGE.search(s or ""))
def has_cyrillic(s: str) -> bool: return bool(CYR_RANGE.search(s or ""))

def looks_vietnamese(s: str) -> bool:
    s2 = (s or "").lower()
    return any(w in s2 for w in VI_HINTS)

# ---------- ค่าเริ่มต้น/ลิสต์สำรอง ----------
# จำกัดไว้ที่ภาษาหลัก ๆ ที่ STT v1 รองรับดี และครอบคลุม use-case ของคุณ
FALLBACK_ALTS_ORDER = [
    "en-US", "ja-JP", "cmn-Hans-CN", "cmn-Hant-TW", "yue-Hant-HK",
    "ru-RU", "ko-KR", "vi-VN",
]

def _seed_scores() -> Dict[str, float]:
    # อังกฤษมี 0.4 เป็น fallback ทั่วไป, อื่น ๆ ให้ baseline บ้างพอให้ติด rank
    return {
        "th-TH": 0.0,
        "en-US": 0.4,
        "ja-JP": 0.2,
        "cmn-Hans-CN": 0.2,
        "cmn-Hant-TW": 0.1,
        "yue-Hant-HK": 0.1,
        "ru-RU": 0.1,
        "ko-KR": 0.1,
        "vi-VN": 0.1,
    }

# ---------- วิเคราะห์บริบทเพื่อทำ bias ----------
def detect_lang_hints_from_context(
    *,
    username: str = "",
    channel_name: str = "",
    caption_text: str = "",
    base_scores: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """
    ให้คะแนน bias ตามบริบท (ชื่อห้อง/ผู้ใช้/คำบรรยายรอบไฟล์เสียง)
    """
    score = dict(base_scores or _seed_scores())
    blob = " ".join([username or "", channel_name or "", caption_text or ""])

    if has_thai(blob):      score["th-TH"]        += 2.0
    if has_japanese(blob):  score["ja-JP"]        += 2.0
    if has_chinese(blob):
        score["cmn-Hans-CN"] += 1.4
        score["cmn-Hant-TW"] += 1.0  # มองทั้ง Hans/Hant
        score["yue-Hant-HK"] += 0.6  # กันกรณีกวางตุ้ง
    if has_korean(blob):    score["ko-KR"]        += 2.0
    if looks_vietnamese(blob): score["vi-VN"]     += 1.5
    if has_cyrillic(blob):  score["ru-RU"]        += 2.0

    return score

# ---------- เลือกภาษาสำรอง ----------
def pick_alternative_langs(
    *,
    base_lang: str = "th-TH",
    default_pool: List[str] = tuple(FALLBACK_ALTS_ORDER),
    max_alts: int = 3,  # v1 แนะนำสูงสุด 3
    channel_hist: Optional[Dict[str, int]] = None,
    user_hist: Optional[Dict[str, int]] = None,
    context_bias: Optional[Dict[str, float]] = None,
    damp_jp_when_uncertain: bool = False,   # ค่าเริ่มเป็น False เพื่อไม่กดญี่ปุ่นโดยไม่จำเป็น
    jp_min_weight: float = 2.0,
) -> List[str]:
    """
    สร้างรายการ alternativeLanguageCodes โดยถ่วงน้ำหนักจาก:
      - ประวัติภาษาในช่อง/ผู้ใช้
      - บริบท
    จากนั้นเลือก top-N (ไม่รวม base_lang) และถ้าจำนวนไม่ถึง N ให้เติมจาก FALLBACK_ALTS_ORDER
    """
    weights: Dict[str, float] = {lang: 0.0 for lang in default_pool}

    # น้ำหนัก: ผู้ใช้ > ช่อง > บริบท
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

    # เติมให้ครบ N จากลำดับสำรอง (ห้ามซ้ำ)
    if len(alts) < max_alts:
        for lang in FALLBACK_ALTS_ORDER:
            if lang != base_lang and lang not in alts:
                alts.append(lang)
            if len(alts) >= max_alts:
                break

    return alts[:max_alts]

# ---------- ตรวจสคริปต์จากผลถอดเสียง ----------
def detect_script_from_text(s: str) -> str:
    if has_thai(s):      return "th-TH"
    if has_japanese(s):  return "ja-JP"
    if has_chinese(s):   return "cmn-Hans-CN"  # เดา Hans เป็นค่าเริ่มต้น
    if has_korean(s):    return "ko-KR"
    if has_cyrillic(s):  return "ru-RU"
    return "en-US"

# ---------- กลยุทธ์สองจังหวะ ----------
def choose_alts_strict_first(
    *,
    base_lang: str = "th-TH",
    alt_smart: Optional[List[str]] = None,  # ลิสต์เรียงตามความน่าจะเป็นสูง→ต่ำ (มากกว่า 3 ก็ได้)
    force_strict_if_confident: bool = True,
    context_bias: Optional[Dict[str, float]] = None,
    strict_confidence_threshold: float = 2.0,
    exclude_in_fallback: Optional[List[str]] = None,
    per_round_limit: int = 3,  # v1: alt ได้ ~3
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """
    คืนค่า (alts_round1, alts_round2)
      - รอบ 1: alt_smart[:3] หรือ strict (None) ถ้ามั่นใจใน base
      - รอบ 2: alt_smart ตัวถัดไป (เช่น [3:6]) โดยตัดด้วย exclude_in_fallback
    """
    seq = list(alt_smart or [])

    # strict ถ้ามั่นใจ base_lang
    if force_strict_if_confident and context_bias:
        if context_bias.get(base_lang, 0.0) >= strict_confidence_threshold:
            alts_round1 = None
        else:
            alts_round1 = seq[:per_round_limit] or None
    else:
        alts_round1 = seq[:per_round_limit] or None

    # รอบ 2: ก้อนถัดไป
    rest = seq[per_round_limit:]
    if exclude_in_fallback:
        rest = [x for x in rest if x not in set(exclude_in_fallback)]
    alts_round2 = (rest[:per_round_limit] or (seq[:per_round_limit] if not rest else None)) or None

    return alts_round1, alts_round2

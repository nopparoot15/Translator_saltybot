# tone_translate.py
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

Lang = str

# =========================================================
# ภาค Render: คำแทนแท็กของแต่ละภาษา (เลือกให้ "เป็นธรรมชาติ")
# ถ้าไม่พบภาษานั้น จะ fallback เป็น EN อัตโนมัติ
# =========================================================
TAG_RENDER: Dict[Lang, Dict[str, List[str]]] = {
    "th": {
        "BRO": ["มึง", "แก"],
        "HUH": ["วะ", "ว่ะ", "นะ"],
        "LOL": ["555", "ฮ่าๆ"],
        "KINDOF": ["เหมือนจะ", "ค่อนข้าง"],
        "SARC": ["(ประชด)"],
    },
    "en": {
        "BRO": ["bro", "dude", "man"],
        "HUH": ["huh", "right"],
        "LOL": ["lol", "haha", "lmao"],
        "KINDOF": ["kinda", "sort of", "low-key"],
        "SARC": ["(sarcasm)"],
    },
    "ja": {
        "BRO": ["お前", "君"],
        "HUH": ["な", "ね", "よな"],
        "LOL": ["笑", "草"],
        "KINDOF": ["なんか", "ちょっと"],
        "SARC": ["（皮肉）"],
    },
    "zh": {
        "BRO": ["哥们", "兄弟", "老铁"],
        "HUH": ["吧", "嘛"],
        "LOL": ["哈哈", "笑死"],
        "KINDOF": ["有点", "有那么点"],
        "SARC": ["（讽刺）"],
    },
    "ko": {
        "BRO": ["야", "브로"],
        "HUH": ["냐", "지"],
        "LOL": ["ㅋㅋ", "ㅎㅎ"],
        "KINDOF": ["약간", "좀"],
        "SARC": ["(비꼼)"],
    },
    "vi": {
        "BRO": ["mày", "ông"],
        "HUH": ["hả", "nhỉ"],
        "LOL": ["haha", "kkk"],
        "KINDOF": ["kiểu như", "hơi bị"],
        "SARC": ["(mỉa mai)"],
    },
    "fil": {
        "BRO": ["pre", "pare", "tol"],
        "HUH": ["no?", "ha?"],
        "LOL": ["haha", "lmao"],
        "KINDOF": ["parang", "medyo"],
        "SARC": ["(sarkasmo)"],
    },
    "id": {
        "BRO": ["bro", "bang"],
        "HUH": ["kan?", "ya?"],
        "LOL": ["wkwk", "haha"],
        "KINDOF": ["kayak", "agak"],
        "SARC": ["(nyinyir)"],
    },
    "fr": { "BRO": ["mec", "frère"], "HUH": ["hein", "non?"], "LOL": ["mdr", "haha"], "KINDOF": ["un peu", "genre"], "SARC": ["(sarcasme)"] },
    "de": { "BRO": ["Alter", "Digga"], "HUH": ["oder?", "ne?"], "LOL": ["lol", "haha"], "KINDOF": ["irgendwie", "ein bisschen"], "SARC": ["(Sarkasmus)"] },
    "es": { "BRO": ["wey", "tío"], "HUH": ["¿no?", "¿eh?"], "LOL": ["jaja", "xd"], "KINDOF": ["como que", "medio"], "SARC": ["(sarcasmo)"] },
    "it": { "BRO": ["bro", "amico"], "HUH": ["eh?", "no?"], "LOL": ["ahaha", "lol"], "KINDOF": ["tipo", "un po'"], "SARC": ["(sarcasmo)"] },
    "pt": { "BRO": ["mano", "cara"], "HUH": ["né?", "hein?"], "LOL": ["kkk", "haha"], "KINDOF": ["meio que", "um pouco"], "SARC": ["(sarcasmo)"] },
    "pl": { "BRO": ["stary", "ziomek"], "HUH": ["no nie?", "co?"], "LOL": ["xD", "haha"], "KINDOF": ["trochę", "tak jakby"], "SARC": ["(sarkazm)"] },
    "uk": { "BRO": ["друже", "чувак"], "HUH": ["га?", "так?"], "LOL": ["лол", "хаха"], "KINDOF": ["ніби", "трохи"], "SARC": ["(сарказм)"] },
    "ru": { "BRO": ["чувак", "бро"], "HUH": ["а?", "да?"], "LOL": ["лол", "ахаха"], "KINDOF": ["типа", "слегка"], "SARC": ["(сарказм)"] },
    "hi": { "BRO": ["यार", "भाई"], "HUH": ["ना", "क्या"], "LOL": ["हाहा", "lol"], "KINDOF": ["थोड़ा", "किस्म का"], "SARC": ["(तंज)"] },
    "km": { "BRO": ["បង"], "HUH": ["ដែរ?", "ម៉េច?"], "LOL": ["ហា​ហា"], "KINDOF": ["ប្រហែល", "តិចតួច"], "SARC": ["(សើចចំ)"] },
    "my": { "BRO": ["ရေ"], "HUH": ["လား", "နော်"], "LOL": ["ဟားဟား"], "KINDOF": ["အနည်းငယ်"], "SARC": ["(သံယောဇဥ်)"] },
    "ar": { "BRO": ["يا زلمة", "يا صاح"], "HUH": ["ها؟", "مش كده؟"], "LOL": ["هههه", "لول"], "KINDOF": ["شوية", "كده"], "SARC": ["(سخرية)"] },
}
TAG_RENDER["fil-PH"] = TAG_RENDER["fil"]
TAG_RENDER["tl"] = TAG_RENDER["fil"]

# =========================================================
# Helpers
# =========================================================
def _pick(lang: Lang, key: str) -> str:
    lst = TAG_RENDER.get(lang, TAG_RENDER["en"]).get(key) or TAG_RENDER["en"].get(key, [])
    return lst[0] if lst else ""

_TAG_NAME_RE = re.compile(r"<([A-Z_]+)>")

def _collect_tags(s: str) -> set[str]:
    return set(_TAG_NAME_RE.findall(s or ""))

def _strip_unallowed_tags(text: str, allowed: set[str]) -> str:
    if not text:
        return text
    def _repl(m: re.Match) -> str:
        tag = m.group(1)
        return f"<{tag}>" if tag in allowed else ""
    return _TAG_NAME_RE.sub(_repl, text)

# =========================================================
# 1) PRE-ANNOTATE — ใส่แท็กจากข้อความต้นฉบับ
# =========================================================
@dataclass
class ToneHints:
    has_slang: bool = False
    sarcasm: bool = False
    intensity: str = "normal"  # soft|normal|hard
    style: str = "preserve"    # preserve|neutralize

# สัญญาณตรวจจับแบบกว้าง ๆ
_TAGS = {
    "BRO": r"(มึง|แก|เอ็ง|\b(bro|dude|man)\b|お前|哥们|兄弟|老铁|야|mày|pre|tol|bang|wey|mano|stary|чувак|يا زلمة)",
    "LOL": r"(555+|ฮ่า+|ขำ|\blol\b|\blmao\b|haha|草|笑|ㅋㅋ+|ㅎㅎ+|jaja|xd|\bkkk\b|mdr|xD|лол|ахаха|هههه)",
    "KINDOF": r"(เหมือนจะ|ค่อนข้าง|นิดหน่อย)|\b(kinda|sort of|low[- ]?key)\b|なんか|ちょっと|有点|약간|parang|medyo|kayak|agak|un poco|irgendwie|medio|tipo|meio que|trochę|ніби|типа|شوية",
    # ลงท้ายเชิงแหย่/ย้ำ
    "HUH_END": r"(วะ|ว่ะ|นะ|ปะ|หรือไง)\s*$|(?:\b(huh|right|eh)\b|¿no\?|né\?)\s*$|[なね]？?$|吧?$|냐\?$",
    "SARC": r"(ประชด|แดกดัน)|\(sarcasm\)|皮肉|讽刺|비꼼|sarkasmo|Sarkasmus|sarcasmo|сарказм|سخرية",
}

def pre_annotate(text: str, src_lang: Lang) -> Tuple[str, ToneHints]:
    """
    ตรวจคำสแลง/รูปประโยค แล้วแปะแท็ก <BRO> <LOL> <KINDOF> และ <HUH> (ถ้าเข้าเงื่อนไข)
    """
    t = (text or "").strip()
    hints = ToneHints()

    if re.search(_TAGS["BRO"], t, flags=re.I):
        t = f"<BRO>{t}"
        hints.has_slang = True

    if re.search(_TAGS["LOL"], t, flags=re.I):
        t = f"<LOL>{t}"
        hints.has_slang = True

    if re.search(_TAGS["KINDOF"], t, flags=re.I):
        t = f"<KINDOF>{t}"
        hints.has_slang = True

    if re.search(_TAGS["HUH_END"], t, flags=re.I):
        t = f"{t} <HUH>"
        hints.has_slang = True

    if re.search(_TAGS["SARC"], t, flags=re.I):
        t = f"<SARC>{t}"
        hints.sarcasm = True

    return t, hints

# =========================================================
# 2) PROMPTS
# =========================================================
def build_tone_prompt(src: Lang, tgt: Lang) -> str:
    return (
        "You are a tone-preserving translator.\n"
        "RULES:\n"
        "1) Preserve slang/register and teasing/sarcasm if present.\n"
        "2) NEVER translate or remove tags: <BRO>, <HUH>, <LOL>, <KINDOF>, <SARC>.\n"
        "3) Do NOT invent new tags; only keep tags already present in the input.\n"
        "4) Translate the rest naturally into the target language.\n"
        "5) Return ONLY the translated text with the tags kept.\n"
        f"Source={src} Target={tgt}\n"
    )

def build_natural_prompt(src: Lang, tgt: Lang, hints: ToneHints) -> str:
    style_line = (
        "If style='neutralize', slightly soften profanity but keep the teasing intent.\n"
        if hints.style == "neutralize" else
        "Keep original roughness if present; do NOT sanitize profanity.\n"
    )
    intensity_line = {
        "soft": "Hedge more if casual. ",
        "normal": "",
        "hard": "Allow strong slang/insults if natural in the target. ",
    }[hints.intensity]

    return (
        "You are a **tone-preserving, natural translator**.\n"
        "Goals:\n"
        "1) Preserve register (casual/rude/polite), teasing/sarcasm, and slang strength.\n"
        "2) Make the target sentence sound NATURAL for native speakers.\n"
        "3) NEVER translate/remove angle-bracket tags (<BRO>, <HUH>, <LOL>, <KINDOF>, <SARC>).\n"
        "4) Do NOT invent new tags; only keep tags already present in the input.\n"
        "5) Choose idiomatic equivalents for slang/insults common in the target language.\n"
        f"{style_line}{intensity_line}"
        f"Source={src} Target={tgt}\n"
        "Return ONLY the translated text with the tags kept."
    )

# =========================================================
# 3) POST-RENDER — แทนแท็กเป็นสำนวนภาษาปลายทาง
# =========================================================
def post_render(text: str, tgt_lang: Lang) -> str:
    out = (text or "").strip()
    prof = TAG_RENDER.get(tgt_lang, TAG_RENDER["en"])

    def rep(tag: str) -> str:
        lst = prof.get(tag) or TAG_RENDER["en"].get(tag, [])
        return lst[0] if lst else ""

    if "<BRO>" in out:
        out = out.replace("<BRO>", (rep("BRO") + " ") if rep("BRO") else "")
    if "<LOL>" in out:
        out = out.replace("<LOL>", (" " + rep("LOL")) if rep("LOL") else "")
    if "<KINDOF>" in out:
        out = out.replace("<KINDOF>", (" " + rep("KINDOF")) if rep("KINDOF") else "")
    if "<HUH>" in out:
        out = out.replace("<HUH>", (", " + rep("HUH")) if rep("HUH") else "")
    if "<SARC>" in out:
        out = out.replace("<SARC>", (" " + rep("SARC")) if rep("SARC") else "")

    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+,", ",", out)
    return out.strip()

# =========================================================
# 4) SMART TRANSLATE — Entry point สำหรับ translation_service
# =========================================================
@dataclass
class _Hints(ToneHints):
    pass

def _pre_annotate_natural(text: str, src_lang: Lang, style: str) -> tuple[str, _Hints]:
    annotated, h = pre_annotate(text, src_lang)
    h.style = style
    return annotated, _Hints(**h.__dict__)

async def smart_translate(
    text: str,
    src: Lang,
    tgt: Lang,
    engine: str,
    *,
    style: str = "preserve",      # "preserve" | "neutralize"
    natural_pass: bool = True,
    llm_translate_callable=None,  # async def (text, src, tgt, engine, system_prompt=...)
) -> str:
    """
    ใช้ร่วมกับ translation_service.llm_translate_wrapper
    คืนค่าเป็น "ข้อความล้วน" (ไม่มี <T>...</T>)
    """
    assert llm_translate_callable is not None, "smart_translate requires llm_translate_callable"

    annotated, hints = _pre_annotate_natural(text or "", src, style=style)
    allowed_tags = _collect_tags(annotated)  # <--- อนุญาตเฉพาะแท็กที่เราติดไว้

    system_prompt = build_natural_prompt(src, tgt, hints) if natural_pass else build_tone_prompt(src, tgt)
    raw = await llm_translate_callable(annotated, src, tgt, engine, system_prompt=system_prompt)

    # ลบแท็กที่ LLM เผลอแถมมา (เช่น <HUH> ทั้งที่เราไม่ได้ใส่)
    raw = _strip_unallowed_tags(raw, allowed_tags)

    # เรนเดอร์แท็กเป็นสำนวนปลายทาง
    return post_render(raw, tgt)

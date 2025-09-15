from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

Lang = str

# --- 1) Lexicon คำสแลง/คำด่าแบบหลายภาษา (มีระดับความแรง) ---
# หมายเหตุ: ไม่ต้องครบจักรวาล แต่พอให้ “ฟีลธรรมชาติ” ขึ้นมาก
# สามารถเติม/แก้คำตามคอมมูนิตี้ของคุณได้เรื่อย ๆ
SLANG_LEX: Dict[Lang, Dict[str, Dict[str, str]]] = {
    # Thai
    "th": {
        "เชี่ย":   {"cat": "insult", "sev": "mid", "neutral": "ซวยละ"},
        "เหี้ย":    {"cat": "insult", "sev": "high", "neutral": "แย่จัด"},
        "ไอเวร":   {"cat": "insult", "sev": "mid", "neutral": "ไอนี่"},
        "ไอบ้า":   {"cat": "insult", "sev": "low", "neutral": "บ้าไปแล้ว"},
        "แม่ง":     {"cat": "intensity", "sev": "low", "neutral": "โคตร"},
        "กู":       {"cat": "pronoun_1p", "sev": "casual", "neutral": "ฉัน"},
        "มึง":      {"cat": "pronoun_2p", "sev": "casual", "neutral": "นาย"},
        "ว่ะ":     {"cat": "ending", "sev": "casual", "neutral": "นะ"},
        "วะ":      {"cat": "ending", "sev": "casual", "neutral": "นะ"},
    },
    # English
    "en": {
        "shit":         {"cat": "insult", "sev": "mid", "neutral": "dang"},
        "fuck":         {"cat": "insult", "sev": "high", "neutral": "freaking"},
        "asshole":      {"cat": "insult", "sev": "high", "neutral": "jerk"},
        "idiot":        {"cat": "insult", "sev": "low", "neutral": "dummy"},
        "moron":        {"cat": "insult", "sev": "mid", "neutral": "dumbass"},
        "bro":          {"cat": "pronoun_2p", "sev": "casual", "neutral": "you"},
        "dude":         {"cat": "pronoun_2p", "sev": "casual", "neutral": "you"},
        "man":          {"cat": "discourse", "sev": "casual", "neutral": ""},
        "huh":          {"cat": "ending", "sev": "casual", "neutral": ""},
        "kinda":        {"cat": "hedge", "sev": "soft", "neutral": "somewhat"},
        "low-key":      {"cat": "hedge", "sev": "soft", "neutral": "slightly"},
        "lol":          {"cat": "laugh", "sev": "casual", "neutral": "haha"},
    },
    # Japanese
    "ja": {
        "バカ": {"cat":"insult","sev":"low","neutral":"バカ"},
        "アホ": {"cat":"insult","sev":"low","neutral":"アホ"},
        "くそ": {"cat":"insult","sev":"mid","neutral":"やば"},
        "お前": {"cat":"pronoun_2p","sev":"casual","neutral":"君"},
        "だよな": {"cat":"ending","sev":"casual","neutral":"よね"},
        "な？":  {"cat":"ending","sev":"casual","neutral":"よね？"},
    },
    # Spanish
    "es": {
        "idiota": {"cat":"insult","sev":"low","neutral":"tonto"},
        "boludo": {"cat":"insult","sev":"mid","neutral":"tarado"},
        "wey":    {"cat":"pronoun_2p","sev":"casual","neutral":"tío"},
        "¿no?":   {"cat":"ending","sev":"casual","neutral":"¿no?"},
        "jaja":   {"cat":"laugh","sev":"casual","neutral":"jaja"},
    },
    # Filipino
    "fil": {
        "tanga": {"cat":"insult","sev":"mid","neutral":"bobo"},
        "bobo":  {"cat":"insult","sev":"low","neutral":"tanga"},
        "pre":   {"cat":"pronoun_2p","sev":"casual","neutral":"ikaw"},
        "tol":   {"cat":"pronoun_2p","sev":"casual","neutral":"ikaw"},
        "no?":   {"cat":"ending","sev":"casual","neutral":"no?"},
    },
    # เพิ่ม zh/ko/id/ru/... ตามต้องการในภายหลังได้
}

# ให้ alias ตกไปภาษาพื้นฐาน
for alias, base in [("fil-PH","fil"), ("tl","fil")]:
    if base in SLANG_LEX and alias not in SLANG_LEX:
        SLANG_LEX[alias] = SLANG_LEX[base]

# --- 2) Hints & Config ---
@dataclass
class ToneHints:
    has_slang: bool = False
    sarcasm: bool = False
    intensity: str = "normal"  # "soft"|"normal"|"hard"
    style: str = "preserve"    # "preserve"|"neutralize"

# --- 3) Utility: ตรวจสแลง/คำด่าเพื่อชี้นำ LLM ---
def detect_slang_intent(text: str, lang: Lang) -> ToneHints:
    t = text.strip()
    hints = ToneHints()
    lex = SLANG_LEX.get(lang, {})
    hit = 0
    for w in lex.keys():
        if re.search(rf"\b{re.escape(w)}\b", t, flags=re.I):
            hit += 1
            cat = lex[w]["cat"]
            if cat in ("insult","pronoun_2p","ending","hedge","laugh"):
                hints.has_slang = True
            if cat == "insult" and lex[w]["sev"] in ("mid","high"):
                hints.intensity = "hard"
    # heuristic เพิ่มเติม
    if re.search(r"(เหมือนจะ|ค่อนข้าง|นิดหน่อย|kinda|low[- ]?key|なんか|ちょっと|有点)", t, flags=re.I):
        hints.has_slang = True
    if re.search(r"(ประชด|แดกดัน|\(sarcasm\)|皮肉|讽刺|비꼼|sarkasmo|Sarkasmus|sarcasmo|сарказм|سخرية)", t, flags=re.I):
        hints.sarcasm = True
    return hints

# --- 4) Prompt สั่ง LLM ให้ทั้ง “คงแท็ก” และ “พูดเป็นธรรมชาติ” ---
def build_natural_prompt(src: Lang, tgt: Lang, hints: ToneHints) -> str:
    style_line = (
        "If style='neutralize', slightly soften profanity but keep the teasing intent.\n"
        if hints.style == "neutralize" else
        "Keep original roughness if present; do NOT sanitize profanity.\n"
    )
    intensity_line = {
        "soft":   "Hedge more (kinda/sort of / ちょっと / 有点). ",
        "normal": "",
        "hard":   "Allow strong slang/insults in target if common/natural. ",
    }[hints.intensity]

    # few-shot สั้น ๆ เป็นแนวทางธรรมชาติ (ระวังไม่ให้ยาวเกิน)
    examples = (
        "Examples:\n"
        "- TH casual → EN: '<BRO>เหมือนมึงหล่ออะ <HUH>' → 'bro, it’s like you’re handsome, huh'\n"
        "- EN insult → TH: 'you idiot, seriously' → 'ไอ้โง่ จริงจังดิ'\n"
        "- ES tease → EN: 'Wey, como que te crees famoso ¿no?' → 'bro, kinda acting like you’re famous, huh?'\n"
        "- JA casual → TH: 'お前 調子乗ってんな' → 'มึง กำลังหลงตัวเองนะ'\n"
    )

    return (
        "You are a **tone-preserving, natural translator**.\n"
        "Goals:\n"
        "1) Preserve register (casual/rude/polite), teasing/sarcasm, slang strength.\n"
        "2) Make the target sentence SOUND NATURAL for a native speaker of the target language.\n"
        "3) NEVER translate or remove tags like <BRO>, <HUH>, <LOL>, <KINDOF>, <SARC>.\n"
        "4) Choose idiomatic equivalents for slang/insults that are commonly used in the target language.\n"
        f"{style_line}{intensity_line}"
        f"Source={src} Target={tgt}\n"
        f"{examples}"
        "Return ONLY the translated text with tags kept in place."
    )

# --- 5) Pre-annotate (คุณมีอยู่แล้ว) + ปรับเล็กน้อยให้เรียก detect_slang_intent ---
def pre_annotate_natural(text: str, src_lang: Lang, style: str = "preserve") -> Tuple[str, ToneHints]:
    # ใช้ของเดิม: ใส่ <BRO>/<HUH>/... (ถ้ามี)
    annotated, base_hints = pre_annotate(text, src_lang)  # <-- ฟังก์ชันเดิมในไฟล์คุณ
    # เติม hints จาก lexicon
    lex_hints = detect_slang_intent(text, src_lang)
    base_hints.has_slang = base_hints.has_slang or lex_hints.has_slang
    base_hints.sarcasm  = base_hints.sarcasm  or lex_hints.sarcasm
    if lex_hints.intensity == "hard":
        base_hints.intensity = "hard"
    base_hints.style = style
    return annotated, base_hints

# --- 6) ฟังก์ชัน high-level ใช้แทน translate_with_tone ของเดิม ---
async def smart_translate(
    text: str,
    src: Lang,
    tgt: Lang,
    engine: str,
    *,
    style: str = "preserve",      # "preserve" | "neutralize"
    natural_pass: bool = True,    # ให้ LLM ช่วย “ทำให้เป็นธรรมชาติ” ระหว่างแปล
    llm_translate_callable=None,  # ฟังก์ชันเรียก LLM ของโปรเจกต์คุณ
) -> str:
    """
    ใช้แทนที่เดิม: natural, slang-aware, tone-preserving
    """
    assert llm_translate_callable is not None, "Provide llm_translate(text, src, tgt, engine, system_prompt=...)"

    # 1) annotate + hints
    annotated, hints = pre_annotate_natural(text, src, style=style)

    # 2) system prompt
    system_prompt = build_natural_prompt(src, tgt, hints) if natural_pass \
                    else build_tone_prompt(src, tgt)  # fallback prompt เดิม

    # 3) แปล (เก็บแท็กไว้)
    raw = await llm_translate_callable(annotated, src, tgt, engine, system_prompt=system_prompt)

    # 4) post-render แท็ก → สำนวนภาษาปลายทาง
    out = post_render(raw, tgt)

    # 5) (ออปชัน) ถ้า style='neutralize' ให้ลดความหยาบแรงนิดหนึ่งโดยใช้ lexicon เป้า
    if style == "neutralize":
        out = soften_profanity(out, tgt)

    # เกลาครั้งสุดท้าย: ช่องว่าง/วรรคตอน
    out = re.sub(r"\s{2,}", " ", out).strip()
    out = re.sub(r"\s+,", ",", out)
    return out

def soften_profanity(text: str, tgt: Lang) -> str:
    lex = SLANG_LEX.get(tgt, {})
    out = text
    # แทนคำแรงระดับ high → mid/neutral ถ้ามีใน lexicon
    for w, meta in lex.items():
        if meta["cat"] == "insult" and meta.get("sev") in ("mid","high"):
            out = re.sub(rf"\b{re.escape(w)}\b", meta.get("neutral", w), out, flags=re.I)
    return out

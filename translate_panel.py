from __future__ import annotations

from io import BytesIO
import discord
from typing import Callable, Optional, Dict, Any, Tuple, List
import regex as re
from types import SimpleNamespace
from collections import defaultdict

# ===== Language choices =====
LANG_CHOICES: List[Tuple[str, str]] = [
    ("th", "Thai"),
    ("en", "English"),
    ("ja", "Japanese"),
    ("zh-CN", "Chinese"),
    ("ko", "Korean"),
    ("vi", "Vietnamese"),
    ("fil", "Filipino"),
    ("id", "Indonesian"),
    ("hi", "Hindi"),
    ("km", "Khmer"),
    ("my", "Burmese"),
    ("fr", "French"),
    ("de", "German"),
    ("es", "Spanish"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("pl", "Polish"),
    ("uk", "Ukrainian"),
    ("ar", "Arabic"),
]

DEFAULT_MAJOR_LANGS = ["th", "en", "ja"]  # ปุ่มหลัก 3 ภาษา

# ===== Helpers =====
def source_hint_for_ja(text: str) -> Optional[str]:
    if not text:
        return None
    if re.search(r'[\p{Hiragana}\p{Katakana}]', text):
        return "ja"
    t = re.sub(r'\s+', '', text)
    if len(t) <= 2 and re.fullmatch(r'[\p{Han}]+', t):
        return "ja"
    return None

def _finalize_text(src_text: str, translated: str) -> str:
    if not translated or not translated.strip():
        return src_text
    if translated.strip().lower() == (src_text or "").strip().lower():
        return src_text
    return translated

def _parse_target_from_content(content: str) -> Optional[str]:
    """
    ค้นหา Target code จากทั้งข้อความ (เผื่อมีบรรทัด Engine อยู่บนสุด)
    รองรับทั้งรูปแบบ **Target:** `xx` และ fallback จาก backticks ในบรรทัดที่มีคำว่า Target
    """
    if not content:
        return None

    # 1) หา **Target:** `xx` จากทั้งข้อความก่อน
    m = re.search(r"Target[^`:\n]*:\s*`?\s*([A-Za-z]{2,3}(?:-[A-Za-z]{2,3})?)\s*`?", content, flags=re.I)
    if m:
        return m.group(1).strip()

    # 2) ไล่ทีละบรรทัด: ถ้าบรรทัดไหนมีคำว่า Target ให้ลองดึง code ใน backticks จากบรรทัดนั้น
    for line in content.splitlines():
        if re.search(r"Target", line, flags=re.I):
            codes = re.findall(r"`\s*([A-Za-z]{2,3}(?:-[A-Za-z]{2,3})?)\s*`", line)
            if codes:
                return codes[-1].strip()

    return None


def _parse_source_from_content(content: str) -> Optional[str]:
    """
    ค้นหา Source code จากทั้งข้อความ (เผื่อมีบรรทัด Engine อยู่บนสุด)
    รองรับทั้งรูปแบบ **Source:** `xx` และ fallback จาก backticks ในบรรทัดที่มีคำว่า Source
    """
    if not content:
        return None

    # 1) หา **Source:** `xx` จากทั้งข้อความก่อน
    m = re.search(r"Source[^`:\n]*:\s*`?\s*([A-Za-z]{2,3}(?:-[A-Za-z]{2,3})?)\s*`?", content, flags=re.I)
    if m:
        return m.group(1).strip()

    # 2) ไล่ทีละบรรทัด: ถ้าบรรทัดไหนมีคำว่า Source ให้ลองดึง code ใน backticks จากบรรทัดนั้น
    for line in content.splitlines():
        if re.search(r"Source", line, flags=re.I):
            codes = re.findall(r"`\s*([A-Za-z]{2,3}(?:-[A-Za-z]{2,3})?)\s*`", line)
            if codes:
                return codes[0].strip()

    return None


def _parse_result_text_from_content(content: str, flags: Dict[str, str]) -> Optional[str]:
    """
    ดึงข้อความผลลัพธ์:
    - พยายามดึงจาก code block ```...``` (รองรับ ```lang\n...```)
    - ถ้าไม่เจอ ใช้บรรทัดที่สองแบบรุ่นเก่า และลอกธงออกถ้ามี
    """
    if not content:
        return None

    # 1) จาก code block (มีหรือไม่มีภาษา)
    m = re.search(r"```(?:[A-Za-z0-9_+\-]+\n)?(.*?)```", content, flags=re.S)
    if m:
        return (m.group(1) or "").strip()

    # 2) fallback รุ่นเก่า: บรรทัดที่สอง อาจขึ้นต้นด้วยธง
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None

    second = lines[1].lstrip()
    for fl in flags.values():
        if fl and second.startswith(fl):
            second = second[len(fl):].lstrip()
            break

    return second or None

def _engine_label_line(message: discord.Message | None, provider_cb: Optional[Callable[[discord.Message], str]]) -> str:
    if not provider_cb or not isinstance(message, discord.Message):
        return ""
    try:
        label = (provider_cb(message) or "").strip()
    except Exception:
        label = ""
    # ให้เป็นสไตล์เดียวกับ Source/Target
    return (f"**Engine:** `{label}`\n") if label else ""
    

async def send_transcript(
    message: discord.Message,
    text: str,
    stt_tag: str,
    *,
    # ใส่ภาษาให้โชว์ เช่น "ja" / "ja-JP" / "th-TH"
    lang_display: Optional[str] = None,
    # ซ่อน/แสดง Engine (เราจะส่ง False ตอนใช้กับ STT)
    show_engine: bool = True,
    engine_label_provider=None,
    # ถ้าจะอ้างอิงไปยังข้อความอื่น (เช่น ข้อความที่แนบไฟล์เสียง)
    reply_to: Optional[discord.Message] = None,
):
    # ------- เตรียม label ต่าง ๆ -------
    engine_label = ""
    if show_engine and callable(engine_label_provider) and isinstance(message, discord.Message):
        try:
            engine_label = (engine_label_provider(message) or "").strip()
        except Exception:
            engine_label = ""

    # --- preview (กันล้น embed) ---
    safe_text = (text or "").replace("```", "``\u200b`")
    PREVIEW_MAX = 1800
    is_truncated = len(safe_text) > PREVIEW_MAX
    preview = safe_text[:PREVIEW_MAX] + ("…" if is_truncated else "")

    # ------- สร้าง embed -------
    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_author(name="Translator bot")
    # ❌ ไม่ใส่ Engine เมื่อ show_engine=False
    if show_engine and engine_label:
        embed.add_field(name="Engine", value=engine_label, inline=True)

    # ใส่โหมด STT เสมอ
    embed.add_field(name="STT", value=stt_tag, inline=True)

    # ✅ โชว์ภาษาที่ผู้ใช้เลือก
    if lang_display:
        embed.add_field(name="Lang", value=f"`{lang_display}`", inline=True)

    desc = f"📝 **Transcript (preview):**\n```{preview}```"
    if is_truncated:
        desc += "\n_(ข้อความยาว – แนบไฟล์ฉบับเต็มไว้ให้แล้ว)_"
    embed.description = desc

    # ส่งเป็น reply ไปยังข้อความที่แนบไฟล์เสียง (ถ้าไม่ได้ส่งมาก็อ้างอิง message เดิม)
    ref_msg = reply_to if isinstance(reply_to, discord.Message) else message
    msg = await message.channel.send(embed=embed, reference=ref_msg, mention_author=False)

    # แนบไฟล์ฉบับเต็มถ้ายาว
    if is_truncated:
        from io import BytesIO
        bio = BytesIO((text or "").encode("utf-8"))
        bio.seek(0)
        await message.channel.send(
            content="📎 **Full transcript (TXT)**",
            file=discord.File(bio, filename="transcript.txt"),
            reference=ref_msg,
            mention_author=False,
        )

    return msg


def _format_result_content(
    src_code: Optional[str],
    tgt_code: str,
    flag: str,
    translated: str,
    *,
    engine_line: str = "",   # NEW
) -> str:
    labels_en = dict(LANG_CHOICES)
    base = (tgt_code or "").split("-")[0]
    label = labels_en.get(tgt_code) or labels_en.get(base) or tgt_code

    body = (translated or "").strip()
    body = body.replace("```", "``\u200b`")  # กัน code fence แตก

    src_disp = src_code or "auto"
    return (
        f"{engine_line}"  # NEW: โชว์ engine ถ้ามี
        f"**Source:** `{src_disp}` → **Target:** `{tgt_code}`\n"
        f"{flag} Translated to {label}:\n"
        f"```{body}```"
    )

def _norm_lang(code: Optional[str]) -> str:
    c = (code or "").strip()
    if not c:
        return "auto"
    cl = c.lower()
    if cl in ("zh", "zh-cn", "zh_cn"):
        return "zh-CN"
    if cl in ("ja", "ja-jp", "ja_jp"):
        return "ja"
    if cl in ("fil", "fil-ph", "tl", "tl-ph"):
        return "fil"
    # ค่าอย่าง en-GB → เอา base 'en' ก็พอสำหรับ TTS ส่วนใหญ่
    return c.split("-")[0]
    

# ===== Playback cycle state =====
_cycle_state = defaultdict(int)
NORMAL_RATE = 1.0
SLOW_RATE = 0.8

def next_rate(user_id: int, message_id: int, button_tag: str) -> float:
    key = (message_id, button_tag, user_id)
    state = _cycle_state.get(key, 0)
    rate = NORMAL_RATE if state == 0 else SLOW_RATE
    _cycle_state[key] = 1 - state
    return rate

def reset_cycle_for_message(message_id: int) -> None:
    to_del = [k for k in list(_cycle_state.keys()) if k[0] == message_id]
    for k in to_del:
        del _cycle_state[k]

# ========= Persistent Result Actions (Listen) =========
LISTEN_RESULT_CUSTOM_ID = "translate_panel_listen_result_v2"
LISTEN_SOURCE_CUSTOM_ID = "translate_panel_listen_source_v2"

class PersistentListenView(discord.ui.View):
    def __init__(
        self,
        *,
        tts_fn_multi: Callable[..., Any],
        flags: Dict[str, str],
        source_text: Optional[str] = None,   # เก็บต้นฉบับไว้ใน View
        source_lang: Optional[str] = None,   # โค้ดภาษาต้นฉบับ (เช่น "ja" หรือ "auto")
    ):
        super().__init__(timeout=None)
        self.tts_fn_multi = tts_fn_multi
        self.flags = flags
        self.source_text = (source_text or "").strip()
        self.source_lang = (source_lang or "auto").strip() or "auto"

        # ปุ่มฟังผลลัพธ์
        self.add_item(ListenResultButton())

        # ปุ่มฟังต้นฉบับ (ปิดถ้าไม่มี source_text ใน view นี้)
        src_btn = ListenSourceButton()
        src_btn.disabled = not bool(self.source_text)
        self.add_item(src_btn)


class ListenResultButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🔊 Listen (Result)",
            style=discord.ButtonStyle.secondary,
            custom_id=LISTEN_RESULT_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction):
        view: PersistentListenView = self.view  # type: ignore
        if not interaction.response.is_done():
            await interaction.response.defer()

        msg = interaction.message
        if not isinstance(msg, discord.Message) or not msg.content:
            return

        # ดึงข้อความแปลจาก code block
        result_text = _parse_result_text_from_content(msg.content, view.flags)
        if not result_text:
            return

        # ภาษาปลายทางสำหรับเสียง
        target_code = _parse_target_from_content(msg.content) or "en"
        preferred = _norm_lang(target_code)
        
        rate = next_rate(interaction.user.id, msg.id, "result")
        fake_msg = SimpleNamespace(guild=msg.guild, channel=msg.channel, author=interaction.user)
        try:
            # เดิม: await view.tts_fn_multi(fake_msg, [(result_text, target_code)], playback_rate=rate)
            await view.tts_fn_multi(
                fake_msg,
                [(result_text, preferred)],       # ส่งคู่ (ข้อความ, ภาษาที่ normalize แล้ว)
                playback_rate=rate,
                preferred_lang=preferred          # 🆕 บังคับภาษาให้ชนะ heuristic
            )
        except Exception:
            pass


class ListenSourceButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🗣️ Listen (Source)",
            style=discord.ButtonStyle.secondary,
            custom_id=LISTEN_SOURCE_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction):
        view: PersistentListenView = self.view  # type: ignore
        if not interaction.response.is_done():
            await interaction.response.defer()

        msg = interaction.message
        if not isinstance(msg, discord.Message):
            return

        # ใช้ต้นฉบับที่เก็บใน View (จะมีค่าเมื่อสร้าง view ตอนสรุปผลลัพธ์)
        original_text = (getattr(view, "source_text", "") or "").strip()
        if not original_text:
            return  # ไม่มีต้นฉบับใน view นี้ ให้กดไม่ได้ (ปุ่มถูก disable ไว้อยู่แล้ว)

        source_code = (getattr(view, "source_lang", "") or "auto").strip() or "auto"
        preferred = _norm_lang(source_code)
        
        rate = next_rate(interaction.user.id, msg.id, "source")
        fake_msg = SimpleNamespace(guild=msg.guild, channel=msg.channel, author=interaction.user)
        try:
            # เดิม: await view.tts_fn_multi(fake_msg, [(original_text, source_code)], playback_rate=rate)
            await view.tts_fn_multi(
                fake_msg,
                [(original_text, preferred)],
                playback_rate=rate,
                preferred_lang=preferred
            )
        except Exception:
            pass


# ========= ปุ่มหลัก 3 ภาษา =========
class MajorLangButton(discord.ui.Button):
    def __init__(self, code: str, label_text: str, style: discord.ButtonStyle):
        super().__init__(label=label_text, style=style, custom_id=f"translate_major_{code}")
        self.code = code

    async def callback(self, interaction: discord.Interaction):
        panel: TwoWayTranslatePanel = self.view  # type: ignore
        if not interaction.response.is_done():
            await interaction.response.defer()
        if panel.is_finished() or panel.finalized:
            return
        if not panel.allow_anyone and (interaction.user.id != panel.source_message.author.id):
            return
        await panel._perform_translate(interaction, self.code)

# ========= Dropdown เลือกภาษาอื่น ๆ =========
class OtherLangsSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(
            placeholder="🌐 Choose another language...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        panel: TwoWayTranslatePanel = self.view  # type: ignore
        if not interaction.response.is_done():
            await interaction.response.defer()
        if panel.is_finished() or panel.finalized:
            return
        if not panel.allow_anyone and (interaction.user.id != panel.source_message.author.id):
            return
        tgt = self.values[0]
        await panel._perform_translate(interaction, tgt)

# ========= VIEW หลัก =========
class TwoWayTranslatePanel(discord.ui.View):
    def __init__(
        self,
        *,
        source_message: discord.Message,
        translate_fn: Callable[..., Any],
        clean_fn: Callable[[str, str], str],
        lang_names: Dict[str, str],
        flags: Dict[str, str],
        tts_fn_multi: Callable[..., Any],
        timeout: int = 180,
        allow_anyone: bool = True,
        major_langs: Optional[List[str]] = None,   # NEW: ระบุปุ่มภาษาหลักจากภายนอก
        major_primary: Optional[str] = None,       # NEW: ระบุปุ่มไหนเป็น primary
        engine_label_provider: Optional[Callable[[discord.Message], str]] = None,
    ):
        super().__init__(timeout=timeout)
        self.engine_label_provider = engine_label_provider
        self.source_message = source_message
        self.translate_fn = translate_fn
        self.clean_fn = clean_fn
        self.lang_names = lang_names
        self.flags = flags
        self.tts_fn_multi = tts_fn_multi

        self.frame_message: Optional[discord.Message] = None
        self.allow_anyone: bool = allow_anyone
        self.finalized: bool = False

        # ===== สร้างรายการปุ่มหลัก/ภาษาอื่น แบบไดนามิก =====
        codes_all = {code for code, _ in LANG_CHOICES}
        majors = list(major_langs or DEFAULT_MAJOR_LANGS)
        self.major_langs: List[str] = [c for c in majors if c in codes_all]
        self.other_langs: List[Tuple[str, str]] = [(c, n) for c, n in LANG_CHOICES if c not in self.major_langs]

        # ปุ่มไหนเป็น primary? ถ้าไม่ระบุ ใช้อันแรกใน major_langs
        primary = major_primary if (major_primary in self.major_langs) else (self.major_langs[0] if self.major_langs else None)

        # --- แถวปุ่มภาษาหลัก (ไดนามิก) ---
        for code in self.major_langs:
            flag = self.flags.get(code, self.flags.get(code.split("-")[0], "")) or ""
            name = lang_names.get(code) or next((n for c, n in LANG_CHOICES if c == code), code)
            label = f"{flag} {name}".strip()
            style = discord.ButtonStyle.primary if (primary and code == primary) else discord.ButtonStyle.secondary
            self.add_item(MajorLangButton(code, label, style))

        # --- Dropdown สำหรับภาษาอื่น ๆ (จะไม่รวมภาษาหลักโดยอัตโนมัติ) ---
        if self.other_langs:
            options = [discord.SelectOption(label=f"{code} · {name}", value=code) for code, name in self.other_langs]
            self.add_item(OtherLangsSelect(options))

    async def attach(self, channel: discord.abc.Messageable):
        self.frame_message = await channel.send(
            content="🌐 Select the **target language** using the button above, or use the dropdown for other languages.",
            view=self,
            reference=self.source_message,
            mention_author=False,
        )

    def is_finished(self) -> bool:
        return bool(self.finalized)

    async def _perform_translate(self, interaction: discord.Interaction, tgt: str) -> None:
        src_msg = self.source_message
        frame_msg = self.frame_message
        original = (src_msg.content or "").strip()
        if not original:
            return

        # 1) ปิดปุ่มทั้งหมดทันที กันกดซ้ำ + กัน race
        for child in self.children:
            child.disabled = True
        self.finalized = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            import traceback; traceback.print_exc()

        src_hint = source_hint_for_ja(original)
        tgt_key = tgt.split("-")[0]
        tgt_name = self.lang_names.get(tgt_key, self.lang_names.get(tgt, "Target"))
        flag = self.flags.get(tgt_key, self.flags.get(tgt, ""))

        try:
            # 2) แสดงสถานะกำลังแปล
            if frame_msg:
                try:
                    await frame_msg.edit(content=f"⏳ Translating to {tgt_name}…", view=self)
                except Exception:
                    import traceback; traceback.print_exc()

            # 3) แปลจริง
            translated = await self.translate_fn(
                src_msg, original, tgt, tgt_name, source_code=src_hint
            )
            translated = self.clean_fn(original, translated)
            translated = _finalize_text(original, translated)

            # --- กันข้อความยาวเกินลิมิต Discord ---
            safe_text = (translated or "").replace("```", "``\u200b`").strip()
            PREVIEW_LIMIT = 1800  # เผื่อหัวข้อ/บรรทัดอื่น ๆ
            too_long = len(safe_text) > PREVIEW_LIMIT
            body_for_msg = safe_text[:PREVIEW_LIMIT] + ("…" if too_long else "")

            # 4) อัปเดตผลลัพธ์ + ใส่ปุ่มฟังแบบ persistent
            engine_line = _engine_label_line(self.source_message, self.engine_label_provider)
            content = _format_result_content(
                src_hint, tgt, flag, body_for_msg, engine_line=engine_line
            )
            if frame_msg:
                persistent_view = PersistentListenView(
                    tts_fn_multi=self.tts_fn_multi,
                    flags=self.flags,
                    source_text=original,
                    source_lang=src_hint or "auto",
                )
                await frame_msg.edit(content=content, view=persistent_view)
                reset_cycle_for_message(frame_msg.id)
            else:
                # fallback ถ้า frame_msg ไม่มี
                frame_msg = await src_msg.channel.send(content=content)

            # 5) ถ้ายาว แนบไฟล์ฉบับเต็มเพิ่มอีก 1 ข้อความ
            if too_long:
                from io import BytesIO
                bio = BytesIO(safe_text.encode("utf-8"))
                bio.seek(0)
                try:
                    await src_msg.channel.send(
                        content="📎 **Full translation (TXT)**",
                        file=discord.File(bio, filename="translation.txt"),
                        reference=src_msg,
                        mention_author=False,
                    )
                except Exception:
                    pass

        except Exception:
            import traceback; traceback.print_exc()
        finally:
            # 6) ปิด view เสมอ กัน state ค้าง
            self.stop()

    async def on_timeout(self) -> None:
        if self.finalized:
            return
        try:
            for child in self.children:
                child.disabled = True
            if self.frame_message:
                await self.frame_message.edit(view=self)
        except Exception:
            pass


# ========= Helper สำหรับ on_ready =========
def register_persistent_views(bot: discord.Client, tts_fn_multi: Callable[..., Any], flags: Dict[str, str]) -> None:
    try:
        # persistent view สำหรับปุ่ม (ไม่มี source_text/source_lang ตอนบูต ซึ่งโอเค)
        bot.add_view(PersistentListenView(tts_fn_multi=tts_fn_multi, flags=flags))
    except Exception:
        pass

# ========= NEW: OCR View (ใช้ translate_with_provider) =========
OCR_LISTEN_CUSTOM_ID = "ocr_listen_source_v1"
OCR_TRANSLATE_CUSTOM_ID = "ocr_translate_to_th_v2_provider"

class OCRListenTranslateView(discord.ui.View):
    def __init__(
        self,
        *,
        original_text: str,
        tts_fn_multi: Callable[..., Any],
        translate_provider_fn: Callable[..., Any],
        flags: Dict[str, str],
        allow_listen: bool = True,
        engine_label_provider: Optional[Callable[[discord.Message], str]] = None,
    ):
        super().__init__(timeout=None)
        self.engine_label_provider = engine_label_provider
        self.original_text = (original_text or "").strip()
        self.tts_fn_multi = tts_fn_multi
        self.translate_provider_fn = translate_provider_fn
        self.flags = flags
        self.allow_listen = allow_listen

        self.add_item(self._make_listen_button())
        self.add_item(self._make_translate_button())

    def _make_listen_button(self) -> discord.ui.Button:
        btn = discord.ui.Button(
            label="🗣️ Listen (Source)",
            style=discord.ButtonStyle.secondary,
            custom_id=OCR_LISTEN_CUSTOM_ID,
            disabled=(not self.allow_listen or not self.original_text),
        )

        async def _cb(interaction: discord.Interaction):
            if not interaction.response.is_done():
                await interaction.response.defer()
            msg = interaction.message
            if not isinstance(msg, discord.Message):
                return
            rate = next_rate(interaction.user.id, msg.id, "ocr_listen")
            fake_msg = SimpleNamespace(guild=msg.guild, channel=msg.channel, author=interaction.user)
            try:
                await self.tts_fn_multi(fake_msg, [(self.original_text, "auto")], playback_rate=rate)
            except Exception:
                pass

        btn.callback = _cb  # type: ignore
        return btn

    def _make_listen_result_button(self, result_text: str) -> discord.ui.Button:
        btn = discord.ui.Button(
            label="🔊 Listen (Result)",
            style=discord.ButtonStyle.secondary,
            custom_id="ocr_listen_result_v1",
            disabled=(not result_text),
        )

        async def _cb(interaction: discord.Interaction):
            if not interaction.response.is_done():
                await interaction.response.defer()
            fake_msg = SimpleNamespace(
                guild=interaction.guild, channel=interaction.channel, author=interaction.user
            )
            try:
                await self.tts_fn_multi(
                    fake_msg, [(result_text, "th")], playback_rate=1.0, preferred_lang="th"
                )
            except Exception:
                pass

        btn.callback = _cb  # type: ignore
        return btn

    def _make_translate_button(self) -> discord.ui.Button:
        btn = discord.ui.Button(
            label="🔍 Translate",
            style=discord.ButtonStyle.primary,
            custom_id=OCR_TRANSLATE_CUSTOM_ID,
            disabled=(not self.original_text),
        )

        if not hasattr(self, "processing"):
            self.processing = False

        async def _cb(interaction: discord.Interaction):
            # กันสแปม
            if self.processing:
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return
            self.processing = True

            # ปิดเฉพาะปุ่ม Translate ใน view นี้
            btn.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                if not interaction.response.is_done():
                    await interaction.response.defer()

            # ⏳ ส่ง progress โดย "reply ไปที่ข้อความ OCR"
            progress_msg = None
            try:
                progress_msg = await interaction.channel.send(
                    "⏳ Translating image…",
                    reference=interaction.message, 
                    mention_author=False,
                )
            except Exception:
                pass

            # เตรียมข้อความ
            text = (self.original_text or "").strip()
            if not text:
                msg = "⚠️ No text to translate."
                if progress_msg:
                    await progress_msg.edit(content=msg)
                else:
                    await interaction.channel.send(
                        msg, reference=interaction.message, mention_author=False
                    )
                return

            # แปลทั้งก้อน
            try:
                th = await self.translate_provider_fn(
                    interaction.message,
                    text,
                    "th",
                    "Thai",
                    source_code=None,
                )
                result = (th or "").strip() or text
            except Exception:
                result = text

            # จัดรูปข้อความผลลัพธ์
            safe_result = result.replace("```", "``\u200b`")
            body = (
                f"🇹🇭 Translated into Thai:\n```{safe_result}```"
                if len(safe_result) <= 1900 else
                f"🇹🇭 Translated into Thai:\n```{safe_result[:1800]}…```\n⚠️ Message too long, some parts were truncated"
            )
            engine_line = _engine_label_line(interaction.message, self.engine_label_provider)
            header = f"{engine_line}**Target:** `th`\n"
            final_content = f"{header}{body}"

            # สร้าง view ที่มีปุ่ม Listen(Result) เพียงปุ่มเดียว
            result_view = discord.ui.View(timeout=None)
            result_view.add_item(self._make_listen_result_button(result))

            # อัปเดต progress ให้กลายเป็น “คำแปล” (ยังคง reply โหน OCR อยู่)
            try:
                if progress_msg:
                    await progress_msg.edit(content=final_content, view=result_view)
                else:
                    await interaction.channel.send(
                        final_content,
                        view=result_view,
                        reference=interaction.message, 
                        mention_author=False,
                    )
            except Exception:
                import traceback; traceback.print_exc()

        btn.callback = _cb  
        return btn


__all__ = [
    "TwoWayTranslatePanel",
    "register_persistent_views",
    "LANG_CHOICES",
    "OCRListenTranslateView",
    "send_transcript",
]

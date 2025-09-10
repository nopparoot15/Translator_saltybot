from __future__ import annotations

import discord
from typing import Callable, Optional, List, Tuple, Any
from translate_panel import LANG_CHOICES, DEFAULT_MAJOR_LANGS  # ใช้รายการภาษา/ปุ่มหลักเดียวกับแผงแปล

# ===== ปุ่มหลัก 3 ภาษา =====
class _MajorLangButton(discord.ui.Button):
    def __init__(self, code: str, label_text: str, style: discord.ButtonStyle):
        super().__init__(label=label_text, style=style, custom_id=f"stt_choose_{code}")
        self.code = code

    async def callback(self, interaction: discord.Interaction):
        view: STTLanguagePanel = self.view  # type: ignore
        if view.processing:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return
        await view._choose(interaction, self.code)

# ===== Dropdown ภาษาอื่น ๆ =====
class _OtherLangsSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(
            placeholder="🌐 Choose another language...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view: STTLanguagePanel = self.view  # type: ignore
        if view.processing:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return
        tgt = self.values[0]
        await view._choose(interaction, tgt)

# ===== View หลัก =====
class STTLanguagePanel(discord.ui.View):
    """
    แผงถามภาษาพูดของไฟล์เสียง ก่อนเริ่ม STT
    - on_choose_lang: Coroutine ที่รับ (interaction, lang_code) แล้วไปถอดเสียง
    - flags: map code -> ธง (เช่น {"th": "🇹🇭"})
    """
    def __init__(
        self,
        *,
        source_message: discord.Message,
        on_choose_lang: Callable[[discord.Interaction, str], Any],
        flags: dict[str, str],
        timeout: int = 180,
        major_langs: Optional[List[str]] = None,
        major_primary: Optional[str] = None,
    ):
        super().__init__(timeout=timeout)
        self.source_message = source_message
        self.on_choose_lang = on_choose_lang
        self.flags = flags
        self.processing = False
        self.frame_message: Optional[discord.Message] = None

        codes_all = {code for code, _ in LANG_CHOICES}
        majors = list(major_langs or DEFAULT_MAJOR_LANGS)
        self.major_langs: List[str] = [c for c in majors if c in codes_all]
        self.other_langs: List[Tuple[str, str]] = [(c, n) for c, n in LANG_CHOICES if c not in self.major_langs]

        primary = major_primary if (major_primary in self.major_langs) else (self.major_langs[0] if self.major_langs else None)

        # ปุ่มหลัก
        for code in self.major_langs:
            flag = self.flags.get(code, self.flags.get(code.split("-")[0], "")) or ""
            name = next((n for c, n in LANG_CHOICES if c == code), code)
            label = f"{flag} {name}".strip()
            style = discord.ButtonStyle.primary if (primary and code == primary) else discord.ButtonStyle.secondary
            self.add_item(_MajorLangButton(code, label, style))

        # Dropdown อื่น ๆ
        if self.other_langs:
            options = [discord.SelectOption(label=f"{code} · {name}", value=code) for code, name in self.other_langs]
            self.add_item(_OtherLangsSelect(options))

    async def attach(self, channel: discord.abc.Messageable):
        self.frame_message = await channel.send(
            content="🌍 Select the **spoken language** of the audio using the buttons above, or use the dropdown.",
            view=self,
            reference=self.source_message,
            mention_author=False,
        )

    async def _choose(self, interaction: discord.Interaction, code: str):
        self.processing = True
        # ปิดปุ่มทั้งหมด
        for child in self.children:
            child.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(
                    content=f"⏳ Transcribing… (`{code}` selected)", view=self
                )
            else:
                await interaction.message.edit(content=f"⏳ Transcribing… (`{code}` selected)", view=self)
        except Exception:
            pass

        # เรียก callback ภายนอกเพื่อถอดเสียง
        try:
            await self.on_choose_lang(interaction, code)
        finally:
            self.stop()

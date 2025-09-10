from __future__ import annotations

import discord
from typing import Callable, Optional, List, Tuple, Any
from translate_panel import LANG_CHOICES, DEFAULT_MAJOR_LANGS  # ใช้รายการภาษา/ปุ่มหลักเดียวกับแผงแปล

# ===== map โค้ด UI -> โค้ดสำหรับ Google STT (BCP-47) =====
_STT_CODE_MAP = {
    "th": "th-TH",
    "en": "en-US",
    "ja": "ja-JP",
    "zh": "cmn-Hans-CN",   # UI 'zh' หรือ 'zh-CN' → จีนกลาง (ตัวย่อ)
    "zh-CN": "cmn-Hans-CN",
    "zh-TW": "cmn-Hant-TW",
    "yue": "yue-Hant-HK",
    "ko": "ko-KR",
    "ru": "ru-RU",
    "vi": "vi-VN",
}

def _to_stt_code(ui_code: str) -> str:
    """คืนโค้ดภาษาสำหรับ Google STT (fallback → en-US)"""
    c = (ui_code or "").strip()
    return _STT_CODE_MAP.get(c, _STT_CODE_MAP.get(c.split("-")[0], "en-US"))

# ===== ปุ่มหลัก 3 ภาษา =====
class _MajorLangButton(discord.ui.Button):
    def __init__(self, code: str, label_text: str, style: discord.ButtonStyle):
        super().__init__(label=label_text, style=style, custom_id=f"stt_choose_{code}")
        self.code = code  # โค้ด UI เช่น "th","en","ja","zh-CN"

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
        tgt_ui = self.values[0]
        await view._choose(interaction, tgt_ui)

# ===== View หลัก =====
class STTLanguagePanel(discord.ui.View):
    """
    แผงถามภาษาพูดของไฟล์เสียง ก่อนเริ่ม STT
    - on_choose_lang: Coroutine ที่รับ (interaction, stt_lang_code) แล้วไปถอดเสียง
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

    async def _choose(self, interaction: discord.Interaction, ui_code: str):
        """เมื่อผู้ใช้เลือกภาษา → ลบแผง แล้วส่งโค้ด BCP-47 ให้ callback ถอดเสียง"""
        self.processing = True

        # รับ interaction กัน timeout
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass

        # ลบกล่องเลือกภาษา
        try:
            picker_msg = interaction.message or getattr(self, "frame_message", None)
            if isinstance(picker_msg, discord.Message):
                await picker_msg.delete()
        except Exception:
            # ถ้าลบไม่ได้ให้ปิดปุ่มและแสดงสถานะแทน (ไม่บังคับ)
            try:
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(
                    content=f"⏳ Transcribing… (`{ui_code}` selected)", view=self
                )
            except Exception:
                pass

        # ส่งต่อให้ handler ด้วยโค้ด STT ที่แปลงแล้ว
        stt_code = _to_stt_code(ui_code)
        try:
            await self.on_choose_lang(interaction, stt_code)
        finally:
            self.stop()

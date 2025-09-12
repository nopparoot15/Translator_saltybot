import os
import asyncio
from uuid import uuid4
from typing import Optional
from collections import defaultdict

import discord
from discord.ext import commands

from gtts import gTTS

from tts_lang_resolver import (
    resolve_tts_code, normalize_gtts_lang, resolve_parts_for_tts,
    sanitize_requested_lang, normalize_parts_shape, strip_emojis_for_tts,
)

# ---- State (engines) ----
user_tts_engine = defaultdict(lambda: "gtts")   # "gtts" | "edge" (reserved)
server_tts_engine = defaultdict(lambda: "gtts")

def get_tts_engine(user_id: int, guild_id: int) -> str:
    return user_tts_engine.get(user_id) or server_tts_engine.get(guild_id) or "gtts"

# ---- Concurrency / queues ----
voice_locks = defaultdict(asyncio.Lock)
guild_speaking_locks = defaultdict(asyncio.Lock)
tts_queues = defaultdict(asyncio.Queue)
playback_generation = defaultdict(int)

async def safe_voice_connect(guild_id: int, voice_channel: discord.VoiceChannel) -> Optional[discord.VoiceClient]:
    max_retries = 2
    async with voice_locks[guild_id]:
        for attempt in range(1, max_retries + 1):
            vc = voice_channel.guild.voice_client
            if vc and vc.is_connected():
                if vc.channel == voice_channel:
                    return vc
                try:
                    await vc.disconnect(force=True)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            try:
                vc = await voice_channel.connect(timeout=10.0, reconnect=True)
                return vc
            except discord.ClientException:
                if voice_channel.guild.voice_client:
                    try:
                        await voice_channel.guild.voice_client.disconnect(force=True)
                        await asyncio.sleep(2.0)
                        continue
                    except Exception:
                        pass
                await asyncio.sleep(2.0)
            except Exception:
                await asyncio.sleep(2.0)
    return None

async def speak_text(message: discord.Message, text: str, lang: str = "auto") -> None:
    guild_id = message.guild.id
    tts_queues[guild_id].put_nowait((message, text, lang))
    async with guild_speaking_locks[guild_id]:
        while not tts_queues[guild_id].empty():
            msg, speak_text_value, speak_lang = await tts_queues[guild_id].get()
            filename = None
            try:
                if not getattr(msg.author, "voice", None):
                    continue
                voice_channel = msg.author.voice.channel
                vc = await safe_voice_connect(guild_id, voice_channel)
                if not vc: continue

                speak_text_value = strip_emojis_for_tts(speak_text_value or "").strip()
                if not speak_text_value: continue

                requested = sanitize_requested_lang(speak_lang or "auto")
                if requested == "auto":
                    requested = resolve_tts_code(speak_text_value, "auto")

                # ใช้ normalize_gtts_lang() ที่รองรับ fil/fil-PH/tl-PH → tl
                gtts_key, display_code = normalize_gtts_lang(requested)

                filename = f"tts_{uuid4().hex}.mp3"
                gTTS(text=speak_text_value, lang=gtts_key).save(filename)

                while vc.is_playing():
                    await asyncio.sleep(0.1)
                vc.play(discord.FFmpegPCMAudio(filename))
                start = asyncio.get_event_loop().time()
                while vc.is_playing():
                    if asyncio.get_event_loop().time() - start > 30:
                        vc.stop(); break
                    await asyncio.sleep(0.1)
            except Exception:
                pass
            finally:
                if filename and os.path.exists(filename):
                    try: os.remove(filename)
                    except Exception: pass

async def speak_text_multi(
    message: discord.Message,
    parts: list[tuple[str, str]],
    playback_rate: float = 1.0,
    preferred_lang: Optional[str] = None,
) -> None:
    guild_id = message.guild.id
    parts = normalize_parts_shape(parts)
    if not parts:
        return
    tts_queues[guild_id].put_nowait((message, parts, playback_rate, preferred_lang))
    if guild_speaking_locks[guild_id].locked():
        return

    async with guild_speaking_locks[guild_id]:
        while not tts_queues[guild_id].empty():
            msg, input_parts, rate, pref = await tts_queues[guild_id].get()
            if not getattr(msg.author, "voice", None):
                continue

            voice_channel = msg.author.voice.channel
            vc = await safe_voice_connect(guild_id, voice_channel)
            if not vc:
                continue

            input_parts = resolve_parts_for_tts(input_parts, preferred_lang=pref)
            merged_text = " ".join(seg for seg, _ in input_parts).strip()
            merged_text = strip_emojis_for_tts(merged_text)
            if not merged_text:
                continue

            requested_lang = sanitize_requested_lang(next((lg for _, lg in input_parts if lg), "auto"))
            if requested_lang == "auto":
                requested_lang = resolve_tts_code(merged_text, "auto")
            if pref:
                pref_sanitized = sanitize_requested_lang(pref)
                if pref_sanitized and pref_sanitized.lower() != "auto":
                    requested_lang = pref_sanitized

            # ใช้ normalize_gtts_lang() ที่รองรับ fil/fil-PH/tl-PH → tl
            gtts_key, display_code = normalize_gtts_lang(requested_lang)

            filename = f"tts_{uuid4().hex}.mp3"
            try:
                gTTS(text=merged_text, lang=gtts_key).save(filename)
                if not os.path.exists(filename) or os.path.getsize(filename) < 1000:
                    continue

                if vc.is_playing():
                    vc.stop(); await asyncio.sleep(0.1)

                if 0.5 <= rate <= 2.0 and abs(rate - 1.0) > 1e-3:
                    vc.play(discord.FFmpegPCMAudio(filename, options=f"-filter:a atempo={rate}"))
                else:
                    vc.play(discord.FFmpegPCMAudio(filename))

                start = asyncio.get_event_loop().time()
                while vc.is_playing():
                    if asyncio.get_event_loop().time() - start > 60:
                        vc.stop(); break
                    await asyncio.sleep(0.1)
            finally:
                try:
                    if os.path.exists(filename): os.remove(filename)
                except Exception:
                    pass

async def interrupt_tts(guild_id: int) -> None:
    try:
        playback_generation[guild_id] += 1
        guild = next((g for g in discord.utils.get(discord.Client().guilds, id=guild_id) or []), None)  # not used in current flow
    except Exception:
        pass

def start_empty_vc_watcher(bot: commands.Bot):
    async def _watcher():
        # loop forever
        while True:
            try:
                for guild in bot.guilds:
                    vc = guild.voice_client
                    if vc and vc.is_connected():
                        members = [m for m in vc.channel.members if not m.bot]
                        if not members:
                            await vc.disconnect()
            except Exception:
                pass
            await asyncio.sleep(10)
    # fire and forget
    bot.loop.create_task(_watcher())

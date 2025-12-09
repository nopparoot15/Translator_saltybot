import os
import asyncio
from uuid import uuid4
from typing import Optional, List, Tuple
from collections import defaultdict

import discord
from discord.ext import commands

from gtts import gTTS

from tts_lang_resolver import (
    resolve_tts_code, normalize_gtts_lang, resolve_parts_for_tts,
    sanitize_requested_lang, normalize_parts_shape, strip_emojis_for_tts,
)

import logging
logger = logging.getLogger(__name__)

# =========================
# Engine selection (extensible)
# =========================
user_tts_engine = defaultdict(lambda: "gtts")
server_tts_engine = defaultdict(lambda: "gtts")

def get_tts_engine(user_id: int, guild_id: int) -> str:
    return user_tts_engine.get(user_id) or server_tts_engine.get(guild_id) or "gtts"

# =========================
# Concurrency / queues
# =========================
voice_locks = defaultdict(asyncio.Lock)
guild_speaking_locks = defaultdict(asyncio.Lock)
tts_queues = defaultdict(asyncio.Queue)
playback_generation = defaultdict(int)

# =========================
# Helpers
# =========================
def _tmp_mp3() -> str:
    return f"tts_{uuid4().hex}.mp3"

def _chunk_text_for_gtts(text: str, max_len: int = 200) -> List[str]:
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= max_len:
        return [t]

    out: List[str] = []
    current = []
    cur_len = 0
    for token in t.split():
        if cur_len + len(token) + (1 if cur_len > 0 else 0) > max_len:
            out.append(" ".join(current))
            current = [token]
            cur_len = len(token)
        else:
            if cur_len > 0:
                current.append(token)
                cur_len += len(token) + 1
            else:
                current = [token]
                cur_len = len(token)
    if current:
        out.append(" ".join(current))
    return out

async def _safe_remove(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

async def _safe_voice_disconnect(vc: Optional[discord.VoiceClient]) -> None:
    try:
        if vc and vc.is_connected():
            await vc.disconnect(force=True)
    except Exception:
        pass

async def safe_voice_connect(guild_id: int, voice_channel: discord.VoiceChannel) -> Optional[discord.VoiceClient]:
    max_retries = 2
    async with voice_locks[guild_id]:
        for _ in range(max_retries):
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

async def _play_mp3(vc: discord.VoiceClient, path: str, rate: float = 1.0, timeout: float = 60.0) -> None:
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        return

    try:
        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.1)
        if 0.5 <= rate <= 2.0 and abs(rate - 1.0) > 1e-3:
            vc.play(discord.FFmpegPCMAudio(path, options=f"-filter:a atempo={rate}"))
        else:
            vc.play(discord.FFmpegPCMAudio(path))

        start = asyncio.get_event_loop().time()
        while vc.is_playing():
            if asyncio.get_event_loop().time() - start > timeout:
                vc.stop()
                break
            await asyncio.sleep(0.1)
    except Exception:
        pass

def _normalize_engine_lang(code: str) -> Tuple[str, str]:
    req = sanitize_requested_lang(code or "auto")
    if req == "auto":
        return "en", "en"
    gtts_key, display = normalize_gtts_lang(req)
    if gtts_key == "zh":
        gtts_key, display = "zh-CN", "zh-CN"
    return gtts_key, display

def _supported_by_gtts(lang: str) -> bool:
    likely = {
        "en","th","ja","zh-CN","zh-TW","ko","ru","de","fr","es","pt","it","tl","fil","vi","id",
        "hi","ar","km","my","pl","uk"
    }
    return lang in likely

def _pick_engine_for_lang(lang: str) -> str:
    if _supported_by_gtts(lang):
        return "gtts"
    return "gtts"

def _synthesize_gtts(text: str, lang: str) -> Optional[str]:
    try:
        filename = _tmp_mp3()
        gTTS(text=text, lang=lang).save(filename)
        if os.path.exists(filename) and os.path.getsize(filename) >= 1000:
            return filename
    except Exception:
        return None
    return None

async def _speak_text_with_lang(vc: discord.VoiceClient, text: str, lang_code: str, rate: float = 1.0) -> None:
    t = strip_emojis_for_tts(text or "").strip()
    if not t:
        return

    eng_key, eng_disp = _normalize_engine_lang(lang_code)
    engine = _pick_engine_for_lang(eng_key)
    segments = _chunk_text_for_gtts(t, max_len=200)

    if engine == "gtts":
        for seg in segments:
            lang_try = eng_key
            path = _synthesize_gtts(seg, lang_try)
            if path is None and lang_try not in ("en",):
                path = _synthesize_gtts(seg, "en")
            if path:
                try:
                    await _play_mp3(vc, path, rate=rate, timeout=60.0)
                finally:
                    await _safe_remove(path)
    else:
        for seg in segments:
            path = _synthesize_gtts(seg, "en")
            if path:
                try:
                    await _play_mp3(vc, path, rate=rate, timeout=60.0)
                finally:
                    await _safe_remove(path)

# =========================
# Public APIs
# =========================
async def speak_text(message: discord.Message, text: str, lang: str = "auto") -> None:
    if not getattr(message.author, "voice", None):
        return

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
                if not vc:
                    continue

                speak_text_value = strip_emojis_for_tts(speak_text_value or "").strip()
                if not speak_text_value:
                    continue

                requested = sanitize_requested_lang(speak_lang or "auto")
                if requested == "auto":
                    requested = resolve_tts_code(speak_text_value, "auto")

                gtts_key, display_code = _normalize_engine_lang(requested)
                await _speak_text_with_lang(vc, speak_text_value, gtts_key, rate=1.0)

            except Exception:
                pass
            finally:
                if filename:
                    await _safe_remove(filename)

async def speak_text_multi(message: discord.Message, parts: List[Tuple[str, str]], playback_rate: float = 1.0, preferred_lang: Optional[str] = None) -> None:
    if not getattr(message.author, "voice", None):
        return

    guild_id = message.guild.id
    shaped = normalize_parts_shape(parts)
    if not shaped:
        return

    tts_queues[guild_id].put_nowait((message, shaped, playback_rate, preferred_lang))
    if guild_speaking_locks[guild_id].locked():
        return

    async with guild_speaking_locks[guild_id]:
        while not tts_queues[guild_id].empty():
            msg, input_parts, rate, pref = await tts_queues[guild_id].get()
            try:
                if not getattr(msg.author, "voice", None):
                    continue

                voice_channel = msg.author.voice.channel
                vc = await safe_voice_connect(guild_id, voice_channel)
                if not vc:
                    continue

                resolved_parts = resolve_parts_for_tts(input_parts, preferred_lang=pref)

                for seg_text, seg_lang in resolved_parts:
                    seg_text = strip_emojis_for_tts(seg_text or "").strip()
                    if not seg_text:
                        continue

                    if pref:
                        pref_sanitized = sanitize_requested_lang(pref)
                        if pref_sanitized and pref_sanitized.lower() != "auto":
                            seg_lang = pref_sanitized

                    gtts_key, _ = _normalize_engine_lang(seg_lang)
                    await _speak_text_with_lang(vc, seg_text, gtts_key, rate=float(rate))

            except Exception:
                pass

async def interrupt_tts(guild_id: int) -> None:
    try:
        playback_generation[guild_id] += 1
    except Exception:
        pass

# =========================
# NEW — Empty VC Watcher (แก้ใหม่)
# =========================
_empty_vc_task = None

def start_empty_vc_watcher(bot: commands.Bot):
    """
    Watcher ออกจากห้องอัตโนมัติเมื่อไม่มี human เหลือ
    """

    global _empty_vc_task

    # กัน start ซ้ำ
    if _empty_vc_task is not None and not _empty_vc_task.done():
        logger.info("[empty_vc] watcher already running, skip start()")
        return

    async def _watcher():
        logger.info("[empty_vc] watcher started")
        while True:
            try:
                for guild in bot.guilds:
                    vc = guild.voice_client
                    if not vc or not vc.is_connected():
                        continue

                    all_members = list(vc.channel.members)
                    humans = [m for m in all_members if not m.bot]

                    logger.debug(
                        f"[empty_vc] guild={guild.id} "
                        f"channel={vc.channel.name} "
                        f"members={[f'{m} (bot={m.bot})' for m in all_members]}"
                    )

                    if not humans:
                        await vc.disconnect()
                        logger.info(
                            f"👋 Left empty voice channel '{vc.channel.name}' "
                            f"in guild '{guild.name}' (no humans left)"
                        )

            except Exception as e:
                logger.exception(f"[empty_vc] loop crashed: {e}")

            await asyncio.sleep(10)

    _empty_vc_task = bot.loop.create_task(_watcher())

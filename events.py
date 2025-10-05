import os
import logging
import re

from constants import (
    TRANSLATION_CHANNELS, DETAILED_EN_CHANNELS, DETAILED_JA_CHANNELS,
    AUTO_TTS_CHANNELS, AUDIO_EXTS, MAX_INPUT_LENGTH, MAX_APPROX_TOKENS,
)
from lang_config import LANG_NAMES, FLAGS
from translate_panel import TwoWayTranslatePanel, OCRListenTranslateView, send_transcript
from translation_service import translate_with_provider, engine_label_for_message
from messaging_utils import send_long_message

from ocr_service import ocr_google_vision_api_key
from app_redis import (
    increment_user_usage, get_channel_lang_hist, get_user_lang_hist,
    incr_channel_lang_hist, incr_user_lang_hist,
    stt_try_reserve, stt_refund, stt_get_used,
)
from media_utils import (
    ensure_stt_compatible, transcode_to_wav_pcm16,
    download_to_temp, probe_duration_seconds,
)
from stt_google_sync import stt_transcribe_bytes
from stt_google_async import transcribe_long_audio_bytes
from stt_lang_utils import (
    detect_lang_hints_from_context, pick_alternative_langs, detect_script_from_text
)
from tts_lang_resolver import (
    split_text_by_script, merge_adjacent_parts, resolve_parts_for_tts,
    is_emoji_only, safe_detect,
)
from tts_service import speak_text_multi
from config import GOOGLE_API_KEY, GCS_BUCKET_NAME, STT_DAILY_LIMIT_SECONDS, TZ
from stt_select_panel import STTLanguagePanel, _to_stt_code

logger = logging.getLogger(__name__)

# ===== Helper: จำกัดขอบเขตช่องที่บอทรับผิดชอบ =====
def _is_managed_channel(ch_id: int) -> bool:
    return (
        (ch_id in AUTO_TTS_CHANNELS)
        or (ch_id in DETAILED_EN_CHANNELS)
        or (ch_id in DETAILED_JA_CHANNELS)
        or (ch_id in TRANSLATION_CHANNELS)
    )

# --- STT helpers ---
_COMPRESSED_EXTS = {".mp3", ".m4a", ".ogg", ".opus", ".webm", ".mp4"}

def _is_compressed(name: str, content_type: str) -> bool:
    n = (name or "").lower()
    ct = (content_type or "").lower()
    return (
        any(n.endswith(ext) for ext in _COMPRESSED_EXTS)
        or ct.startswith("audio/ogg")
        or ct.startswith("audio/webm")
        or ct.startswith("audio/mpeg")
        or ct.startswith("video/mp4")
    )

def _should_force_longrun(size_bytes: int, name: str, content_type: str) -> bool:
    if _is_compressed(name, content_type):
        return size_bytes > 1_800_000
    return size_bytes > 9_000_000

def _ensure_alts_for_code_switch(base_lang_code: str, alt_iso: list[str] | None) -> list[str]:
    alts = list(alt_iso or [])
    fam = (base_lang_code or "").split("-")[0].lower()
    if fam in {"th", "km", "my"}:   # ไทย/เขมร/พม่า → ชอบปนอังกฤษ
        fams = [a.split("-")[0].lower() for a in alts]
        if "en" not in fams:
            alts = ["en"] + alts
    return alts[:3]

_TH_RE = re.compile(r'[\u0E00-\u0E7F]')
def _looks_thai(s: str) -> bool:
    return bool(_TH_RE.search(s or ""))

def register_message_handlers(bot):
    @bot.listen("on_message")
    async def _on_message(message):
        if message.author.bot:
            return
        if message.content.startswith("!"):
            return

        # ========== OCR / STT ==========
        channel_cfg = TRANSLATION_CHANNELS.get(message.channel.id)
        if channel_cfg == "multi" and message.attachments:
            valid_img_exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif")
            image_attachments = [
                a for a in message.attachments
                if (a.filename or "").lower().endswith(valid_img_exts)
                or (a.content_type or "").startswith("image/")
            ]
            audio_attachments = [
                a for a in message.attachments
                if (a.filename or "").lower().endswith(AUDIO_EXTS)
                or (a.content_type or "").startswith(("audio/", "video/"))
            ]

            # ---- OCR ----
            if image_attachments:
                for attachment in image_attachments[:1]:
                    try:
                        async with message.channel.typing():
                            image_bytes = await attachment.read()
                            await increment_user_usage(message.author.id, message.guild.id)
                            result_text = await ocr_google_vision_api_key(image_bytes, message)
                            if not result_text:
                                continue
                            if result_text.strip().startswith(("❌", "⏳")):
                                await message.channel.send(result_text)
                                continue
                            safe_text = result_text.replace("```", "``\u200b`")
                            await message.channel.send(
                                content=f"📝 Extracted text:\n```{safe_text}```",
                                view=OCRListenTranslateView(
                                    original_text=result_text,
                                    tts_fn_multi=speak_text_multi,
                                    translate_provider_fn=translate_with_provider,
                                    flags=FLAGS,
                                    engine_label_provider=engine_label_for_message,
                                ),
                                reference=message,
                                mention_author=False,
                            )
                    except Exception as e:
                        logger.exception(f"❌ OCR error: {e}")
                        await message.channel.send(f"❌ เกิดข้อผิดพลาดระหว่าง OCR: {e}")
                return

            # ---- STT ----
            if audio_attachments:
                a = audio_attachments[0]
                filename = (a.filename or "").lower()
                content_type = (a.content_type or "").lower()

                async def _run_stt_with_lang(interaction, base_lang_code: str):
                    base_lang_code_bcp = _to_stt_code(base_lang_code)
                    flag = FLAGS.get(base_lang_code_bcp, FLAGS.get(base_lang_code_bcp.split("-")[0], "")) or ""
                    progress_msg = None

                    async def _status(msg: str):
                        nonlocal progress_msg
                        try:
                            if progress_msg is None:
                                progress_msg = await message.channel.send(
                                    f"{flag} {msg} (`{base_lang_code_bcp}`)",
                                    reference=message,
                                    mention_author=False,
                                )
                            else:
                                await progress_msg.edit(content=f"{flag} {msg} (`{base_lang_code_bcp}`)")
                        except Exception:
                            pass

                    tmp_path = None
                    reserved_sec = 0
                    try:
                        # เตรียมไฟล์
                        await _status("กำลังเตรียมไฟล์เสียง…")
                        tmp_path = await download_to_temp(a)
                        dur_sec = await probe_duration_seconds(tmp_path)
                        if dur_sec <= 0: dur_sec = 60
                        reserved_sec = int(dur_sec)

                        guild_id = message.guild.id if message.guild else None
                        user_id = message.author.id
                        ok = await stt_try_reserve(user_id, guild_id, reserved_sec, STT_DAILY_LIMIT_SECONDS, TZ)
                        if not ok:
                            used = await stt_get_used(user_id, guild_id, TZ)
                            remain = max(0, STT_DAILY_LIMIT_SECONDS - int(used))
                            await _status("❌ เกินโควต้า STT วันนี้")
                            await message.channel.send(
                                f"❌ ใช้โควต้า STT หมดแล้ว — ใช้ไป {used}s / {STT_DAILY_LIMIT_SECONDS}s (เหลือ {remain}s)",
                                reference=message, mention_author=False
                            )
                            return

                        with open(tmp_path, "rb") as f:
                            raw_bytes = f.read()
                        if not raw_bytes:
                            await _status("❌ ไม่สามารถอ่านไฟล์เสียงได้")
                            await stt_refund(user_id, guild_id, reserved_sec, TZ)
                            return

                        await increment_user_usage(message.author.id, message.guild.id)

                        # เข้ากับ STT
                        audio_bytes, fn, ctype, did_trans = await ensure_stt_compatible(filename, content_type, raw_bytes)
                        filename2, content_type2 = fn, ctype

                        # alts
                        context_bias = detect_lang_hints_from_context(
                            username=str(message.author),
                            channel_name=getattr(message.channel, "name", "") or "",
                            caption_text=(message.content or ""),
                        )
                        channel_hist = await get_channel_lang_hist(message.channel.id)
                        user_hist    = await get_user_lang_hist(message.author.id)
                        iso_base = (base_lang_code_bcp or "").split("-")[0]
                        alt_iso = pick_alternative_langs(iso_base, 3, channel_hist, user_hist, context_bias)
                        alt_iso_first = _ensure_alts_for_code_switch(base_lang_code_bcp, alt_iso)

                        use_long = _should_force_longrun(len(audio_bytes), filename2, content_type2)
                        stt_mode = "google longrunning" if use_long else "google sync"
                        await _status(f"กำลังเริ่มถอดเสียง… (โหมด: {stt_mode})")

                        if use_long:
                            try:
                                audio_bytes = await transcode_to_wav_pcm16(audio_bytes, 16000, 1,
                                                                           os.path.splitext(filename2)[1], content_type2)
                                filename2 = f"{os.path.splitext(filename2)[0]}.wav"
                                content_type2 = "audio/wav"
                            except Exception:
                                pass

                        async def _run_once(alts_iso: list[str] | None):
                            alts_bcp = [_to_stt_code(c) for c in (alts_iso or [])[:3]] if alts_iso else None
                            if use_long:
                                return await transcribe_long_audio_bytes(
                                    audio_bytes=audio_bytes,
                                    file_ext=os.path.splitext(filename2)[1] or ".wav",
                                    content_type=content_type2,
                                    bucket_name=GCS_BUCKET_NAME,
                                    lang_hint=base_lang_code_bcp,
                                    alternative_language_codes=alts_bcp,
                                    poll=True,
                                    max_wait_sec=900.0,
                                    audio_channel_count=1,
                                    enable_separate_recognition_per_channel=False,
                                )
                            else:
                                return await stt_transcribe_bytes(
                                    audio_bytes=audio_bytes,
                                    api_key=GOOGLE_API_KEY,
                                    filename=a.filename,
                                    content_type=content_type2,
                                    lang_hint=base_lang_code_bcp,
                                    enable_punctuation=True,
                                    max_alternatives=1,
                                    alternative_language_codes=alts_bcp,
                                    sample_rate_hz=16000 if content_type2.startswith("audio/wav") else None,
                                    timeout_s=90.0,
                                )

                        # run1
                        await _status("กำลังถอดเสียง…")
                        text, raw = await _run_once(None)

                        # retry
                        if not (text or "").strip():
                            text2, raw2 = await _run_once(alt_iso_first)
                            if (text2 or "").strip():
                                text, raw = text2, raw2

                        if not (text or "").strip():
                            await _status("⚠️ ไม่พบข้อความจากเสียง")
                            return

                        # hist
                        try:
                            lang_seen = detect_script_from_text(text)
                            await incr_channel_lang_hist(message.channel.id, lang_seen)
                            await incr_user_lang_hist(message.author.id, lang_seen)
                        except Exception:
                            pass

                        if progress_msg:
                            try: await progress_msg.delete()
                            except: pass

                        sent_msg = await send_transcript(
                            message, text, stt_tag=stt_mode,
                            lang_display=base_lang_code_bcp,
                            show_engine=False, reply_to=message,
                        )
                        try:
                            view = OCRListenTranslateView(
                                original_text=text,
                                tts_fn_multi=speak_text_multi,
                                translate_provider_fn=translate_with_provider,
                                flags=FLAGS,
                                engine_label_provider=engine_label_for_message,
                            )
                            await sent_msg.edit(view=view)
                        except Exception:
                            pass
                    except Exception as e:
                        if progress_msg:
                            try: await progress_msg.delete()
                            except: pass
                        logger.exception(f"❌ STT error: {e}")
                        await message.channel.send("❌ เกิดข้อผิดพลาดระหว่างถอดเสียง", reference=message, mention_author=False)
                        try:
                            if reserved_sec > 0:
                                await stt_refund(user_id, guild_id, reserved_sec, TZ)
                        except: pass
                    finally:
                        if tmp_path:
                            try: os.remove(tmp_path)
                            except: pass

                panel = STTLanguagePanel(
                    source_message=message,
                    on_choose_lang=_run_stt_with_lang,
                    flags=FLAGS,
                    major_langs=["th", "en", "ja", "km", "my", "zh"],
                    major_primary="th",
                )
                await panel.attach(message.channel)
                return

        # ========== ข้อความปกติ ==========
        text = (message.content or "").strip()
        if not text:
            return

        if is_emoji_only(text):
            if _is_managed_channel(message.channel.id):
                try: await message.channel.send("ℹ️ ข้าม: มีแค่อีโมจิ")
                except: pass
            return

        if message.channel.id in AUTO_TTS_CHANNELS:
            try:
                await increment_user_usage(message.author.id, message.guild.id)
                parts = merge_adjacent_parts(split_text_by_script(text))
                await speak_text_multi(message, resolve_parts_for_tts(parts))
            except Exception as e:
                logger.error(f"❌ Auto TTS failed: {e}")
            return

        if message.channel.id in TRANSLATION_CHANNELS:
            await increment_user_usage(message.author.id, message.guild.id)

            if message.channel.id in DETAILED_EN_CHANNELS:
                if len(text) > MAX_INPUT_LENGTH:
                    await message.channel.send("❗ ข้อความยาวเกินไป")
                    return
                prompt = (
                    "วิเคราะห์ประโยคภาษาอังกฤษต่อไปนี้เป็นภาษาไทย:\n"
                    "- คำศัพท์/ไวยากรณ์\n- สรุปคำแปล\n\n"
                    f"ประโยค: {text}"
                )
                from translation_service import get_translation
                ans = await get_translation(prompt, "gpt-4o-mini")
                await send_long_message(message.channel, (ans or "").strip())
                return

            if message.channel.id in DETAILED_JA_CHANNELS:
                if len(text) > MAX_INPUT_LENGTH:
                    await message.channel.send("❗ ข้อความยาวเกินไป")
                    return
                prompt = (
                    "วิเคราะห์ประโยคภาษาญี่ปุ่นต่อไปนี้เป็นภาษาไทย:\n"
                    "- คำศัพท์/คำช่วย/ไวยากรณ์\n- ตัวอย่างใหม่\n- คำแปล\n\n"
                    f"ประโยค: {text}"
                )
                from translation_service import get_translation
                ans = await get_translation(prompt, "gpt-4o-mini")
                await send_long_message(message.channel, (ans or "").strip())
                return

            cfg = TRANSLATION_CHANNELS.get(message.channel.id)
            if cfg == "multi":
                panel = TwoWayTranslatePanel(
                    source_message=message,
                    translate_fn=translate_with_provider,
                    clean_fn=lambda s, t: t,
                    lang_names=LANG_NAMES,
                    flags=FLAGS,
                    tts_fn_multi=speak_text_multi,
                    timeout=180,
                    allow_anyone=True,
                    engine_label_provider=engine_label_for_message,
                )
                await panel.attach(message.channel)
                return
            else:
                src_lang, tgt_lang = cfg or ("", "")
                try: lang = safe_detect(text)
                except: lang = ""
                target_lang = tgt_lang if lang == src_lang else src_lang
                lang_name = LANG_NAMES.get(target_lang, "ภาษาปลายทาง")
                flag = FLAGS.get(target_lang, "")
                voice_lang = target_lang

                approx_tokens = len(text.encode("utf-8")) // 3
                if approx_tokens > MAX_APPROX_TOKENS:
                    await message.channel.send("❗ ยาวเกินไป")
                    return

                translated = (await translate_with_provider(message, text, target_lang, lang_name) or "").strip()
                if not translated or translated.lower() == text.lower():
                    return
                if translated.startswith(("❌", "⚠️")):
                    await message.channel.send(translated)
                    return
                await send_long_message(message.channel, f"{flag} {translated}")
                try:
                    vl = _to_stt_code(voice_lang)
                    await speak_text_multi(message, [(translated, vl)], playback_rate=1.0, preferred_lang=vl)
                except: pass

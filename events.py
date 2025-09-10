# events.py

import os
import logging

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
    incr_channel_lang_hist, incr_user_lang_hist
)
from media_utils import ensure_stt_compatible, transcode_to_wav_pcm16  # ⬅️ นำเข้า transcode
from stt_google_sync import stt_transcribe_bytes
from stt_google_async import transcribe_long_audio_bytes
from stt_lang_utils import (
    detect_lang_hints_from_context, pick_alternative_langs,
    choose_alts_strict_first, detect_script_from_text,
)
from tts_lang_resolver import (
    split_text_by_script, merge_adjacent_parts, resolve_parts_for_tts,
    is_emoji_only, safe_detect,
)
from tts_service import speak_text_multi
from config import GOOGLE_API_KEY, GCS_BUCKET_NAME

logger = logging.getLogger(__name__)


def register_message_handlers(bot):
    @bot.listen("on_message")
    async def _on_message(message):
        if message.author.bot:
            return

        # prefix commands → ให้ commands framework จัดการ
        if message.content.startswith("!"):
            return

        logger.info(
            f"[DEBUG] 📥 from={message.author} | channel={message.channel.id} | attachments={len(message.attachments)}"
        )

        # 2) OCR / STT เมื่ออยู่ในห้อง multi และมีแนบไฟล์
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

            # ---- (A) OCR ----
            if image_attachments:
                for attachment in image_attachments[:1]:  # กันสแปม: เอารูปแรก
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
                        logger.exception(f"❌ OCR(multi) handler error: {e}")
                        await message.channel.send(f"❌ เกิดข้อผิดพลาดระหว่าง OCR (multi): {e}")
                return

            # ---- (B) STT ----
            if audio_attachments:
                a = audio_attachments[0]
                filename = (a.filename or "").lower()
                content_type = (a.content_type or "").lower()

                try:
                    async with message.channel.typing():
                        raw_bytes = await a.read()
                        if not raw_bytes:
                            await message.channel.send("❌ ไม่สามารถอ่านไฟล์เสียงได้")
                            return

                        await increment_user_usage(message.author.id, message.guild.id)

                        # 1) ทำให้ไฟล์เข้ากับ STT (กัน 400 + ลด empty)
                        audio_bytes, filename, content_type, did_trans = await ensure_stt_compatible(
                            filename, content_type, raw_bytes
                        )

                        # 2) สร้าง alt ภาษาแบบฉลาด
                        base_lang = "th-TH"
                        context_bias = detect_lang_hints_from_context(
                            username=str(message.author),
                            channel_name=getattr(message.channel, "name", "") or "",
                            caption_text=(message.content or ""),
                        )
                        channel_hist = await get_channel_lang_hist(message.channel.id)
                        user_hist = await get_user_lang_hist(message.author.id)

                        alt_smart = pick_alternative_langs(
                            base_lang=base_lang, max_alts=3,
                            channel_hist=channel_hist, user_hist=user_hist,
                            context_bias=context_bias,
                        )
                        # ใช้ alt ทันที ไม่ strict-first เพื่อลดเคสล็อกไทย
                        alt_round1, alt_round2 = choose_alts_strict_first(
                            base_lang=base_lang,
                            alt_smart=alt_smart,
                            force_strict_if_confident=False,
                            context_bias=context_bias,
                            strict_confidence_threshold=2.0,
                            exclude_in_fallback=None,
                            per_round_limit=3,
                        )

                        # 3) เลือกโหมด
                        use_long = len(audio_bytes) > 9_000_000
                        stt_mode = "google longrunning" if use_long else "google sync"

                        # ✅ สำคัญ: ถ้าใช้ longrunning → บังคับ mono 16k เสมอ (กัน 400 stereo)
                        if use_long:
                            try:
                                audio_bytes = await transcode_to_wav_pcm16(
                                    audio_bytes,
                                    rate=16000,
                                    ch=1,
                                    src_ext=os.path.splitext(filename)[1] or ".bin",
                                    content_type=content_type or ""
                                )
                                filename = os.path.splitext(filename)[0] + ".wav"
                                content_type = "audio/wav"
                                did_trans = True
                            except Exception as e:
                                logger.warning(f"[STT] force-mono(long) failed: {e}")

                        async def _run_stt_once(alt_list):
                            if use_long:
                                lr_kwargs = dict(
                                    audio_bytes=audio_bytes,
                                    file_ext=os.path.splitext(filename)[1] or ".wav",
                                    content_type=content_type or None,
                                    bucket_name=GCS_BUCKET_NAME,
                                    lang_hint=base_lang,
                                    alternative_language_codes=(alt_list or [])[:3],
                                    poll=True,
                                    max_wait_sec=900.0,
                                )
                                # แจ้ง audio_channel_count=1 เมื่อเราเพิ่งบังคับ mono
                                lr_kwargs.update(
                                    audio_channel_count=1,
                                    enable_separate_recognition_per_channel=False
                                )
                                return await transcribe_long_audio_bytes(**lr_kwargs)
                            else:
                                sync_kwargs = dict(
                                    audio_bytes=audio_bytes,
                                    api_key=GOOGLE_API_KEY,
                                    filename=a.filename,
                                    content_type=content_type,
                                    lang_hint=base_lang,
                                    alternative_language_codes=(alt_list or [])[:3],
                                    enable_punctuation=True,
                                    max_alternatives=1,
                                    timeout_s=90.0,
                                )
                                # synchronous: เติม sample_rate_hz ให้เหมาะ
                                if content_type.startswith("audio/wav") or filename.endswith(".wav"):
                                    sync_kwargs.update(
                                        sample_rate_hz=16000,
                                        audio_channel_count=1,
                                        enable_separate_recognition_per_channel=False
                                    )
                                elif filename.endswith((".ogg", ".opus")) or "opus" in content_type:
                                    sync_kwargs.update(sample_rate_hz=48000)
                                else:
                                    sync_kwargs.update(
                                        audio_channel_count=1,
                                        enable_separate_recognition_per_channel=False
                                    )
                                return await stt_transcribe_bytes(**sync_kwargs)

                        # ---- Attempt 1 (alts รอบแรก) ----
                        text, raw = await _run_stt_once(alt_round1)

                        # error ฝั่ง API
                        if text.startswith("❌") or (isinstance(raw, dict) and raw.get("error")):
                            err_preview = ""
                            if isinstance(raw, dict):
                                try:
                                    err_preview = (raw.get("error") or "")[:400]
                                except Exception:
                                    pass
                            await message.channel.send(f"{text}\n{err_preview}" if err_preview else text)
                            return

                        # ต้องลองรอบสองไหม?
                        if not text.strip():
                            text2, raw2 = await _run_stt_once(alt_round2)
                            if text2.strip():
                                text, raw = text2, raw2

                        # ---- second chance: transcode→wav แล้วลองใหม่ (กรณีแรกไม่สำเร็จและเรายังไม่ได้แปลง) ----
                        if (not text.strip()) and (not did_trans):
                            try:
                                audio_bytes2, filename2, content_type2, _ = await ensure_stt_compatible(
                                    a.filename or "", a.content_type or "", raw_bytes
                                )
                                # บังคับ mono สำหรับ long อีกครั้งถ้าจำเป็น
                                if len(audio_bytes2) > 9_000_000:
                                    audio_bytes2 = await transcode_to_wav_pcm16(
                                        audio_bytes2, rate=16000, ch=1,
                                        src_ext=os.path.splitext(filename2)[1] or ".bin",
                                        content_type=content_type2 or ""
                                    )
                                    filename2 = os.path.splitext(filename2)[0] + ".wav"
                                    content_type2 = "audio/wav"

                                audio_bytes = audio_bytes2
                                filename = filename2
                                content_type = content_type2

                                use_long = len(audio_bytes) > 9_000_000
                                stt_mode = "google longrunning" if use_long else "google sync"

                                t3, r3 = await _run_stt_once(alt_round1)
                                if not t3.strip():
                                    t4, r4 = await _run_stt_once(alt_round2)
                                    text, raw = (t4, r4) if t4.strip() else (t3, r3)
                                else:
                                    text, raw = t3, r3
                            except Exception as e:
                                logger.warning(f"[STT] second-chance failed: {e}")

                        if not text.strip():
                            await message.channel.send("⚠️ ไม่พบข้อความจากเสียง (หรือเสียงไม่ชัดพอ)")
                            return

                        # อัปเดตสถิติภาษา (เรียนรู้)
                        try:
                            lang_seen = detect_script_from_text(text)
                            await incr_channel_lang_hist(message.channel.id, lang_seen)
                            await incr_user_lang_hist(message.author.id, lang_seen)
                        except Exception:
                            pass

                        # ส่ง Transcript + ปุ่ม
                        sent_msg = await send_transcript(
                            message, text,
                            engine_label_provider=engine_label_for_message,
                            stt_tag=stt_mode,
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
                    logger.exception(f"❌ STT(multi) handler error: {e}")
                    await message.channel.send("❌ เกิดข้อผิดพลาดระหว่างถอดเสียง")
                return

        # 3) ข้อความล้วน → ออกถ้าไม่มีข้อความ
        text = (message.content or "").strip()
        if not text:
            return

        # 4) emoji-only guard
        if is_emoji_only(text):
            try:
                await message.channel.send("ℹ️ ข้ามการแปล/อ่านออกเสียง: ข้อความมีแค่อีโมจิอย่างเดียว")
            except Exception:
                pass
            return

        # 5) Auto TTS
        if message.channel.id in AUTO_TTS_CHANNELS:
            try:
                await increment_user_usage(message.author.id, message.guild.id)
                parts = merge_adjacent_parts(split_text_by_script(text))
                cleaned_parts = resolve_parts_for_tts(parts)
                await speak_text_multi(message, cleaned_parts)
            except Exception as e:
                logger.error(f"❌ Auto TTS multi-lang failed: {e}")
            return

        # 6) Translation
        if message.channel.id in TRANSLATION_CHANNELS:
            await increment_user_usage(message.author.id, message.guild.id)

            # DETAILED EN
            if message.channel.id in DETAILED_EN_CHANNELS:
                if len(text) > MAX_INPUT_LENGTH:
                    await message.channel.send("❗ ข้อความยาวเกินไปสำหรับการวิเคราะห์แบบละเอียด กรุณาส่งประโยคสั้นลง")
                    return
                prompt = (
                    "วิเคราะห์ประโยคภาษาอังกฤษต่อไปนี้เป็นภาษาไทย โดยอธิบายให้เข้าใจง่าย:\n"
                    "- คำศัพท์: ชนิดคำ ความหมาย ตัวอย่าง\n"
                    "- ไวยากรณ์: tense โครงสร้าง คำเชื่อม\n"
                    "- สรุป: คำแปลไทยอย่างเป็นธรรมชาติของประโยคทั้งหมด\n\n"
                    f"ประโยค: {text}"
                )
                from translation_service import get_translation
                ans = await get_translation(prompt, "gpt-4o-mini")
                await send_long_message(message.channel, (ans or "").strip())
                return

            # DETAILED JA
            if message.channel.id in DETAILED_JA_CHANNELS:
                if len(text) > MAX_INPUT_LENGTH:
                    await message.channel.send("❗ ข้อความยาวเกินไปสำหรับการวิเคราะห์แบบละเอียด กรุณาส่งประโยคสั้นลง")
                    return
                prompt = (
                    "วิเคราะห์ประโยคภาษาญี่ปุ่นต่อไปนี้เป็นภาษาไทย แบบกระชับ:\n"
                    "- คำศัพท์: Kanji/Hiragana/Romaji/ความหมาย/ชนิดคำ\n"
                    "- คำช่วย: หน้าที่\n"
                    "- ไวยากรณ์: โครงสร้างหลัก/tense/ความหมายตามบริบท\n"
                    "- ตัวอย่างใหม่ 1 ประโยค พร้อมคำแปลไทย\n"
                    "- สรุป: ต้นฉบับ/คำอ่าน(Hira+Romaji)/คำแปลไทย\n\n"
                    f"ประโยค: {text}"
                )
                from translation_service import get_translation
                ans = await get_translation(prompt, "gpt-4o-mini")
                await send_long_message(message.channel, (ans or "").strip())
                return

            # NORMAL & MULTI
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
                try:
                    lang = safe_detect(text)
                except Exception:
                    lang = ""
                target_lang = tgt_lang if lang == src_lang else src_lang
                lang_name = LANG_NAMES.get(target_lang, "ภาษาปลายทาง")
                flag = FLAGS.get(target_lang, "")
                voice_lang = target_lang

                approx_tokens = len((text or "").encode("utf-8")) // 3
                if approx_tokens > MAX_APPROX_TOKENS:
                    await message.channel.send("❗ ข้อความหรือคำอธิบายยาวเกินไป ไม่สามารถแปลได้")
                    return

                translated = await translate_with_provider(message, text, target_lang, lang_name)
                translated = (translated or "").strip()

                if not translated:
                    await message.channel.send("⚠️ แปลไม่สำเร็จ")
                    return
                if translated.lower() == text.strip().lower():
                    return
                if translated.startswith(("❌", "⚠️")):
                    await message.channel.send(translated)
                    return

                await send_long_message(message.channel, f"{flag} {translated}")

                try:
                    vl_norm = voice_lang.lower().replace("_", "-")
                    vl = "zh-CN" if vl_norm in {"zh", "zh-cn"} else voice_lang
                    await speak_text_multi(message, [(translated, vl)], playback_rate=1.0, preferred_lang=vl)
                except Exception:
                    pass

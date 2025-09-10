# stt_google_sync.py
# ------------------------------------------------------------
# Google Speech-to-Text (Synchronous)
# ใช้กับไฟล์ "สั้น/ขนาดไม่ใหญ่มาก" (≤ ~1 นาที หรือ ≤ ~8-9MB)
# ถ้าไฟล์ยาว/ใหญ่ แนะนำใช้ long-running ใน stt_google_async.py
#
# Public APIs:
#   - stt_transcribe_bytes(...) -> (text, raw_json)
#   - stt_transcribe_file(path, ...) -> (text, raw_json)
# ------------------------------------------------------------

from __future__ import annotations

import base64
import os
from typing import Optional, Tuple, Dict, Any, List
import httpx

# ---------- Helpers ----------
def _guess_mime_by_ext(filename: Optional[str], content_type: Optional[str]) -> str:
    """เดา MIME type จากนามสกุลไฟล์ ถ้า caller ไม่ได้ส่ง content_type มาด้วย"""
    if content_type:
        return content_type
    name = (filename or "").lower()
    if name.endswith(".wav"):  return "audio/wav"
    if name.endswith(".flac"): return "audio/flac"
    if name.endswith(".mp3"):  return "audio/mpeg"
    if name.endswith(".m4a"):  return "audio/mp4"      # บางระบบรายงานเป็น audio/mp4
    if name.endswith(".aac"):  return "audio/aac"
    if name.endswith(".ogg") or name.endswith(".opus"): return "audio/ogg"
    if name.endswith(".webm"): return "audio/webm"
    if name.endswith(".mp4"):  return "video/mp4"      # เผื่ออัพเป็น mp4 ที่มีแค่เสียง
    return "application/octet-stream"

def _mime_to_encoding(mime: str, filename: Optional[str]) -> str:
    """
    Map MIME/extension → Speech-to-Text RecognitionConfig.encoding
    https://cloud.google.com/speech-to-text/docs/reference/rest/v1/RecognitionConfig#AudioEncoding
    """
    m = (mime or "").lower()
    name = (filename or "").lower()
    # OPUS in containers first
    if "webm" in m or name.endswith(".webm"):
        return "WEBM_OPUS"
    if "ogg" in m or name.endswith(".ogg") or name.endswith(".opus"):
        return "OGG_OPUS"
    if "mpeg" in m or name.endswith(".mp3"):
        return "MP3"
    if "flac" in m or name.endswith(".flac"):
        return "FLAC"
    if "wav" in m or name.endswith(".wav"):
        return "LINEAR16"  # PCM ใน .wav
    # AAC/MP4 อาจถอดไม่ตรง → แนะนำ transcode เป็น WAV ถ้าเจอปัญหา
    return "ENCODING_UNSPECIFIED"

def _build_config(
    *,
    language_code: str,
    enable_punctuation: bool,
    max_alternatives: int,
    diarization_speaker_count: Optional[int],
    profanity_filter: Optional[bool],
    audio_channel_count: Optional[int],
    enable_separate_recognition_per_channel: Optional[bool],
    model: Optional[str],
    use_enhanced: Optional[bool],
    encoding: str,
    alternative_language_codes: Optional[List[str]] = None,
    sample_rate_hz: Optional[int] = None,
) -> Dict[str, Any]:
    """สร้าง RecognitionConfig สำหรับ REST /speech:recognize"""
    cfg: Dict[str, Any] = {
        "languageCode": language_code,                         # ต้องมีเสมอ
        "enableAutomaticPunctuation": bool(enable_punctuation),
        "maxAlternatives": int(max_alternatives),
        "encoding": encoding,                                  # ใส่ให้ชัด
    }
    if alternative_language_codes:
        cfg["alternativeLanguageCodes"] = alternative_language_codes
    if sample_rate_hz:
        cfg["sampleRateHertz"] = int(sample_rate_hz)           # กรณี Opus ต้องกำหนด (เช่น 48000)
    if diarization_speaker_count:
        cfg["diarizationConfig"] = {
            "enableSpeakerDiarization": True,
            "minSpeakerCount": max(1, diarization_speaker_count),
            "maxSpeakerCount": max(1, diarization_speaker_count),
        }
    if profanity_filter is not None:
        cfg["profanityFilter"] = bool(profanity_filter)
    if audio_channel_count:
        cfg["audioChannelCount"] = int(audio_channel_count)
    if enable_separate_recognition_per_channel is not None:
        cfg["enableSeparateRecognitionPerChannel"] = bool(enable_separate_recognition_per_channel)
    if model:
        cfg["model"] = model
    if use_enhanced is not None:
        cfg["useEnhanced"] = bool(use_enhanced)
    # หมายเหตุ: sampleRateHertz ต้อง "ตรงกับไฟล์จริง" เท่านั้น ถ้าไม่ชัวร์อย่าใส่
    return cfg

# ---------- Public APIs ----------
async def stt_transcribe_bytes(
    audio_bytes: bytes,
    *,
    api_key: Optional[str] = None,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    # การตั้งค่าทั่วไป
    lang_hint: Optional[str] = None,                 # เช่น "th-TH", "en-US"
    enable_punctuation: bool = True,
    max_alternatives: int = 1,
    # ตัวเลือกเสริม
    diarization_speaker_count: Optional[int] = None,
    profanity_filter: Optional[bool] = None,
    audio_channel_count: Optional[int] = None,
    enable_separate_recognition_per_channel: Optional[bool] = None,
    model: Optional[str] = None,
    use_enhanced: Optional[bool] = None,
    alternative_language_codes: Optional[List[str]] = None,
    sample_rate_hz: Optional[int] = None,            # ⭐ เพิ่ม: กำหนด sample rate เมื่อจำเป็น (Opus)
    timeout_s: float = 120.0,
) -> Tuple[str, Dict[str, Any]]:
    """
    ถอดเสียงแบบ synchronous ผ่าน REST API
    แนะนำใช้กับไฟล์สั้น หรือขนาดไม่ใหญ่มาก (≤ ~1 นาที หรือ ≤ ~8-9MB)
    Return: (text, raw_json_response)
    """
    key = api_key or os.getenv("GOOGLE_API_KEY", "")
    if not key:
        return "❌ Missing GOOGLE_API_KEY", {}

    # กำหนด MIME/encoding ให้ตรงกับไฟล์จริง
    mime = _guess_mime_by_ext(filename, content_type)
    encoding = _mime_to_encoding(mime, filename)

    # สำหรับ Opus (OGG/WEBM) Google ต้องการ sampleRateHertz ชัดเจน → 48000
    if sample_rate_hz is None and encoding in ("OGG_OPUS", "WEBM_OPUS"):
        sample_rate_hz = 48000

    # ภาษา: ถ้า caller ไม่ส่งมา ให้ default เป็นไทย
    language_code = (lang_hint or "th-TH").strip()

    # Base64 audio
    b64 = base64.b64encode(audio_bytes).decode("utf-8")

    config = _build_config(
        language_code=language_code,
        enable_punctuation=enable_punctuation,
        max_alternatives=max_alternatives,
        diarization_speaker_count=diarization_speaker_count,
        profanity_filter=profanity_filter,
        audio_channel_count=audio_channel_count,
        enable_separate_recognition_per_channel=enable_separate_recognition_per_channel,
        model=model,
        use_enhanced=use_enhanced,
        encoding=encoding,
        alternative_language_codes=alternative_language_codes,
        sample_rate_hz=sample_rate_hz,
    )

    payload: Dict[str, Any] = {"config": config, "audio": {"content": b64}}
    url = f"https://speech.googleapis.com/v1/speech:recognize?key={key}"
    timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json; charset=utf-8"})
            if resp.status_code != 200:
                return f"❌ STT HTTP {resp.status_code}", {"error": resp.text}
            data = resp.json()
    except httpx.TimeoutException:
        return "⏳ STT timeout", {}
    except Exception as e:
        return f"❌ STT request error: {type(e).__name__}: {e}", {}

    # รวมข้อความจาก alternatives
    results = data.get("results", []) if isinstance(data, dict) else []
    text = " ".join(
        alt.get("transcript", "").strip()
        for res in results
        for alt in (res.get("alternatives") or [])
        if alt.get("transcript")
    ).strip()

    return text, data

async def stt_transcribe_file(
    path: str,
    *,
    api_key: Optional[str] = None,
    lang_hint: Optional[str] = None,
    enable_punctuation: bool = True,
    max_alternatives: int = 1,
    diarization_speaker_count: Optional[int] = None,
    profanity_filter: Optional[bool] = None,
    audio_channel_count: Optional[int] = None,
    enable_separate_recognition_per_channel: Optional[bool] = None,
    model: Optional[str] = None,
    use_enhanced: Optional[bool] = None,
    alternative_language_codes: Optional[List[str]] = None,
    sample_rate_hz: Optional[int] = None,            # ⭐ รองรับ parameter เดียวกัน
    timeout_s: float = 120.0,
) -> Tuple[str, Dict[str, Any]]:
    """Wrapper อ่านไฟล์จากดิสก์แล้วเรียก stt_transcribe_bytes"""
    try:
        with open(path, "rb") as f:
            audio_bytes = f.read()
    except Exception as e:
        return f"❌ Cannot read file: {e}", {}

    filename = os.path.basename(path)
    content_type = _guess_mime_by_ext(filename, None)

    # ถ้าเป็น Opus และ caller ไม่ส่ง sample_rate_hz มา → ตั้ง 48000
    if sample_rate_hz is None:
        enc = _mime_to_encoding(content_type, filename)
        if enc in ("OGG_OPUS", "WEBM_OPUS"):
            sample_rate_hz = 48000

    return await stt_transcribe_bytes(
        audio_bytes,
        api_key=api_key,
        filename=filename,
        content_type=content_type,
        lang_hint=lang_hint,
        enable_punctuation=enable_punctuation,
        max_alternatives=max_alternatives,
        diarization_speaker_count=diarization_speaker_count,
        profanity_filter=profanity_filter,
        audio_channel_count=audio_channel_count,
        enable_separate_recognition_per_channel=enable_separate_recognition_per_channel,
        model=model,
        use_enhanced=use_enhanced,
        alternative_language_codes=alternative_language_codes,
        sample_rate_hz=sample_rate_hz,
        timeout_s=timeout_s,
    )

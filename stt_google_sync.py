from __future__ import annotations

import base64
import os
from typing import Optional, Tuple, Dict, Any, List
import httpx

# ✅ ใช้ long-running อัตโนมัติเมื่อ sync ใช้ไม่ได้
from stt_google_async import transcribe_long_audio_bytes as _stt_longrun

# ---------- Helpers ----------
def _guess_mime_by_ext(filename: Optional[str], content_type: Optional[str]) -> str:
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

# ---------- Language normalization ----------
# แปลงโค้ดสั้น/alias → BCP-47 ที่ Google STT ชอบ
# (คงปล่อยผ่านถ้า caller ส่ง BCP-47 ที่ถูกต้องมาแล้ว)
_LANG_MAP_BASE_TO_BCP = {
    # เอเชียตะวันออก/ตะวันออกเฉียงใต้
    "th": "th-TH",
    "en": "en-US",
    "ja": "ja-JP",
    "zh": "cmn-Hans-CN",   # จีนกลางตัวง่าย (ค่าเริ่มต้น); ไต้หวันใช้ "cmn-Hant-TW"
    "ko": "ko-KR",
    "vi": "vi-VN",
    "id": "id-ID",
    "tl": "tl-PH",
    "fil": "fil-PH",
    "km": "km-KH",
    "my": "my-MM",
    # เอเชียใต้/ตะวันออกกลาง
    "hi": "hi-IN",
    "ar": "ar-SA",         # ถ้าต้องอียิปต์: "ar-EG"
    # ยุโรป
    "ru": "ru-RU",
    "uk": "uk-UA",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "it": "it-IT",
    "pt": "pt-PT",
    "pl": "pl-PL",
    # กวางตุ้ง (ผู้ใช้บางเคสอยากบังคับ)
    "yue": "yue-Hant-HK",
    # จีนตัวเต็มแบบค่าเริ่มต้น
    "zh-tw": "cmn-Hant-TW",
    "zh_tw": "cmn-Hant-TW",
    "zh-cn": "cmn-Hans-CN",
    "zh_cn": "cmn-Hans-CN",
}

def _norm_lang(code: Optional[str]) -> Optional[str]:
    """ทำให้ languageCode เป็น BCP-47 ที่ Google STT ชอบ"""
    if not code:
        return None
    c = code.strip()
    low = c.lower().replace("_", "-")
    base = low.split("-")[0]
    # ถ้า caller ส่ง BCP-47 ที่ถูกต้องอยู่แล้ว ปล่อยผ่าน
    if "-" in low and base not in _LANG_MAP_BASE_TO_BCP:
        return c
    # map base/alias → BCP-47
    mapped = _LANG_MAP_BASE_TO_BCP.get(low) or _LANG_MAP_BASE_TO_BCP.get(base)
    return mapped or c

def _norm_alt_codes(codes: Optional[List[str]]) -> Optional[List[str]]:
    """normalize รายการ alt ให้เป็น BCP-47 ทั้งหมด และลบซ้ำ"""
    if not codes:
        return None
    out: List[str] = []
    seen = set()
    for x in codes:
        m = _norm_lang(x)
        if not m:
            continue
        key = m.lower()
        if key not in seen:
            out.append(m)
            seen.add(key)
    return out or None

def _guess_ext(filename: Optional[str], mime: str) -> str:
    """เดา .ext สำหรับส่งให้ long-running"""
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext:
            return ext
    m = (mime or "").lower()
    if "mpeg" in m: return ".mp3"
    if "ogg" in m:  return ".ogg"
    if "webm" in m: return ".webm"
    if "wav" in m:  return ".wav"
    if "flac" in m: return ".flac"
    if "mp4" in m:  return ".m4a"
    return ".wav"

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

def _resolve_bucket(name: Optional[str]) -> Optional[str]:
    """คืนชื่อบัคเก็ต: ใช้ parameter ก่อน, ถ้าไม่มีลอง ENV"""
    return name or os.getenv("GCS_BUCKET_NAME") or os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET")

def _should_force_longrun(encoding: str, size_bytes: int) -> bool:
    """
    ถ้าเป็นไฟล์บีบอัด (MP3/OGG/WEBM/M4A) และขนาดเกิน ~1.8MB
    มีโอกาสสูงว่าเกิน 1 นาที → บังคับใช้ long-running เลย
    """
    if encoding in ("MP3", "OGG_OPUS", "WEBM_OPUS", "ENCODING_UNSPECIFIED"):
        return size_bytes > 1_800_000
    # WAV/FLAC ไม่บีบอัด ใช้เกณฑ์เดิม
    return size_bytes > 9_000_000

# ---------- Public APIs ----------
async def stt_transcribe_bytes(
    audio_bytes: bytes,
    *,
    api_key: Optional[str] = None,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    # การตั้งค่าทั่วไป
    lang_hint: Optional[str] = None,                 # เช่น "th-TH", "en-US", "km-KH"
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
    sample_rate_hz: Optional[int] = None,            # ⭐ กำหนด sample rate เมื่อจำเป็น (Opus)
    timeout_s: float = 120.0,
    # ⭐ ให้ระบุ bucket เพื่อ fallback อัตโนมัติไป long-running เมื่อ sync ใช้ไม่ได้
    fallback_async_bucket_name: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    ถอดเสียงแบบ synchronous ผ่าน REST API
    แนะนำใช้กับไฟล์สั้น หรือขนาดไม่ใหญ่มาก (≤ ~1 นาที หรือ ≤ ~8-9MB)
    Return: (text, raw_json_response). ถ้า sync ใช้ไม่ได้:
      - จะลองอ่านชื่อบัคเก็ตจาก fallback_async_bucket_name หรือ ENV แล้ว fallback ไป long-running อัตโนมัติ
    """
    key = api_key or os.getenv("GOOGLE_API_KEY", "")
    if not key:
        return "❌ Missing GOOGLE_API_KEY", {}

    bucket = _resolve_bucket(fallback_async_bucket_name)

    # ⛔ sync เหมาะกับไฟล์ไม่เกิน ~9MB (base64 แล้วจะพองอีก)
    if len(audio_bytes or b"") > 9_000_000:
        if bucket:
            mime = _guess_mime_by_ext(filename, content_type)
            ext = _guess_ext(filename, mime)
            language_code = _norm_lang(lang_hint) or "th-TH"
            alt_codes = _norm_alt_codes(alternative_language_codes)
            # เติม en-US ให้อัตโนมัติกับภาษาในภูมิภาคที่ปนอังกฤษบ่อย (ไทย/เขมร/พม่า)
            if language_code.split("-")[0] in {"th", "km", "my"} and (not alt_codes or "en-US" not in [a for a in alt_codes]):
                alt_codes = ["en-US"] + (alt_codes or [])
            text, raw = await _stt_longrun(
                audio_bytes,
                file_ext=ext,
                content_type=mime,
                bucket_name=bucket,
                lang_hint=language_code,
                alternative_language_codes=alt_codes,
            )
            return text, raw
        return "❌ Audio too large for synchronous STT (use long-running)", {"hint": "set GCS_BUCKET_NAME env or pass fallback_async_bucket_name"}

    mime = _guess_mime_by_ext(filename, content_type)
    encoding = _mime_to_encoding(mime, filename)

    # สำหรับ Opus (OGG/WEBM) Google ต้องการ sampleRateHertz ชัดเจน → 48000
    if sample_rate_hz is None and encoding in ("OGG_OPUS", "WEBM_OPUS"):
        sample_rate_hz = 48000

    # ภาษา: ถ้า caller ไม่ส่งมา ให้ default เป็นไทย และ normalize ให้ Google ชอบ
    language_code = _norm_lang(lang_hint) or "th-TH"
    alt_codes = _norm_alt_codes(alternative_language_codes)

    # ⭐ บังคับไป long-running เลยถ้าไฟล์บีบอัดและน่าจะยาวเกิน 1 นาที
    if bucket and _should_force_longrun(encoding, len(audio_bytes or b"")):
        ext = _guess_ext(filename, mime)
        if language_code.split("-")[0] in {"th", "km", "my"} and (not alt_codes or "en-US" not in [a for a in alt_codes]):
            alt_codes = ["en-US"] + (alt_codes or [])
        text, raw = await _stt_longrun(
            audio_bytes,
            file_ext=ext,
            content_type=mime,
            bucket_name=bucket,
            lang_hint=language_code,
            alternative_language_codes=alt_codes,
        )
        return text, raw

    # Base64 audio (ไปทาง sync)
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
        alternative_language_codes=alt_codes,
        sample_rate_hz=sample_rate_hz,
    )

    payload: Dict[str, Any] = {"config": config, "audio": {"content": b64}}
    url = f"https://speech.googleapis.com/v1/speech:recognize?key={key}"
    timeout = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers={"Content-Type": "application/json; charset=utf-8"})
        if resp.status_code != 200:
            # ถ้าเป็นเคสยาวเกิน ให้ fallback ไป long-running อัตโนมัติ (ใช้ ENV ถ้าไม่ได้ส่งพารามิเตอร์)
            body = resp.text or ""
            if bucket and resp.status_code == 400 and "Sync input too long" in body:
                ext = _guess_ext(filename, mime)
                if language_code.split("-")[0] in {"th", "km", "my"} and (not alt_codes or "en-US" not in [a for a in alt_codes]):
                    alt_codes = ["en-US"] + (alt_codes or [])
                text, raw = await _stt_longrun(
                    audio_bytes,
                    file_ext=ext,
                    content_type=mime,
                    bucket_name=bucket,
                    lang_hint=language_code,
                    alternative_language_codes=alt_codes,
                )
                return text, raw
            return f"❌ STT HTTP {resp.status_code}", {"error": body, "config": config}
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
    fallback_async_bucket_name: Optional[str] = None, # ⭐ auto fallback (จะอ่าน ENV ถ้าไม่ได้ส่ง)
) -> Tuple[str, Dict[str, Any]]:
    """Wrapper อ่านไฟล์จากดิสก์แล้วเรียก stt_transcribe_bytes"""
    try:
        with open(path, "rb") as f:
            audio_bytes = f.read()
    except Exception as e:
        return f"❌ Cannot read file: {e}", {}

    filename = os.path.basename(path)
    content_type = _guess_mime_by_ext(filename, None)

    if sample_rate_hz is None:
        enc = _mime_to_encoding(content_type, filename)
        if enc in ("OGG_OPUS", "WEBM_OPUS"):
            sample_rate_hz = 48000

    # อ่านบัคเก็ตจาก param หรือ ENV
    bucket = _resolve_bucket(fallback_async_bucket_name)

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
        fallback_async_bucket_name=bucket,  # 👈 ส่งต่อ (ถ้า None จะอ่านจาก ENV ในตัวฟังก์ชัน)
    )

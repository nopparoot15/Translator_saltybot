# stt_google_async.py
# ------------------------------------------------------------
# Google Speech-to-Text (Long Running) for LONG audio (e.g. 10+ minutes)
# Flow:
#   1) Upload bytes -> GCS object (using service account access token)
#   2) Call speech:longrunningrecognize with gs:// URI (OAuth Bearer)
#   3) Poll operation until done, return (transcript, raw_json)
#   4) (optional) delete GCS object immediately or after a delay
# ------------------------------------------------------------

from __future__ import annotations

import os
import uuid
import asyncio
import logging
from urllib.parse import quote
from typing import Optional, Tuple, Dict, Any, List

import httpx
import google.auth
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

# ---------- Helpers ----------

def _guess_mime_by_ext(ext: str) -> str:
    ext = (ext or "").lower()
    if ext.endswith(".wav"):  return "audio/wav"
    if ext.endswith(".flac"): return "audio/flac"
    if ext.endswith(".mp3"):  return "audio/mpeg"
    if ext.endswith(".m4a"):  return "audio/mp4"
    if ext.endswith(".aac"):  return "audio/aac"
    if ext.endswith(".ogg"):  return "audio/ogg"
    if ext.endswith(".opus"): return "audio/ogg"
    if ext.endswith(".webm"): return "audio/webm"
    if ext.endswith(".mp4"):  return "video/mp4"
    return "application/octet-stream"

def _mime_to_encoding(mime: str, file_ext: Optional[str]) -> str:
    """
    Map MIME/extension -> Speech-to-Text RecognitionConfig.encoding
    https://cloud.google.com/speech-to-text/docs/reference/rest/v1/RecognitionConfig#AudioEncoding
    """
    m = (mime or "").lower()
    name = (file_ext or "").lower()

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
        return "LINEAR16"

    # AAC/MP4 มักถอดไม่ตรง ให้ปล่อยเดา (หรือควร transcode เป็น wav ก่อน)
    return "ENCODING_UNSPECIFIED"

def _norm_lang(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    mapping = {
        "th": "th-TH",
        "en": "en-US",
        "ja": "ja-JP",
        "zh": "cmn-Hans-CN",
        "ko": "ko-KR",
        "vi": "vi-VN",
        "ru": "ru-RU",
        "fr": "fr-FR",
        "de": "de-DE",
        "es": "es-ES",
        "it": "it-IT",
        "pt": "pt-PT",
        "pl": "pl-PL",
        "uk": "uk-UA",
        "ar": "ar-EG",
        "hi": "hi-IN",
    }
    base = code.strip().lower().split("-")[0]
    return mapping.get(base, code)

async def _get_access_token(scope: str = "https://www.googleapis.com/auth/cloud-platform") -> str:
    creds, _ = google.auth.default(scopes=[scope])
    if not creds.valid:
        request = Request()
        creds.refresh(request)
    return creds.token

# ---------- GCS Upload/Delete ----------

async def _gcs_simple_upload(
    *,
    bucket: str,
    obj_name: str,
    content: bytes,
    content_type: Optional[str] = None,
) -> Dict[str, Any]:
    if not content_type:
        content_type = "application/octet-stream"

    token = await _get_access_token("https://www.googleapis.com/auth/devstorage.read_write")
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?uploadType=media&name={obj_name}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.post(url, headers=headers, content=content)
        r.raise_for_status()
        return r.json()

async def _gcs_delete_object(bucket: str, obj_name: str) -> None:
    """ลบ object ออกจากบัคเก็ตทันที"""
    token = await _get_access_token("https://www.googleapis.com/auth/devstorage.read_write")
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(obj_name, safe='')}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        r = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()

async def _delete_later(bucket: str, obj_name: str, delay_s: int) -> None:
    """หน่วงเวลาลบ object (best-effort; ถ้าโปรเซสดับก่อนถึงเวลา งานนี้จะไม่รัน)"""
    try:
        await asyncio.sleep(max(0, int(delay_s)))
        await _gcs_delete_object(bucket, obj_name)
        logger.info(f"🗑️ Deleted GCS object gs://{bucket}/{obj_name}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to delete gs://{bucket}/{obj_name}: {type(e).__name__}: {e}")

# ---------- Speech Longrunning ----------

async def _speech_longrunning_start(
    *,
    gcs_uri: str,
    language_code: str,   # <-- บังคับต้องมี
    alternative_language_codes: Optional[List[str]] = None,
    enable_automatic_punctuation: bool = True,
    diarization_speaker_count: Optional[int] = None,
    model: Optional[str] = None,
    use_enhanced: Optional[bool] = None,
    audio_channel_count: Optional[int] = None,
    enable_separate_recognition_per_channel: Optional[bool] = None,
    profanity_filter: Optional[bool] = None,
    speech_contexts: Optional[List[Dict[str, Any]]] = None,
    encoding: str = "ENCODING_UNSPECIFIED",
) -> Dict[str, Any]:
    token = await _get_access_token("https://www.googleapis.com/auth/cloud-platform")
    url = "https://speech.googleapis.com/v1/speech:longrunningrecognize"

    config: Dict[str, Any] = {
        "languageCode": language_code,                         # ✅ require
        "enableAutomaticPunctuation": bool(enable_automatic_punctuation),
        "encoding": encoding,                                   # ✅ ใส่ให้ชัด
    }
    if alternative_language_codes:
        config["alternativeLanguageCodes"] = alternative_language_codes
    if diarization_speaker_count:
        config["diarizationConfig"] = {
            "enableSpeakerDiarization": True,
            "minSpeakerCount": max(1, diarization_speaker_count),
            "maxSpeakerCount": max(1, diarization_speaker_count),
        }
    if model:
        config["model"] = model
    if use_enhanced is not None:
        config["useEnhanced"] = bool(use_enhanced)
    if audio_channel_count:
        config["audioChannelCount"] = int(audio_channel_count)
    if enable_separate_recognition_per_channel is not None:
        config["enableSeparateRecognitionPerChannel"] = bool(enable_separate_recognition_per_channel)
    if profanity_filter is not None:
        config["profanityFilter"] = bool(profanity_filter)
    if speech_contexts:
        config["speechContexts"] = speech_contexts

    payload = {"config": config, "audio": {"uri": gcs_uri}}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"❌ Speech start failed (HTTP {r.status_code}): {r.text[:800]}")
        return r.json()

async def _speech_poll_operation(
    *,
    name: str,
    max_wait_sec: float = 900.0,
    interval_sec: float = 5.0
) -> Dict[str, Any]:
    token = await _get_access_token("https://www.googleapis.com/auth/cloud-platform")
    url = f"https://speech.googleapis.com/v1/operations/{name}"
    headers = {"Authorization": f"Bearer {token}"}

    waited = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        while waited < max_wait_sec:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
            if data.get("done"):
                return data
            await asyncio.sleep(interval_sec)
            waited += interval_sec

    return {"done": False, "error": {"message": "timeout while polling speech operation"}}

def _join_transcript_from_operation(op: Dict[str, Any]) -> str:
    if not op or not op.get("done"):
        return ""
    if "error" in op:
        return ""
    resp = op.get("response") or {}
    results = resp.get("results") or []
    out: List[str] = []
    for res in results:
        alts = res.get("alternatives") or []
        if not alts:
            continue
        t = (alts[0].get("transcript") or "").strip()
        if t:
            out.append(t)
    return " ".join(out).strip()

# ---------- Public entry ----------

async def transcribe_long_audio_bytes(
    audio_bytes: bytes,
    *,
    file_ext: str = ".wav",
    content_type: Optional[str] = None,
    bucket_name: Optional[str],
    lang_hint: Optional[str] = None,
    alt_langs: Optional[List[str]] = None,
    # alias เดิม
    alternative_language_codes: Optional[List[str]] = None,
    poll: bool = True,
    max_wait_sec: float = 900.0,
    interval_sec: float = 5.0,
    diarization_speaker_count: Optional[int] = None,
    model: Optional[str] = None,
    use_enhanced: Optional[bool] = None,
    audio_channel_count: Optional[int] = None,
    enable_separate_recognition_per_channel: Optional[bool] = None,
    profanity_filter: Optional[bool] = None,
    speech_contexts: Optional[List[Dict[str, Any]]] = None,
    # 🔥 การลบไฟล์ใน GCS
    delete_after_seconds: Optional[int] = None,   # >0 = ลบแบบตั้งเวลา, None = อ่านจาก ENV, 0/None + immediate=False = ไม่ลบ
    delete_immediately: Optional[bool] = None,    # True = ลบทันทีหลังถอดเสร็จ (เฉพาะ poll=True)
) -> Tuple[str, Dict[str, Any]]:
    if not bucket_name:
        return "❌ Missing GCS_BUCKET_NAME", {}

    # อ่านนโยบายลบจาก ENV หาก caller ไม่ได้ส่ง
    if delete_immediately is None:
        delete_immediately = os.getenv("GCS_DELETE_IMMEDIATELY", "0").strip().lower() in {"1", "true", "yes", "on"}

    if delete_after_seconds is None:
        try:
            delete_after_seconds = int(os.getenv("GCS_DELETE_DELAY_SECONDS", "0"))
        except Exception:
            delete_after_seconds = 0

    # Normalize alias
    if alternative_language_codes and not alt_langs:
        alt_langs = alternative_language_codes

    # 1) MIME
    if not content_type:
        content_type = _guess_mime_by_ext(file_ext or "")

    # 2) Upload to GCS
    obj_name = f"discord_uploads/{uuid.uuid4().hex}{file_ext if file_ext.startswith('.') else f'.{file_ext}'}"
    try:
        _ = await _gcs_simple_upload(
            bucket=bucket_name,
            obj_name=obj_name,
            content=audio_bytes,
            content_type=content_type,
        )
    except httpx.HTTPStatusError as e:
        body = e.response.text[:800] if e.response is not None else ""
        return f"❌ GCS upload failed (HTTP {e.response.status_code})", {"error": body}
    except Exception as e:
        return f"❌ GCS upload error: {type(e).__name__}: {e}", {}

    gcs_uri = f"gs://{bucket_name}/{obj_name}"

    # 3) Build language & encoding
    language_code = _norm_lang(lang_hint) or "th-TH"   # ✅ default language
    alt_codes = [c for c in (alt_langs or []) if c] or None
    encoding = _mime_to_encoding(content_type, file_ext)

    # 4) Start longrunning
    try:
        start = await _speech_longrunning_start(
            gcs_uri=gcs_uri,
            language_code=language_code,
            alternative_language_codes=alt_codes,
            enable_automatic_punctuation=True,
            diarization_speaker_count=diarization_speaker_count,
            model=model,
            use_enhanced=use_enhanced,
            audio_channel_count=audio_channel_count,
            enable_separate_recognition_per_channel=enable_separate_recognition_per_channel,
            profanity_filter=profanity_filter,
            speech_contexts=speech_contexts,
            encoding=encoding,  # ✅ สำคัญ
        )
    except Exception as e:
        # พยายามลบไฟล์ทิ้งเลยถ้าตั้ง immediate
        try:
            if delete_immediately:
                await _gcs_delete_object(bucket_name, obj_name)
        except Exception:
            pass
        return f"❌ Speech start error: {e}", {}

    op_name = start.get("name")
    if not op_name:
        # ลบทิ้งตามนโยบาย
        try:
            if delete_immediately:
                await _gcs_delete_object(bucket_name, obj_name)
            elif (delete_after_seconds or 0) > 0:
                asyncio.create_task(_delete_later(bucket_name, obj_name, int(delete_after_seconds)))
        except Exception:
            pass
        return "❌ Speech operation has no name", start

    if not poll:
        # ถ้า poll=False ห้ามลบทันที (ยังประมวลผลไม่เสร็จ)
        if (delete_after_seconds or 0) > 0:
            asyncio.create_task(_delete_later(bucket_name, obj_name, int(delete_after_seconds)))
        return "⏳ STT job started (poll disabled).", start

    # 5) Poll
    try:
        op = await _speech_poll_operation(
            name=op_name, max_wait_sec=max_wait_sec, interval_sec=interval_sec
        )
    except httpx.HTTPStatusError as e:
        body = e.response.text[:800] if e.response is not None else ""
        # ลบทิ้งตามนโยบาย
        try:
            if delete_immediately:
                await _gcs_delete_object(bucket_name, obj_name)
            elif (delete_after_seconds or 0) > 0:
                asyncio.create_task(_delete_later(bucket_name, obj_name, int(delete_after_seconds)))
        except Exception:
            pass
        return f"❌ Speech poll failed (HTTP {e.response.status_code})", {"error": body}
    except Exception as e:
        try:
            if delete_immediately:
                await _gcs_delete_object(bucket_name, obj_name)
            elif (delete_after_seconds or 0) > 0:
                asyncio.create_task(_delete_later(bucket_name, obj_name, int(delete_after_seconds)))
        except Exception:
            pass
        return f"❌ Speech poll error: {type(e).__name__}: {e}", {}

    # 6) Join transcript
    text = _join_transcript_from_operation(op)

    # 7) Delete per policy
    try:
        if delete_immediately and poll:
            await _gcs_delete_object(bucket_name, obj_name)
        elif (delete_after_seconds or 0) > 0:
            asyncio.create_task(_delete_later(bucket_name, obj_name, int(delete_after_seconds)))
    except Exception as e:
        logger.warning(f"⚠️ cleanup failed for gs://{bucket_name}/{obj_name}: {e}")

    return text, op

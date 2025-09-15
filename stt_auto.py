# stt_auto.py
from __future__ import annotations
import os, uuid, mimetypes
from typing import Optional, Tuple, List

from stt_google_sync import stt_transcribe_bytes
from stt_google_async import transcribe_long_audio_bytes

COMPRESSED_EXTS = {".mp3", ".m4a", ".ogg", ".opus", ".webm", ".mp4"}
OPUS_MIME = {"audio/ogg", "audio/webm"}

def _guess( filename: Optional[str], content_type: Optional[str] ) -> tuple[str, str]:
    ext = (os.path.splitext(filename or "")[1] or "").lower()
    ctype = content_type or mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
    return ext, ctype

def _need_async(ext: str, size: int) -> bool:
    # บีบอัดเกิน ~1.8MB มักยาว > 1 นาที ⇒ บังคับ async
    if ext in COMPRESSED_EXTS:
        return size > 1_800_000
    # WAV/FLAC ไม่บีบอัด ให้เกณฑ์ ~9MB
    return size > 9_000_000

def _norm_lang(code: Optional[str]) -> str:
    if not code: return "th-TH"
    base = code.split("-")[0].lower()
    m = {
        "th":"th-TH","en":"en-US","ja":"ja-JP","zh":"cmn-Hans-CN","ko":"ko-KR",
        "vi":"vi-VN","ru":"ru-RU","fr":"fr-FR","de":"de-DE","es":"es-ES",
        "it":"it-IT","pt":"pt-PT","pl":"pl-PL","uk":"uk-UA","ar":"ar-EG","hi":"hi-IN",
    }
    return m.get(base, code)

async def transcribe_auto(
    *,
    audio_bytes: bytes,
    filename: Optional[str],
    content_type: Optional[str],
    primary_lang: str = "th-TH",
    alt_langs: Optional[List[str]] = None,
    make_txt_path: bool = True,
    gcs_bucket: Optional[str] = None,   # ถ้าไม่ส่ง จะอ่านจาก ENV
) -> Tuple[str, dict, str, Optional[str]]:
    """
    Return: (text, raw_json, mode, txt_path)
      mode: 'google async' | 'google sync'
      txt_path: path ของ .txt ถ้าสร้าง
    """
    ext, ctype = _guess(filename, content_type)
    primary = _norm_lang(primary_lang)
    alts = [ _norm_lang(x) for x in (alt_langs or []) ]

    bucket = gcs_bucket or os.getenv("GCS_BUCKET_NAME") or os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET")

    # เฮอร์ริสติกเลือก async อัตโนมัติ
    if bucket and _need_async(ext, len(audio_bytes or b"")):
        text, raw = await transcribe_long_audio_bytes(
            audio_bytes,
            file_ext=ext or ".wav",
            content_type=ctype,
            bucket_name=bucket,
            lang_hint=primary,
            alternative_language_codes=(["en-US"] + alts) if primary.startswith("th") and "en-US" not in alts else (alts or None),
        )
        mode = "google async"
    else:
        text, raw = await stt_transcribe_bytes(
            audio_bytes,
            filename=filename,
            content_type=ctype,
            lang_hint=primary,
            alternative_language_codes=(["en-US"] + alts) if primary.startswith("th") and "en-US" not in (alts or []) else alts,
        )
        mode = "google sync"

    txt_path = None
    if make_txt_path:
        txt_path = f"/tmp/transcript_{uuid.uuid4().hex}.txt"
        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write((text or "").strip())
        except Exception:
            txt_path = None

    return text, raw, mode, txt_path

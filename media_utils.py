import os
import math
import tempfile
import asyncio.subprocess as asp
from typing import Optional

# ------------------------------------------------------------
# MIME guess
# ------------------------------------------------------------

def guess_content_type(filename: str, fallback: str = "application/octet-stream") -> str:
    ext = (os.path.splitext(filename)[1] or "").lower()
    return {
        ".wav":  "audio/wav",
        ".mp3":  "audio/mpeg",
        ".m4a":  "audio/mp4",
        ".mp4":  "audio/mp4",
        ".aac":  "audio/aac",
        ".ogg":  "audio/ogg",
        ".opus": "audio/ogg",
        ".webm": "audio/webm",
        ".flac": "audio/flac",
    }.get(ext, fallback)

# ------------------------------------------------------------
# Subprocess helper
# ------------------------------------------------------------

async def _run_cmd(cmd: list[str], *, stdin: bytes | None) -> tuple[bytes, str, int]:
    """
    รันคำสั่งแบบ async; คืน (stdout_bytes, stderr_text, returncode)
    """
    proc = await asp.create_subprocess_exec(
        *cmd,
        stdin=asp.PIPE if stdin is not None else None,
        stdout=asp.PIPE,
        stderr=asp.PIPE,
    )
    out, err = await proc.communicate(input=stdin)
    return out or b"", (err.decode("utf-8", "ignore") if err else ""), proc.returncode

# ------------------------------------------------------------
# FFmpeg transcoding
# ------------------------------------------------------------

async def transcode_to_wav_pcm16(
    audio_bytes: bytes, *, rate: int = 16000, ch: int = 1,
    src_ext: Optional[str] = None, content_type: Optional[str] = None,
) -> bytes:
    """
    แปลงสตรีมเสียงให้เป็น WAV (PCM 16-bit, mono, 16kHz) ผ่านหลายแผน (pipe → force demuxer → temp file)
    """
    ext = (src_ext or "").lower()
    ctype = (content_type or "").lower()

    common_tail = [
        "-vn", "-sn",
        "-acodec", "pcm_s16le",
        "-ac", str(ch),
        "-ar", str(rate),
        "-f", "wav", "pipe:1"
    ]

    last_err = ""

    # Plan A: pipe
    cmdA = ["ffmpeg", "-nostdin", "-loglevel", "error", "-hide_banner", "-y",
            "-probesize", "50M", "-analyzeduration", "200M", "-i", "pipe:0", *common_tail]
    out, err, rc = await _run_cmd(cmdA, stdin=audio_bytes)
    if rc == 0 and len(out) > 1000:
        return out
    if err: last_err = err

    # Plan B: force demuxers
    def try_force(fmt: str) -> list[str]:
        return ["ffmpeg", "-nostdin", "-loglevel", "error", "-hide_banner", "-y",
                "-f", fmt, "-probesize", "50M", "-analyzeduration", "200M",
                "-i", "pipe:0", *common_tail]

    if ext in {".m4a", ".mp4"} or "audio/mp4" in ctype or "video/mp4" in ctype:
        out, err, rc = await _run_cmd(try_force("mp4"), stdin=audio_bytes)
        if rc == 0 and len(out) > 1000: return out
        if err: last_err = err

    if ext == ".aac" or "audio/aac" in ctype:
        out, err, rc = await _run_cmd(try_force("aac"), stdin=audio_bytes)
        if rc == 0 and len(out) > 1000: return out
        if err: last_err = err

    if ext == ".webm" or "webm" in ctype:
        out, err, rc = await _run_cmd(try_force("webm"), stdin=audio_bytes)
        if rc == 0 and len(out) > 1000: return out
        if err: last_err = err

    # Plan C: temp file (mp4/aac cases)
    need_seekable = (
        ext in {".m4a", ".mp4", ".aac"} or
        "audio/mp4" in ctype or "video/mp4" in ctype or "audio/aac" in ctype
    )
    if need_seekable:
        tmp_path = None
        try:
            suffix = ext if ext in {".m4a", ".mp4", ".aac"} else ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                f.write(audio_bytes)
                tmp_path = f.name

            cmdC1 = ["ffmpeg", "-nostdin", "-loglevel", "error", "-hide_banner", "-y",
                     "-probesize", "50M", "-analyzeduration", "200M", "-i", tmp_path, *common_tail]
            out, err, rc = await _run_cmd(cmdC1, stdin=None)
            if rc == 0 and len(out) > 1000: return out
            if err: last_err = err

            cmdC2 = ["ffmpeg", "-nostdin", "-loglevel", "error", "-hide_banner", "-y",
                     "-fflags", "+genpts+ignidx", "-err_detect", "ignore_err",
                     "-probesize", "50M", "-analyzeduration", "200M", "-i", tmp_path, *common_tail]
            out, err, rc = await _run_cmd(cmdC2, stdin=None)
            if rc == 0 and len(out) > 1000: return out
            if err: last_err = err
        finally:
            if tmp_path:
                try: os.remove(tmp_path)
                except Exception: pass

    # Plan D: webm temp
    if (ext == ".webm" or "webm" in ctype):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as f:
                f.write(audio_bytes)
                tmp_path = f.name
            cmdD = ["ffmpeg", "-nostdin", "-loglevel", "error", "-hide_banner", "-y",
                    "-probesize", "50M", "-analyzeduration", "200M", "-i", tmp_path, *common_tail]
            out, err, rc = await _run_cmd(cmdD, stdin=None)
            if rc == 0 and len(out) > 1000: return out
            if err: last_err = err
        finally:
            if tmp_path:
                try: os.remove(tmp_path)
                except Exception: pass

    tail = (last_err[-600:] if last_err else "no stderr")
    raise RuntimeError(f"ffmpeg transcode failed (multi-plan). tail:\n{tail}")

# ------------------------------------------------------------
# Ensure STT friendly format
# ------------------------------------------------------------

async def ensure_stt_compatible(
    filename: str, content_type: Optional[str], audio_bytes: bytes
) -> tuple[bytes, str, str, bool]:
    """
    บังคับให้ไฟล์เป็น WAV 16k mono เมื่อจำเป็น เพื่อให้ Google STT sync/long ใช้งานได้เสถียร
    คืน (bytes, new_filename, new_content_type, did_transcode)
    """
    ct = (content_type or "").lower()
    ext = (os.path.splitext(filename)[1] or "").lower()

    need_wav = False
    if ext in {".m4a", ".mp4", ".aac"} or "audio/mp4" in ct or "video/mp4" in ct or "audio/aac" in ct:
        need_wav = True
    elif ext == ".webm" and "opus" not in ct:
        need_wav = True

    if need_wav:
        wav = await transcode_to_wav_pcm16(audio_bytes, rate=16000, ch=1, src_ext=ext, content_type=ct)
        base = os.path.splitext(filename)[0]
        return wav, f"{base}.wav", "audio/wav", True

    return audio_bytes, filename, content_type or "", False

# ------------------------------------------------------------
# Helpers for STT quota flow
# ------------------------------------------------------------

async def download_to_temp(attachment) -> str:
    """
    ดาวน์โหลด discord.Attachment ไปไฟล์ชั่วคราว แล้วคืน path
    ใช้สำหรับวัดความยาวไฟล์ก่อนทำ STT
    """
    suffix = ""
    try:
        if getattr(attachment, "filename", None):
            dot = attachment.filename.rfind(".")
            suffix = attachment.filename[dot:] if dot != -1 else ""
    except Exception:
        pass

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        tmp_path = f.name
    # discord.py: await Attachment.save(fp)
    await attachment.save(tmp_path)
    return tmp_path

async def probe_duration_seconds(path: str) -> int:
    """
    ใช้ ffprobe วัดความยาวสื่อ (วินาที, ปัดขึ้น) — ถ้าอ่านไม่ได้คืน 0
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        out, err, rc = await _run_cmd(cmd, stdin=None)
        if rc != 0:
            return 0
        val = (out.decode("utf-8", "ignore") if isinstance(out, (bytes, bytearray)) else str(out)).strip()
        return max(0, int(math.ceil(float(val))))
    except Exception:
        return 0

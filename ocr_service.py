# ocr_service.py
from typing import Optional
from datetime import datetime
import base64
import httpx

from config import GOOGLE_API_KEY
from constants import OCR_DAILY_LIMIT, MAX_OCR_TEXT_LENGTH
from app_redis import check_and_increment_ocr_usage, increment_user_usage

async def ocr_google_vision_api_key(image_bytes: bytes, message) -> Optional[str]:
    api_key = GOOGLE_API_KEY
    if not api_key:
        await message.channel.send("❌ ไม่พบ GOOGLE_API_KEY ใน environment")
        return None
    if not image_bytes:
        await message.channel.send("❌ ไม่พบข้อมูลภาพที่ส่งมา")
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    allowed = await check_and_increment_ocr_usage(
        message.author.id, message.guild.id, today, global_daily_limit=OCR_DAILY_LIMIT
    )
    if not allowed:
        await message.channel.send(f"❌ เกินจำนวนจำกัด {OCR_DAILY_LIMIT} รูป/วันแล้ว กรุณารอวันถัดไป")
        return None

    try:
        encoded_image = base64.b64encode(image_bytes).decode("utf-8")
    except Exception as e:
        await message.channel.send(f"❌ ไม่สามารถแปลงภาพเป็น base64 ได้: {e}")
        return None

    payload = {
        "requests": [{
            "image": {"content": encoded_image},
            "features": [{"type": "TEXT_DETECTION"}],
            "imageContext": {"languageHints": ["th", "en", "ja", "zh", "ko", "ru", "vi"]},
        }]
    }
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
        resp.raise_for_status()
        result = resp.json()
    except httpx.TimeoutException:
        await message.channel.send("⏳ ขออภัย ระบบ OCR ใช้เวลานานเกินไป กรุณาลองใหม่")
        return None
    except httpx.HTTPStatusError as e:
        await message.channel.send(f"❌ OCR ผิดพลาด (HTTP {e.response.status_code})")
        return None
    except Exception:
        await message.channel.send("❌ ไม่สามารถติดต่อ Google Vision API ได้")
        return None

    if isinstance(result, dict) and "error" in result:
        code = result["error"].get("code")
        msg = result["error"].get("message", "Unknown error")
        await message.channel.send(f"❌ OCR ผิดพลาด (Google: {code}) — {msg}")
        return None

    responses = result.get("responses", []) if isinstance(result, dict) else []
    if not responses:
        await message.channel.send("❌ OCR ไม่ได้ส่งผลลัพธ์กลับมา")
        return None

    annotation = responses[0].get("fullTextAnnotation", {}) or {}
    text = (annotation.get("text") or "").strip()

    if not text:
        text_ann = responses[0].get("textAnnotations", [])
        if text_ann:
            text = (text_ann[0].get("description") or "").strip()

    if not text:
        await message.channel.send("❌ ไม่พบข้อความในภาพ")
        return None

    if len(text) > MAX_OCR_TEXT_LENGTH:
        text = text[:MAX_OCR_TEXT_LENGTH] + "\n... (ข้อความบางส่วนถูกตัด)"

    return text

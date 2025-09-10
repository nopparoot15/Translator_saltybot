from __future__ import annotations
import json
import os
from typing import Dict, List, Tuple, Optional

try:
    import redis.asyncio as redis
except Exception as e:
    redis = None  # จะ raise ตอน init_redis ถ้า lib ไม่พร้อม

# ------- Module State -------
_redis = None  # type: Optional["redis.Redis"]

# ------- Keys / TTL -------
LANG_HIST_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 วัน
OCR_TTL_SECONDS       = 60 * 60 * 24       # 1 วัน
GTRANS_TTL_SECONDS    = 60 * 60 * 24       # 1 วัน

def _key_lang_channel(channel_id: int) -> str:
    return f"langhist:channel:{int(channel_id)}"

def _key_lang_user(user_id: int) -> str:
    return f"langhist:user:{int(user_id)}"

def _key_usage(guild_id: int, user_id: int) -> str:
    return f"usage:{int(guild_id)}:{int(user_id)}"

def _key_ocr_global(date_str: str) -> str:
    return f"ocr_usage:global:{date_str}"

def _key_ocr_user(user_id: int, date_str: str) -> str:
    return f"ocr_usage:user:{int(user_id)}:{date_str}"

def _key_ocr_guild(guild_id: int, date_str: str) -> str:
    return f"ocr_usage:guild:{int(guild_id)}:{date_str}"

def _key_gtrans_global(date_str: str) -> str:
    return f"gtrans_usage:global:{date_str}"

# ============================================================
# Init / Client
# ============================================================

async def init_redis(url: Optional[str] = None, decode_responses: bool = True):
    """
    สร้าง global Redis client ให้โมดูลนี้ (ควรเรียกครั้งเดียวตอนบอทเริ่ม)
    """
    global _redis
    if _redis is not None:
        return _redis
    if redis is None:
        raise RuntimeError("redis.asyncio is not installed. pip install redis>=4.2")

    url = url or os.getenv("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL is not set. Provide it to init_redis().")

    _redis = redis.from_url(url, decode_responses=decode_responses)
    # ping test
    try:
        await _redis.ping()
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Redis: {e}")
    return _redis

def get_redis_client():
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call await init_redis(REDIS_URL) first.")
    return _redis

# ============================================================
# Internal JSON helpers
# ============================================================

async def _get_json(key: str) -> dict:
    r = get_redis_client()
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

async def _set_json(key: str, obj: dict, ttl: Optional[int] = None):
    r = get_redis_client()
    try:
        await r.set(key, json.dumps(obj))
        if ttl:
            await r.expire(key, ttl)
    except Exception:
        pass

# ============================================================
# Google Translate — Global Quota
# ============================================================

async def get_gtrans_used_today(date_str: str) -> int:
    r = get_redis_client()
    key = _key_gtrans_global(date_str)
    try:
        used = await r.get(key)
        return int(used) if used else 0
    except Exception:
        return 0

async def check_and_increment_gtranslate_quota(n_chars: int, date_str: str, daily_limit: int = 15000) -> tuple[bool, Optional[str]]:
    """
    คืน (ok, reason)
      - (True, None)     → บันทึกโควต้าแล้ว ใช้งานได้
      - (False, "exceeded"|"redis") → ใช้ไม่ได้ เพราะเกินโควต้าหรือ Redis ล่ม
    """
    r = get_redis_client()
    key = _key_gtrans_global(date_str)
    try:
        used_str = await r.get(key)
        used = int(used_str) if used_str else 0
        if used + n_chars > daily_limit:
            return False, "exceeded"
        await r.incrby(key, n_chars)
        await r.expire(key, GTRANS_TTL_SECONDS)
        return True, None
    except Exception:
        return False, "redis"

# ============================================================
# OCR — Daily counters (global/user/guild)
# ============================================================

async def check_and_increment_ocr_usage(user_id: int, guild_id: int, date_str: str, global_daily_limit: int = 30) -> bool:
    """
    global limit รายวัน: ถ้าถึงเพดาน -> return False
    อัปเดตนับ global/user/guild พร้อม TTL
    """
    r = get_redis_client()
    g_key = _key_ocr_global(date_str)
    u_key = _key_ocr_user(user_id, date_str)
    d_key = _key_ocr_guild(guild_id, date_str)

    try:
        # เช็ค global ก่อน
        current = await r.get(g_key)
        current = int(current or 0)
        if current >= global_daily_limit:
            return False

        # เพิ่มตัวนับ
        await r.incr(g_key)
        await r.expire(g_key, OCR_TTL_SECONDS)

        await r.incr(u_key)
        await r.expire(u_key, OCR_TTL_SECONDS)

        await r.incr(d_key)
        await r.expire(d_key, OCR_TTL_SECONDS)

        return True
    except Exception:
        return False

async def get_ocr_quota_remaining(user_id: int, date_str: str, per_user_limit: int = 30) -> int:
    """
    เหลือโควต้าผู้ใช้ต่อวัน (ถ้า Redis ล่ม -> -1)
    """
    r = get_redis_client()
    key = _key_ocr_user(user_id, date_str)
    try:
        count = await r.get(key)
        used = int(count) if count else 0
        return max(per_user_limit - used, 0)
    except Exception:
        return -1

# ============================================================
# Usage counters (leaderboard)
# ============================================================

async def increment_user_usage(user_id: int, guild_id: int) -> None:
    r = get_redis_client()
    try:
        await r.incr(_key_usage(guild_id, user_id))
    except Exception:
        pass

async def get_top_users(guild_id: int, top_n: int = 10) -> List[Tuple[int, int]]:
    """
    คืน [(user_id, count), ...] มากสุด top_n
    """
    r = get_redis_client()
    prefix = f"usage:{int(guild_id)}:"
    try:
        keys = await r.keys(f"{prefix}*")
        data: List[Tuple[int, int]] = []
        for k in keys:
            try:
                uid = int(k.split(":")[-1])
                count = await r.get(k)
                if count is not None:
                    data.append((uid, int(count)))
            except Exception:
                continue
        data.sort(key=lambda x: x[1], reverse=True)
        return data[:top_n]
    except Exception:
        return []

# ============================================================
# STT language hist (per channel / per user)
# ============================================================

async def get_channel_lang_hist(channel_id: int) -> Dict[str, int]:
    return await _get_json(_key_lang_channel(channel_id))

async def get_user_lang_hist(user_id: int) -> Dict[str, int]:
    return await _get_json(_key_lang_user(user_id))

async def incr_channel_lang_hist(channel_id: int, lang_code: str) -> None:
    key = _key_lang_channel(channel_id)
    hist = await _get_json(key)
    hist[lang_code] = int(hist.get(lang_code, 0)) + 1
    await _set_json(key, hist, LANG_HIST_TTL_SECONDS)

async def incr_user_lang_hist(user_id: int, lang_code: str) -> None:
    key = _key_lang_user(user_id)
    hist = await _get_json(key)
    hist[lang_code] = int(hist.get(lang_code, 0)) + 1
    await _set_json(key, hist, LANG_HIST_TTL_SECONDS)

from __future__ import annotations
import json
import os
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import redis.asyncio as redis
    from redis.exceptions import ResponseError
except Exception:
    redis = None  # จะ raise ตอน init_redis ถ้า lib ไม่พร้อม
    ResponseError = Exception  # fallback

# ============================================================
# Module State
# ============================================================

_redis = None  # type: Optional["redis.Redis"]
_lua_reserve_sha: Optional[str] = None  # cached SHA ของสคริปต์ Lua (อะตอมมิก reserve)

# ============================================================
# Keys / TTL (เดิม)
# ============================================================

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
# STT Daily-Seconds Quota (ใหม่)
# ============================================================

# รูปแบบกุญแจ: stt:sec:YYYYMMDD:{user_id}  หรือ  stt:sec:YYYYMMDD:{guild_id}:{user_id}
# เลือกสโคปด้วย ENV STT_QUOTA_SCOPE = "user" (ค่าเริ่มต้น) หรือ "guild_user"
_STT_SCOPE = os.getenv("STT_QUOTA_SCOPE", "user").strip().lower()  # "user" | "guild_user"

def _local_datestr(tz: ZoneInfo) -> str:
    return datetime.now(tz).strftime("%Y%m%d")

def _seconds_until_local_midnight(tz: ZoneInfo) -> int:
    now = datetime.now(tz)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((nxt - now).total_seconds()))

def _key_stt_seconds(date_str: str, user_id: int, guild_id: Optional[int]) -> str:
    if _STT_SCOPE == "guild_user" and guild_id:
        return f"stt:sec:{date_str}:{int(guild_id)}:{int(user_id)}"
    return f"stt:sec:{date_str}:{int(user_id)}"

# Lua อะตอมมิก: ถ้า cur + delta > limit => คืน -1 ไม่เพิ่ม; ไม่งั้น INCRBY และตั้ง TTL
_LUA_RESERVE = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local delta = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local cur = tonumber(redis.call('GET', key) or '0')
if cur + delta > limit then
  return -1
else
  local newv = redis.call('INCRBY', key, delta)
  if ttl > 0 then redis.call('EXPIRE', key, ttl) end
  return newv
end
"""

async def _ensure_lua_loaded():
    """โหลดสคริปต์ Lua ลง Redis หนึ่งครั้งต่อโปรเซส"""
    global _lua_reserve_sha
    if _lua_reserve_sha or _redis is None:
        return
    try:
        _lua_reserve_sha = await _redis.script_load(_LUA_RESERVE)
    except Exception:
        _lua_reserve_sha = None  # ให้ค่อย eval ได้ภายหลัง

async def stt_try_reserve(
    user_id: int,
    guild_id: Optional[int],
    seconds: int,
    daily_limit: int,
    tz: ZoneInfo
) -> bool:
    """
    พยายาม "จอง" วินาที STT แบบอะตอมมิกก่อนเริ่มถอดเสียง
    - สำเร็จ (ยังไม่เกินลิมิต): return True
    - เกินลิมิต: return False
    - Redis ล่ม/ผิดพลาด: (เลือก) fail-open -> return True เพื่อไม่บล็อกผู้ใช้
    """
    if _redis is None:
        # ยังไม่ได้ init หรือไม่มีไลบรารี — fail-open
        return True
    try:
        await _ensure_lua_loaded()
        date_str = _local_datestr(tz)
        key = _key_stt_seconds(date_str, user_id, guild_id)
        ttl = _seconds_until_local_midnight(tz) + 60  # กันเผื่อ 1 นาที

        if _lua_reserve_sha:
            try:
                res = await _redis.evalsha(_lua_reserve_sha, 1, key, daily_limit, int(seconds), ttl)
            except ResponseError:
                # กรณี NOSCRIPT ให้ fallback เป็น eval ตรง ๆ
                res = await _redis.eval(_LUA_RESERVE, 1, key, daily_limit, int(seconds), ttl)
        else:
            res = await _redis.eval(_LUA_RESERVE, 1, key, daily_limit, int(seconds), ttl)

        return int(res) != -1
    except Exception:
        # ไม่อยาก "ดับทั้งฟีเจอร์" เพราะ Redis พัง — เลือกเปิดผ่าน
        return True

async def stt_refund(user_id: int, guild_id: Optional[int], seconds: int, tz: ZoneInfo) -> None:
    """
    คืนวินาทีที่จองไว้ (กรณี STT ล้มเหลว) — ลดค่าใช้งานลง แต่ไม่ให้ติดลบ
    """
    if _redis is None:
        return
    try:
        date_str = _local_datestr(tz)
        key = _key_stt_seconds(date_str, user_id, guild_id)
        newv = await _redis.decrby(key, int(seconds))
        if int(newv) < 0:
            # กันค่าติดลบในกรณี concurrent
            await _redis.set(key, 0)
        # ย้ำ TTL ถึงเที่ยงคืน ถ้า key เพิ่งถูกสร้างจาก refund (ไม่น่าเกิด แต่กันไว้)
        ttl = await _redis.ttl(key)
        if ttl is None or ttl < 0:
            await _redis.expire(key, _seconds_until_local_midnight(tz) + 60)
    except Exception:
        pass

async def stt_get_used(user_id: int, guild_id: Optional[int], tz: ZoneInfo) -> int:
    """
    คืนจำนวนวินาทีที่ใช้ไปแล้ววันนี้ (0 ถ้าไม่มี/Redis มีปัญหา)
    """
    if _redis is None:
        return 0
    try:
        date_str = _local_datestr(tz)
        key = _key_stt_seconds(date_str, user_id, guild_id)
        v = await _redis.get(key)
        return int(v or 0)
    except Exception:
        return 0

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
# Google Translate — Global Quota (เดิม)
# ============================================================

async def get_gtrans_used_today(date_str: str) -> int:
    r = get_redis_client()
    key = _key_gtrans_global(date_str)
    try:
        used = await r.get(key)
        return int(used) if used else 0
    except Exception:
        return 0

async def check_and_increment_gtranslate_quota(
    n_chars: int,
    date_str: str,
    daily_limit: int = 15000
) -> tuple[bool, Optional[str]]:
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
# OCR — Daily counters (global/user/guild) (เดิม)
# ============================================================

async def check_and_increment_ocr_usage(
    user_id: int,
    guild_id: int,
    date_str: str,
    global_daily_limit: int = 30
) -> bool:
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
# Usage counters (leaderboard) (เดิม)
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
# STT language hist (per channel / per user) (เดิม)
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

import os
import base64
from typing import Optional

# ---- Basic credentials / endpoints ----
DISCORD_TOKEN     = os.getenv("DISCORD_BOT_TOKEN", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
GOOGLE_API_KEY    = os.getenv("GOOGLE_API_KEY", "")
REDIS_URL         = os.getenv("REDIS_URL", "")
GCS_BUCKET_NAME   = os.getenv("GCS_BUCKET_NAME", "")

# ---- GCP Service Account (base64) -> /app/gcp-key.json ----
GCP_SERVICE_ACCOUNT_B64 = os.getenv("GCP_SERVICE_ACCOUNT_B64")
KEY_PATH = "/app/gcp-key.json"

def prepare_gcp_key() -> None:
    """Decode GCP service account from env (if provided) and set GOOGLE_APPLICATION_CREDENTIALS."""
    try:
        if GCP_SERVICE_ACCOUNT_B64:
            with open(KEY_PATH, "wb") as f:
                f.write(base64.b64decode(GCP_SERVICE_ACCOUNT_B64))
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = KEY_PATH
            print("✅ Wrote service account to /app/gcp-key.json")
        elif os.path.exists(KEY_PATH):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = KEY_PATH
            print("✅ Using existing /app/gcp-key.json")
        else:
            print("⚠️ No GCP key found. Set GCP_SERVICE_ACCOUNT_B64 or mount /app/gcp-key.json")
    except Exception as e:
        print(f"❌ Failed to prepare service account key: {e}")

# ---- Timezone ----
from zoneinfo import ZoneInfo

_TZ_NAME = os.getenv("TZ", "Asia/Bangkok")
try:
    TZ = ZoneInfo(_TZ_NAME)
except Exception:
    # Fallback to UTC if TZ invalid
    print(f"⚠️ Invalid TZ '{_TZ_NAME}', falling back to UTC")
    TZ = ZoneInfo("UTC")

# ---- Helpers to read env safely ----
def _int_env(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        print(f"⚠️ ENV {name}='{v}' is not an int. Using default {default}.")
        return default

def _str_env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v else default

# ---- STT daily quota config ----
# Number of seconds allowed per "day" (local TZ) for STT usage.
STT_DAILY_LIMIT_SECONDS: int = _int_env("STT_DAILY_LIMIT_SECONDS", 120)  # 2 minutes default

# Scope of the quota key in Redis: "user" (per-user global) or "guild_user" (per-user per-guild)
STT_QUOTA_SCOPE: str = _str_env("STT_QUOTA_SCOPE", "user")  # or "guild_user"

__all__ = [
    # credentials
    "DISCORD_TOKEN", "OPENAI_API_KEY", "GOOGLE_API_KEY", "REDIS_URL", "GCS_BUCKET_NAME",
    # gcp
    "GCP_SERVICE_ACCOUNT_B64", "KEY_PATH", "prepare_gcp_key",
    # tz
    "TZ",
    # stt quota
    "STT_DAILY_LIMIT_SECONDS", "STT_QUOTA_SCOPE",
]

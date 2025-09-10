import os
import base64

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")

GCP_SERVICE_ACCOUNT_B64 = os.getenv("GCP_SERVICE_ACCOUNT_B64")
KEY_PATH = "/app/gcp-key.json"

def prepare_gcp_key():
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

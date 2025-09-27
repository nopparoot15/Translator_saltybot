# bot.py
import logging
import asyncio
import discord
from discord.ext import commands

from config import prepare_gcp_key, REDIS_URL
from app_redis import init_redis
from lang_config import FLAGS
from translate_panel import register_persistent_views

from tts_service import speak_text_multi, start_empty_vc_watcher
from commands_registry import register_commands
from events import register_message_handlers

# ---- Logging ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---- Discord intents/bot ----
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="%", intents=intents)

@bot.event
async def on_ready():
    # 1) เตรียม GCP key (สำหรับ STT/Upload GCS)
    prepare_gcp_key()

    # 2) Redis
    try:
        await init_redis(REDIS_URL)
    except Exception as e:
        logger.error(f"❌ Redis init failed: {e}")

    # 3) Register persistent UI views
    register_persistent_views(bot, speak_text_multi, FLAGS)

    # 4) เริ่ม watcher ออกจากห้องเมื่อว่าง
    start_empty_vc_watcher(bot)

    logger.info(f"✅ Logged in as {bot.user}")

def main():
    # ผูกคำสั่งทั้งหมด
    register_commands(bot)
    # ผูก on_message handler
    register_message_handlers(bot)

    # RUN
    from config import DISCORD_TOKEN
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()

import os
import shlex
import discord
from discord.ext import commands
from datetime import datetime, timedelta

from lang_config import FLAGS
from constants import GOOGLE_TRANSLATE_DAILY_LIMIT, OCR_DAILY_LIMIT, EXEMPT_USER_IDS
from app_redis import (
    get_gtrans_used_today,
    get_ocr_quota_remaining,
    stt_get_used,
    init_redis,
    get_redis_client,
)
from tts_service import user_tts_engine, server_tts_engine, get_tts_engine
from translation_service import translator_server_engine, get_translator_engine
from config import STT_DAILY_LIMIT_SECONDS, TZ, STT_QUOTA_SCOPE, REDIS_URL
from gcs_admin import gcs_delete_bucket, gcs_delete_all_objects  # ⬅️ นำเข้าเพิ่ม


def register_commands(bot: commands.Bot):
    try:
        bot.remove_command("help")
    except Exception:
        pass

    # ---------- Helpers ----------
    def _seconds_until_local_midnight(tz) -> int:
        now = datetime.now(tz)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(0, int((nxt - now).total_seconds()))

    def _fmt_hms(sec: int) -> str:
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if h > 0:
            return f"{h}ชม {m}น {s}วิ"
        if m > 0:
            return f"{m}น {s}วิ"
        return f"{s}วิ"

    # ---------- Commands ----------
    @bot.command(name="help")
    async def show_commands(ctx: commands.Context):
        embed = discord.Embed(
            title="📜 รายการคำสั่งทั้งหมด",
            description="คำสั่งหลักที่บอทรองรับ (prefix: `%`)",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="⚙️ General",
            value="`%clear [จำนวน]` — ลบข้อความ (สูงสุด 500)\n`%topusers` — อันดับการใช้งานบอทในเซิร์ฟเวอร์",
            inline=False
        )
        embed.add_field(
            name="🎙️ STT",
            value="`%sttquota` — เช็คโควต้า STT รายวัน (วินาที)",
            inline=False
        )
        embed.add_field(
            name="🔊 TTS",
            value="`%tts engine [user|server] [gtts|edge]` — ตั้งค่า TTS engine\n`%ttsstatus` — ดูสถานะ TTS ปัจจุบัน",
            inline=False
        )
        embed.add_field(
            name="🌐 Translation",
            value="`%translator engine [gpt4omini|gpt5nano|google]` — ตั้งค่า Translator engine\n"
                  "`%translator show` — ดู engine ที่ตั้งไว้\n"
                  "`%translatorstatus` — ดูสถานะ Translator engine",
            inline=False
        )
        embed.add_field(name="📸 OCR", value="`%ocr quota` — เช็คโควต้า OCR รายวัน", inline=False)
        embed.add_field(name="🌐 Google Translate", value="`%gtrans` — เช็คโควต้า Google Translate ทั้งบอท", inline=False)
        embed.add_field(
            name="☁️ GCS (ผู้ดูแลระบบ)",
            value=(
                "`%gcsclear <bucket> [--prefix=<pref>]` — ลบ **objects ทั้งหมด** (หรือเฉพาะ prefix)\n"
                "`%gcsdelbucket <bucket> [--force] [--prefix=<pref>]` — ลบบัคเก็ต (อันตรายมาก)"
            ),
            inline=False
        )
        embed.set_footer(text="พิมพ์ %help เพื่อเรียกดูรายการนี้ได้ตลอดเวลา")
        await ctx.send(embed=embed, delete_after=30)

    @bot.command(name="clear")
    @commands.has_permissions(manage_messages=True)
    async def clear_channel(ctx: commands.Context, amount: int | None = None):
        try:
            await ctx.message.delete()
        except Exception:
            pass
        n = amount if (amount and amount > 0) else 100
        n = min(n, 500)
        try:
            deleted = await ctx.channel.purge(limit=n)
            await ctx.send(f"🧹 ลบข้อความแล้ว {len(deleted)}/{n} ข้อความ", delete_after=5)
        except discord.Forbidden:
            await ctx.send("❌ บอทไม่มีสิทธิ์ลบข้อความในช่องนี้", delete_after=6)
        except Exception:
            await ctx.send("❌ เกิดข้อผิดพลาดระหว่างลบข้อความ", delete_after=6)

    @clear_channel.error
    async def clear_channel_error(ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ คุณไม่มีสิทธิ์ในการใช้คำสั่งนี้", delete_after=5)

    # ---------- STT QUOTA ----------
    @bot.command(name="sttquota")
    async def stt_quota(ctx: commands.Context):
        guild_id = ctx.guild.id if ctx.guild else None
        user_id = ctx.author.id
        is_exempt = user_id in EXEMPT_USER_IDS

        if not is_exempt:
            try:
                try:
                    get_redis_client()
                except RuntimeError:
                    await init_redis(REDIS_URL)
            except Exception as e:
                await ctx.send(
                    "❌ ไม่สามารถเชื่อมต่อ Redis ได้ จึงเช็คโควต้า STT ไม่ได้ในขณะนี้\n"
                    f"`{type(e).__name__}: {e}`",
                    delete_after=12
                )
                return

        try:
            if is_exempt:
                used = 0
                remain = STT_DAILY_LIMIT_SECONDS
            else:
                used = int(await stt_get_used(user_id, guild_id, TZ) or 0)
                remain = max(0, STT_DAILY_LIMIT_SECONDS - used)

            title = "🎙️ STT Quota วันนี้"
            if (STT_QUOTA_SCOPE or "user").lower() == "global":
                title += " (ทั้งบอท)"
            if is_exempt:
                title += " • ยกเว้นโควต้า"

            reset_in = _seconds_until_local_midnight(TZ)
            embed = discord.Embed(title=title, color=discord.Color.teal())
            embed.add_field(name="ใช้ไปแล้ว", value=f"{used} วินาที", inline=True)
            embed.add_field(name="โควต้าทั้งวัน", value=f"{STT_DAILY_LIMIT_SECONDS} วินาที", inline=True)
            embed.add_field(name="เหลือ", value=f"{remain} วินาที", inline=True)
            footer = f"รีเซ็ต 00:00 Asia/Bangkok • เหลืออีก {_fmt_hms(reset_in)}"
            if is_exempt:
                footer += " • คุณได้รับการยกเว้นโควต้า"
            embed.set_footer(text=footer)

            await ctx.send(embed=embed, delete_after=15)
        except Exception as e:
            await ctx.send(
                f"❌ ไม่สามารถตรวจสอบโควต้า STT ได้ในขณะนี้\n`{type(e).__name__}: {e}`",
                delete_after=12
            )

    # ---------- TTS ----------
    @bot.command(name="tts")
    async def set_tts_engine(ctx: commands.Context, *args: str):
        try:
            await ctx.message.delete()
        except Exception:
            pass

        if len(args) != 3 or args[0].lower() != "engine":
            await ctx.send("❗ ใช้งาน: `%tts engine [user|server] [gtts|edge]`", delete_after=8); return
        scope = args[1].lower().strip()
        engine = args[2].lower().strip()
        if scope not in {"user", "server"} or engine not in {"gtts", "edge"}:
            await ctx.send("❗ ใช้งาน: `%tts engine [user|server] [gtts|edge]`", delete_after=8); return

        guild_id = ctx.guild.id if ctx.guild else 0
        if scope == "server":
            if ctx.guild is None:
                await ctx.send("❌ ใช้ในเซิร์ฟเวอร์เท่านั้น", delete_after=6); return
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ ต้องเป็นแอดมินถึงจะตั้งค่าเซิร์ฟเวอร์ได้", delete_after=6); return
            prev = server_tts_engine.get(guild_id, "gtts")
            server_tts_engine[guild_id] = engine
            effective = get_tts_engine(ctx.author.id, guild_id)
            await ctx.send(f"✅ TTS (server): `{prev}` → `{engine}`\n👉 ใช้งานจริงตอนนี้: `{effective}`", delete_after=6)
        else:
            prev = user_tts_engine.get(ctx.author.id, "gtts")
            user_tts_engine[ctx.author.id] = engine
            effective = get_tts_engine(ctx.author.id, guild_id)
            await ctx.send(f"✅ TTS (you): `{prev}` → `{engine}`\n👉 ใช้งานจริงตอนนี้: `{effective}`", delete_after=6)

    @bot.command(name="ttsstatus")
    async def tts_status(ctx: commands.Context):
        user_engine = user_tts_engine.get(ctx.author.id, "gtts")
        server_engine = server_tts_engine.get(ctx.guild.id, "gtts") if ctx.guild else "gtts"
        effective = get_tts_engine(ctx.author.id, ctx.guild.id if ctx.guild else 0)
        embed = discord.Embed(title="🔊 TTS Engine Status", color=discord.Color.purple())
        embed.add_field(name="ของคุณ", value=f"`{user_engine}`", inline=True)
        embed.add_field(name="ของเซิร์ฟเวอร์", value=f"`{server_engine}`", inline=True)
        embed.add_field(name="ใช้งานจริงตอนนี้", value=f"`{effective}`", inline=False)
        await ctx.send(embed=embed, delete_after=10)

    # ---------- OCR ----------
    @bot.command(name="ocr")
    async def ocr_group(ctx: commands.Context, subcommand: str | None = None):
        sub = (subcommand or "").lower().strip()
        if sub == "quota":
            today = datetime.now(TZ).strftime("%Y-%m-%d")
            remaining = await get_ocr_quota_remaining(user_id=ctx.author.id, date_str=today, per_user_limit=OCR_DAILY_LIMIT)
            if remaining >= 0:
                await ctx.send(
                    f"📸 วันนี้คุณใช้ OCR ไปแล้ว {OCR_DAILY_LIMIT - remaining}/{OCR_DAILY_LIMIT} ครั้ง\n"
                    f"✅ เหลืออีก {remaining} ครั้ง"
                )
            else:
                await ctx.send("❌ ไม่สามารถตรวจสอบโควต้า OCR ได้ในขณะนี้")
        else:
            await ctx.send("❓ ใช้งาน: `%ocr quota` เพื่อดูโควต้า OCR วันนี้ของคุณ", delete_after=8)

    # ---------- Leaderboard ----------
    @bot.command(name="topusers")
    async def top_users(ctx: commands.Context):
        from app_redis import get_top_users
        try:
            data = await get_top_users(ctx.guild.id, top_n=10)
            if not data:
                await ctx.send("📊 ยังไม่มีใครใช้งานบอทเลย"); return
            lines = []
            for rank, (user_id, count) in enumerate(data, start=1):
                member = ctx.guild.get_member(user_id)
                name = member.display_name if member else f"<@{user_id}>"
                lines.append(f"{rank}. **{name}** — {count} ครั้ง")
            await ctx.send("📈 Top users ในเซิร์ฟเวอร์นี้:\n\n" + "\n".join(lines))
        except Exception:
            await ctx.send("❌ ไม่สามารถดึงข้อมูลผู้ใช้งานได้", delete_after=6)

    # ---------- Translator ----------
    @bot.command(name="translator")
    async def set_translator_provider(ctx: commands.Context, *args: str):
        try:
            await ctx.message.delete()
        except Exception:
            pass

        mapping = {
            "gpt4omini": "GPT-4o mini",
            "gpt5nano": "GPT-5 nano",
            "google": "Google Translate",
            "gpt": "GPT-4o mini",
        }

        if not args:
            await ctx.send(
                "❗ ใช้งาน: `%translator engine [gpt4omini|gpt5nano|google]` หรือ `%translator show`",
                delete_after=10
            ); return

        sub = args[0].lower().strip()
        if sub == "show":
            guild_id = ctx.guild.id if ctx.guild else 0
            current = get_translator_engine(guild_id)
            display = mapping.get(current.lower(), current)
            await ctx.send(f"🌐 Engine ปัจจุบัน: `{display}`", delete_after=6)
            return

        if sub != "engine" or len(args) != 2:
            await ctx.send("❗ ใช้งาน: `%translator engine [gpt4omini|gpt5nano|google]`", delete_after=8); return

        engine = args[1].lower().strip()
        if engine not in {"gpt4omini", "gpt5nano", "google"}:
            await ctx.send("❗ ค่า engine ต้องเป็น `gpt4omini`, `gpt5nano`, หรือ `google`", delete_after=8); return

        if ctx.guild is None:
            await ctx.send("❌ ใช้ในเซิร์ฟเวอร์เท่านั้น", delete_after=6); return
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ ต้องเป็นแอดมินถึงจะตั้งค่าได้", delete_after=6); return

        guild_id = ctx.guild.id
        prev = translator_server_engine.get(guild_id, "gpt4omini")
        translator_server_engine[guild_id] = engine

        prev_disp = mapping.get(prev, prev)
        new_disp = mapping.get(engine, engine)
        await ctx.send(f"✅ ตั้งค่า Translator Engine: `{prev_disp}` → `{new_disp}`", delete_after=6)

    @bot.command(name="gtrans")
    async def gtrans_cmd(ctx: commands.Context, sub: str | None = None):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        try:
            used = await get_gtrans_used_today(date_str=today)
            remaining = max(0, GOOGLE_TRANSLATE_DAILY_LIMIT - (used or 0))
            await ctx.send(
                f"🌐 โควต้า Google Translate วันนี้: ใช้ไป {used}/{GOOGLE_TRANSLATE_DAILY_LIMIT} ตัวอักษร\n"
                f"✅ เหลือ {remaining} ตัวอักษร",
                delete_after=10
            )
        except Exception:
            await ctx.send("❌ ไม่สามารถตรวจสอบโควต้า Google Translate ได้", delete_after=8)

    @bot.command(name="translatorstatus")
    async def translator_status(ctx: commands.Context):
        name_map = {"gpt4omini": "GPT-4o mini", "gpt5nano": "GPT-5 nano", "google": "Google Translate"}
        guild_id = ctx.guild.id if ctx.guild else 0
        server_engine_key = translator_server_engine.get(guild_id, "gpt4omini")
        effective_key = get_translator_engine(guild_id)
        server_engine = name_map.get(server_engine_key.lower(), server_engine_key)
        effective = name_map.get(effective_key.lower(), effective_key)

        embed = discord.Embed(title="🌐 Translator Engine Status", color=discord.Color.green())
        embed.add_field(name="ตั้งค่าไว้ (เซิร์ฟเวอร์)", value=f"`{server_engine}`", inline=True)
        embed.add_field(name="ใช้งานจริงตอนนี้", value=f"`{effective}`", inline=True)
        await ctx.send(embed=embed, delete_after=10)

    # ---------- GCS Admin (Danger Zone) ----------
    def _gcs_admin_allow(ctx: commands.Context) -> bool:
        """อนุญาตใช้คำสั่ง GCS สำหรับผู้มีสิทธิ์เท่านั้น"""
        allow_env = os.getenv("GCS_ADMIN_ALLOWLIST", "").replace(" ", "")
        if allow_env:
            allowed = {int(x) for x in allow_env.split(",") if x.isdigit()}
            return ctx.author.id in allowed
        perms = getattr(ctx.author, "guild_permissions", None)
        return bool(perms and (perms.administrator or perms.manage_guild))

    @bot.command(name="gcsclear", help="ลบ objects ทั้งหมดในบัคเก็ต (อันตราย!) ใช้: %gcsclear <bucket> [--prefix=<pref>]")
    async def gcsclear(ctx: commands.Context, *, args: str):
        if not _gcs_admin_allow(ctx):
            return await ctx.reply("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้", mention_author=False)

        try:
            parts = shlex.split(args)
        except ValueError:
            return await ctx.reply("รูปแบบไม่ถูกต้อง", mention_author=False)

        if not parts:
            return await ctx.reply("ระบุชื่อบัคเก็ตด้วย เช่น `%gcsclear my-bucket --prefix=discord_uploads/`", mention_author=False)

        bucket = parts[0]
        prefix = None
        for p in parts[1:]:
            if p.startswith("--prefix="):
                prefix = p.split("=", 1)[1] or None

        warn = f"จะลบ **objects ทั้งหมด** ใน `gs://{bucket}`" + (f" ที่ prefix `{prefix}`" if prefix else "") + " — ระวัง!"
        await ctx.send(f"⚠️ {warn}", delete_after=10)

        msg = await ctx.reply("⏳ กำลังลบไฟล์…", mention_author=False)
        try:
            n = await gcs_delete_all_objects(bucket, prefix=prefix)
            if n > 0:
                await msg.edit(content=f"✅ ลบไฟล์แล้ว {n} ชิ้น จาก `gs://{bucket}`" + (f" (prefix=`{prefix}`)" if prefix else ""))
            else:
                await msg.edit(content=f"ℹ️ ไม่พบไฟล์ให้ลบใน `gs://{bucket}`" + (f" (prefix=`{prefix}`)" if prefix else ""))
        except Exception as e:
            await msg.edit(content=f"❌ ลบไม่สำเร็จ: `{type(e).__name__}: {e}`")

    @bot.command(name="gcsdelbucket", help="ลบบัคเก็ต GCS (อันตราย!) ใช้: %gcsdelbucket <bucket> [--force] [--prefix=<pref>]")
    async def gcsdelbucket(ctx: commands.Context, *, args: str):
        if not _gcs_admin_allow(ctx):
            return await ctx.reply("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้", mention_author=False)

        try:
            parts = shlex.split(args)
        except ValueError:
            return await ctx.reply("รูปแบบไม่ถูกต้อง", mention_author=False)

        if not parts:
            return await ctx.reply("ระบุชื่อบัคเก็ตด้วย เช่น `%gcsdelbucket my-bucket --force`", mention_author=False)

        bucket = parts[0]
        force = any(p == "--force" for p in parts[1:])
        prefix = None
        for p in parts[1:]:
            if p.startswith("--prefix="):
                prefix = p.split("=", 1)[1] or None

        warn = (
            f"จะลบบัคเก็ต `{bucket}`"
            + (f" (จะลบ objects ที่ prefix นี้ก่อน: `{prefix}`)" if (force and prefix) else "")
            + (" โดยจะลบ objects ทั้งหมดก่อน" if force and not prefix else "")
            + (" โดยไม่ลบ objects ภายใน" if not force else "")
            + " — ดำเนินการต่อหรือไม่?"
        )
        await ctx.send(f"⚠️ {warn}", delete_after=10)

        msg = await ctx.reply("⏳ กำลังดำเนินการ…", mention_author=False)
        ok, text = await gcs_delete_bucket(bucket, force=force, prefix=prefix)
        await msg.edit(content=text)

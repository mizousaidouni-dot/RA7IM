import os
import re
import time
import base64
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
import yt_dlp

# ==========================================================
# إعدادات التسجيل (Logging)
# ==========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("RA7IM_BOT")

# ==========================================================
# الإعدادات الأساسية
# ==========================================================
API_ID = int(os.getenv("API_ID", "39688640") or 0)
API_HASH = os.getenv("API_HASH", "787fa93ba42cadd097c01c5069ee40b").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8876596447:AAGLbK3QYg4DcED9RzyLwHZvuzrJevmSrKs").strip()

MAX_DURATION = int(os.getenv("MAX_DURATION", "900"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "700"))
RESULTS_COUNT = int(os.getenv("RESULTS_COUNT", "6"))
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "3"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_B64 = os.getenv("COOKIES_BASE64", "").strip()
COOKIES_FILE = Path("cookies.txt")

if COOKIES_B64:
    try:
        COOKIES_FILE.write_bytes(base64.b64decode(COOKIES_B64))
        logger.info("تم تحميل ملف cookies.txt بنجاح من المتغيرات.")
    except Exception as e:
        logger.error(f"فشل فك تشفير COOKIES_BASE64: {e}")

if not shutil.which("ffmpeg"):
    raise SystemExit("ffmpeg غير مثبت. ثبّته أولاً (مطلوب للتحويل).")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise SystemExit("يرجى التأكد من وجود API_ID و API_HASH و BOT_TOKEN.")

app = Client(
    "ra7im_yt_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

download_semaphore = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
SEARCH_CACHE: Dict[str, List[Dict[str, Any]]] = {}

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/|live/|embed/)|youtu\.be/)[\w\-?&=%.]+"
)

def format_duration(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return "غير معروف"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def clean_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()[:80] or "media"

def get_base_ydl_opts(use_cookies: bool = True, client_profile: str = "default") -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    if client_profile == "tv":
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["tv_simply", "tv", "web"],
            }
        }
    elif client_profile == "android":
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "android", "mweb"],
            }
        }
    if use_cookies and COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 10:
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts

def search_youtube_sync(query: str, limit: int = 6) -> List[Dict[str, Any]]:
    opts = get_base_ydl_opts(use_cookies=True, client_profile="default")
    opts["extract_flat"] = "in_playlist"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        entries = info.get("entries") or []
        results = []
        for item in entries:
            if not item:
                continue
            vid = item.get("id")
            if not vid:
                continue
            results.append({
                "id": vid,
                "title": item.get("title") or "بدون عنوان",
                "duration": item.get("duration"),
                "channel": item.get("uploader") or item.get("channel") or "يوتيوب",
                "url": f"https://www.youtube.com/watch?v={vid}",
            })
        return results

def download_media_sync(video_url: str, mode: str, job_dir: Path) -> Dict[str, Any]:
    job_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(job_dir / "%(title).70s.%(ext)s")

    # محاولات متعددة لتجاوز قيود SABR والكوكيز
    attempts = [
        {"use_cookies": True, "profile": "default"},
        {"use_cookies": False, "profile": "default"},
        {"use_cookies": False, "profile": "tv"},
        {"use_cookies": True, "profile": "tv"},
        {"use_cookies": False, "profile": "android"},
    ]

    last_error = None
    for attempt in attempts:
        # تنظيف أي ملفات جزئية من المحاولة السابقة
        for old_f in job_dir.glob("*"):
            try:
                old_f.unlink()
            except Exception:
                pass

        opts = get_base_ydl_opts(
            use_cookies=attempt["use_cookies"],
            client_profile=attempt["profile"],
        )
        opts["outtmpl"] = outtmpl

        if mode == "audio":
            # عدم فرض صيغة صارمة واستخدام محول FFmpeg للاستخراج
            opts["format"] = "ba/b"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        else:
            # صيغة مرنة جداً مع الترتيب التلقائي لأفضل توافق MP4
            opts["format"] = "bv*+ba/b"
            opts["format_sort"] = ["vcodec:h264", "res:720", "acodec:aac", "ext:mp4:m4a"]
            opts["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                files = [f for f in job_dir.glob("*") if f.is_file() and f.stat().st_size > 0]
                if not files:
                    raise RuntimeError("لم يتم العثور على الملف المحمّل.")
                file_path = max(files, key=lambda p: p.stat().st_size)
                return {
                    "path": file_path,
                    "title": info.get("title") or file_path.stem,
                    "duration": int(info.get("duration") or 0),
                    "performer": info.get("uploader") or info.get("channel") or "YouTube",
                }
        except Exception as e:
            last_error = e
            logger.warning(f"محاولة فاشلة ({attempt}): {e}")
            continue

    raise last_error or RuntimeError("فشل التحميل بعد جميع المحاولات.")

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    text = (
        "👋 **أهلاً بك في بوت التحميل من يوتيوب!**\n\n"
        "📌 **طريقة الاستخدام:**\n"
        "• أرسل **رابط فيديو أو شورتس** من يوتيوب مباشرة.\n"
        "• أو اكتب **اسم أي مقطع أو أغنية** وسأبحث لك عنه فوراً."
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.text & filters.private & ~filters.command(["start"]))
async def handle_text(client: Client, message: Message):
    query = (message.text or "").strip()
    if not query:
        return

    match = YOUTUBE_URL_RE.search(query)
    if match:
        url = match.group(0)
        if not url.startswith("http"):
            url = "https://" + url

        vid_match = re.search(r"(?:v=|shorts/|live/|embed/|youtu\.be/)([\w\-]{11})", url)
        vid = vid_match.group(1) if vid_match else None
        if not vid:
            await message.reply_text("❌ تعذر استخراج معرف الفيديو من الرابط.")
            return

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎬 تحميل فيديو (MP4)", callback_data=f"dl|video|{vid}"),
                InlineKeyboardButton("🎵 تحميل صوت (MP3)", callback_data=f"dl|audio|{vid}"),
            ]
        ])
        await message.reply_text(
            "اختر صيغة التحميل المطلوبة:",
            reply_markup=buttons,
        )
        return

    wait_msg = await message.reply_text("🔎 جاري البحث في يوتيوب...")
    try:
        results = await asyncio.to_thread(search_youtube_sync, query, RESULTS_COUNT)
    except Exception as e:
        logger.error(f"Search error: {e}")
        await wait_msg.edit_text("❌ حدث خطأ أثناء البحث، حاول مرة أخرى.")
        return

    if not results:
        await wait_msg.edit_text("⚠️ لم يتم العثور على نتائج مطابقة.")
        return

    cache_key = f"{message.from_user.id}_{int(time.time())}"
    SEARCH_CACHE[cache_key] = results

    lines = ["🎬 **نتائج البحث — اختر المقطع المطلوب:**\n"]
    kb_rows = []
    for idx, item in enumerate(results, 1):
        dur = format_duration(item["duration"])
        lines.append(f"**{idx}.** {item['title'][:55]} (`{dur}`)")
        kb_rows.append([
            InlineKeyboardButton(f"🎬 فيديو {idx}", callback_data=f"dl|video|{item['id']}"),
            InlineKeyboardButton(f"🎵 صوت {idx}", callback_data=f"dl|audio|{item['id']}"),
        ])

    await wait_msg.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb_rows),
        parse_mode=enums.ParseMode.MARKDOWN,
    )

@app.on_callback_query(filters.regex(r"^dl\|(video|audio)\|([\w\-]{11})$"))
async def handle_download_cb(client: Client, cb: CallbackQuery):
    mode = cb.matches[0].group(1)
    vid = cb.matches[0].group(2)
    video_url = f"https://www.youtube.com/watch?v={vid}"

    await cb.answer("⏳ تم استلام طلبك...")
    status_msg = await cb.message.reply_text("⏳ جاري تجهيز التحميل...")

    job_dir = DOWNLOAD_DIR / f"{cb.from_user.id}_{vid}_{int(time.time())}"

    async with download_semaphore:
        try:
            await status_msg.edit_text("⬇️ جاري التحميل من يوتيوب...")
            result = await asyncio.to_thread(download_media_sync, video_url, mode, job_dir)
            file_path: Path = result["path"]
            size_mb = file_path.stat().st_size / (1024 * 1024)

            if size_mb > MAX_FILE_MB:
                await status_msg.edit_text(f"❌ حجم الملف ({size_mb:.1f} MB) يتجاوز الحد المسموح ({MAX_FILE_MB} MB).")
                return

            await status_msg.edit_text("⬆️ جاري الرفع إلى تيليجرام...")
            caption = f"✅ **{result['title']}**"

            if mode == "audio":
                await client.send_audio(
                    chat_id=cb.message.chat.id,
                    audio=str(file_path),
                    caption=caption,
                    title=result["title"][:60],
                    performer=result["performer"][:60],
                    duration=result["duration"],
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            else:
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=str(file_path),
                    caption=caption,
                    duration=result["duration"],
                    supports_streaming=True,
                    parse_mode=enums.ParseMode.MARKDOWN,
                )

            await status_msg.delete()

        except Exception as e:
            logger.error(f"Download error: {e}")
            await status_msg.edit_text(
                f"❌ تعذر تحميل المقطع حالياً.\nالسبب: `{str(e)[:180]}`",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

if __name__ == "__main__":
    logger.info("Starting RA7IM YouTube Bot...")
    app.run()

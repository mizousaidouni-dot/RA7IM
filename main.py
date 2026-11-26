import os
import re
import time
import glob
import math
import base64
import shutil
import asyncio
import logging
from typing import Dict, Any, List, Optional

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# إعداد السجلات
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# التحقق من وجود ffmpeg
if not shutil.which("ffmpeg"):
    raise SystemExit("ffmpeg غير مثبت. ثبّته أولاً (مطلوب للتحويل).")

# إعدادات البوت (تقرأ من متغيرات البيئة وتستخدم القيم الاحتياطية تلقائياً)
API_ID = int(os.getenv("API_ID", "39688640") or 0)
API_HASH = os.getenv("API_HASH", "787fa93ba42cadd097c01c5069ee40b").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8876596447:AAGLbK3QYg4DcED9RzyLwHZvuzrJevmSrKs").strip()

# حدود البوت القابلة للتعديل من Variables
MAX_DURATION = int(os.getenv("MAX_DURATION", "900") or 900)
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "700") or 700)
RESULTS_COUNT = int(os.getenv("RESULTS_COUNT", "6") or 6)
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "3") or 3)

# إعداد ملف الكوكيز من متغير COOKIES_BASE64 داخل السيرفر فقط
COOKIES_FILE = "cookies.txt"
COOKIES_B64 = os.getenv("COOKIES_BASE64", "").strip()
if COOKIES_B64:
    try:
        with open(COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(COOKIES_B64))
        logger.info("تم تحميل ملف الكوكيز بنجاح من متغير البيئة.")
    except Exception as e:
        logger.error(f"فشل فك تشفير COOKIES_BASE64: {e}")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client(
    "ra7im_yt_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

SEARCH_CACHE: Dict[str, List[Dict[str, Any]]] = {}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/\S+",
    re.IGNORECASE,
)


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "غير معروف"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def get_base_ydl_opts() -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "tv_embedded"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE
    return opts


def search_youtube_sync(query: str, limit: int = 6) -> List[Dict[str, Any]]:
    opts = get_base_ydl_opts()
    opts["extract_flat"] = True
    opts["default_search"] = f"ytsearch{limit}"
    results = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        for entry in info.get("entries", []) or []:
            if not entry:
                continue
            vid_id = entry.get("id")
            url = entry.get("url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None)
            if not url:
                continue
            results.append(
                {
                    "id": vid_id,
                    "title": entry.get("title") or "بدون عنوان",
                    "url": url,
                    "duration": entry.get("duration"),
                    "uploader": entry.get("uploader") or entry.get("channel") or "",
                }
            )
    return results


def download_media_sync(url: str, mode: str, uid: str) -> Dict[str, Any]:
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{uid}_%(id)s.%(ext)s")
    opts = get_base_ydl_opts()
    opts["outtmpl"] = outtmpl
    opts["noplaylist"] = True

    if mode == "audio":
        opts["format"] = "ba[ext=m4a]/ba/b"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b"
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = info.get("duration") or 0
        if MAX_DURATION > 0 and duration > MAX_DURATION:
            raise ValueError(
                f"مدة المقطع ({format_duration(duration)}) تتجاوز الحد المسموح ({format_duration(MAX_DURATION)})."
            )

        info = ydl.extract_info(url, download=True)
        files = sorted(glob.glob(os.path.join(DOWNLOAD_DIR, f"{uid}_*")), key=os.path.getmtime, reverse=True)
        if not files:
            raise FileNotFoundError("لم يتم العثور على الملف بعد التحميل.")

        filepath = files[0]
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            os.remove(filepath)
            raise ValueError(f"حجم الملف ({size_mb:.1f} MB) يتجاوز الحد الأقصى ({MAX_FILE_MB} MB).")

        return {
            "filepath": filepath,
            "title": info.get("title") or "YouTube Media",
            "duration": int(duration),
            "performer": info.get("uploader") or info.get("channel") or "YouTube",
        }


def cleanup_files(uid: str):
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{uid}_*")):
        try:
            os.remove(f)
        except OSError:
            pass


def build_quality_keyboard(url: str) -> InlineKeyboardMarkup:
    key = str(abs(hash(url)))[:12]
    SEARCH_CACHE[f"url_{key}"] = [{"url": url}]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 فيديو (MP4)", callback_data=f"dl|video|{key}"),
                InlineKeyboardButton("🎵 صوت (MP3)", callback_data=f"dl|audio|{key}"),
            ]
        ]
    )


@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    await message.reply_text(
        "👋 **أهلاً بك في بوت التحميل من يوتيوب!**\n\n"
        "• أرسل **رابط يوتيوب** مباشرة لتحميله كفيديو أو صوت.\n"
        "• أو اكتب **اسم أي مقطع أو أغنية** للبحث عنه واختياره من النتائج."
    )


@app.on_message(filters.text & filters.private & ~filters.command(["start"]))
async def handle_text(client: Client, message: Message):
    text = (message.text or "").strip()
    if not text:
        return

    match = YOUTUBE_URL_RE.search(text)
    if match:
        url = match.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        await message.reply_text(
            "اختر صيغة التحميل المطلوبة:",
            reply_markup=build_quality_keyboard(url),
            quote=True,
        )
        return

    status = await message.reply_text("🔍 جاري البحث في يوتيوب...", quote=True)
    try:
        results = await asyncio.to_thread(search_youtube_sync, text, RESULTS_COUNT)
        if not results:
            await status.edit_text("❌ لم يتم العثور على نتائج مطابقة.")
            return

        cache_id = f"{message.from_user.id}_{int(time.time())}"
        SEARCH_CACHE[cache_id] = results

        buttons = []
        lines = ["🎬 **نتائج البحث — اختر المقطع المطلوب:**\n"]
        for idx, item in enumerate(results):
            dur = format_duration(item.get("duration"))
            title = item["title"][:45] + ("..." if len(item["title"]) > 45 else "")
            lines.append(f"**{idx + 1}.** {item['title']} (`{dur}`)")
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{idx + 1}. {title} [{dur}]",
                        callback_data=f"pick|{cache_id}|{idx}",
                    )
                ]
            )

        await status.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Search error: {e}")
        await status.edit_text(f"❌ حدث خطأ أثناء البحث:\n`{e}`")


@app.on_callback_query(filters.regex(r"^pick\|"))
async def on_pick_result(client: Client, callback: CallbackQuery):
    parts = callback.data.split("|")
    if len(parts) != 3:
        await callback.answer("بيانات غير صالحة.", show_alert=True)
        return

    cache_id, idx_str = parts[1], parts[2]
    results = SEARCH_CACHE.get(cache_id)
    if not results or not idx_str.isdigit() or int(idx_str) >= len(results):
        await callback.answer("انتهت صلاحية البحث، ابحث مجدداً.", show_alert=True)
        return

    item = results[int(idx_str)]
    await callback.message.edit_text(
        f"📌 **{item['title']}**\n⏱ المدة: `{format_duration(item.get('duration'))}`\n\nاختر صيغة التحميل:",
        reply_markup=build_quality_keyboard(item["url"]),
    )


@app.on_callback_query(filters.regex(r"^dl\|"))
async def on_download(client: Client, callback: CallbackQuery):
    parts = callback.data.split("|")
    if len(parts) != 3:
        await callback.answer("طلب غير صالح.", show_alert=True)
        return

    mode, key = parts[1], parts[2]
    cached = SEARCH_CACHE.get(f"url_{key}")
    if not cached:
        await callback.answer("انتهت صلاحية الرابط، أرسله مجدداً.", show_alert=True)
        return

    url = cached[0]["url"]
    uid = f"{callback.from_user.id}_{int(time.time() * 1000)}"

    await callback.answer("تم استلام طلبك...")
    status = await callback.message.edit_text("⏳ في قائمة الانتظار للتحميل...")

    async with DOWNLOAD_SEMAPHORE:
        try:
            await status.edit_text("⬇️ جاري التحميل والمعالجة من يوتيوب...")
            media = await asyncio.to_thread(download_media_sync, url, mode, uid)

            await status.edit_text("⬆️ جاري الرفع إلى تيليجرام...")
            filepath = media["filepath"]
            caption = f"🎬 **{media['title']}**"

            if mode == "audio":
                await client.send_audio(
                    chat_id=callback.message.chat.id,
                    audio=filepath,
                    caption=caption,
                    title=media["title"],
                    performer=media["performer"],
                    duration=media["duration"],
                )
            else:
                await client.send_video(
                    chat_id=callback.message.chat.id,
                    video=filepath,
                    caption=caption,
                    duration=media["duration"],
                    supports_streaming=True,
                )

            await status.delete()
        except Exception as e:
            logger.error(f"Download error: {e}")
            await status.edit_text(f"❌ فشل التحميل:\n`{e}`")
        finally:
            cleanup_files(uid)


if __name__ == "__main__":
    logger.info("Starting RA7IM YouTube Bot...")
    app.run()

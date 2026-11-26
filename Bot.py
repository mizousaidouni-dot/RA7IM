"""
بوت أغاني تيليجرام (نسخة MTProto: يستخدم API ID و API HASH)
- بحث يوتيوب عبر yt-dlp (مع ملف الكوكيز)
- بطاقة بصورة المقطع + زر تحميل، ثم اختيار: صوت MP3 أو فيديو MP4
- الحد الأقصى للمدة: 15 دقيقة
- رفع حتى حوالي 2GB (بدل 50MB) بفضل MTProto
- شريط تقدم للرفع

المتطلبات: Python 3.10+  و  ffmpeg
التشغيل:
    export API_ID="1234567"
    export API_HASH="abcdef0123456789abcdef0123456789"
    export BOT_TOKEN="123456:ABC..."
    python bot.py
"""

import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

import yt_dlp
from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ───────────────────────── الإعدادات ─────────────────────────
BASE_DIR = Path(__file__).resolve().parent

# يمكنك وضع القيم هنا مباشرة بدل متغيرات البيئة
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8876596447:AAGLbK3QYg4DcED9RzyLwHZvuzrJevmSrKs")

# مكان ملف الكوكيز: بنفس مجلد bot.py باسم cookies.txt (أو غيّره بالمتغير COOKIES_FILE)
COOKIES_FILE = Path(os.getenv("COOKIES_FILE", str(BASE_DIR / "cookies.txt")))

MAX_DURATION = 15 * 60                      # 15 دقيقة
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "700"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024  # الحد الأقصى لتيليجرام ~2000MB
RESULTS_COUNT = 6
MAX_PARALLEL_DOWNLOADS = 3

THUMB_URL = "https://i.ytimg.com/vi/{}/hqdefault.jpg"
COLORS = ["🟢", "🔵", "🟣", "🟠", "🔴", "🟡"]
VID_RE = re.compile(r"^[\w-]{11}$")

VIDEO_FORMAT = (
    "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
    "b[height<=720][ext=mp4]/"
    "bv*[height<=720]+ba/b[height<=720]/b"
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("music-bot")

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit(
        "ناقص إعدادات! لازم تحدد API_ID و API_HASH و BOT_TOKEN.\n"
        "API_ID و API_HASH من: https://my.telegram.org  ->  API development tools"
    )

app = Client(
    "music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(BASE_DIR),
    parse_mode=ParseMode.HTML,
)

DOWNLOAD_SEM = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
BUSY: set[int] = set()
CACHE: "OrderedDict[str, dict]" = OrderedDict()   # معلومات نتائج البحث


# ───────────────────────── أدوات مساعدة ─────────────────────────
def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def fmt_dur(seconds) -> str:
    if not seconds:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def cache_put(items: list[dict]) -> None:
    for r in items:
        CACHE[r["id"]] = r
        CACHE.move_to_end(r["id"])
    while len(CACHE) > 1000:
        CACHE.popitem(last=False)


def base_opts(workdir: Path) -> dict:
    """إعدادات yt-dlp المشتركة. تُنسخ الكوكيز لمجلد مؤقت لتفادي التعارض."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    if COOKIES_FILE.exists():
        tmp_cookies = workdir / "cookies.txt"
        shutil.copy(COOKIES_FILE, tmp_cookies)
        opts["cookiefile"] = str(tmp_cookies)
    return opts


# ───────────────────────── البحث والتحميل (يعملان في Thread) ─────────────────────────
def search_youtube(query: str) -> list[dict]:
    workdir = Path(tempfile.mkdtemp(prefix="ytsearch_"))
    try:
        opts = base_opts(workdir)
        opts.update({"extract_flat": True, "skip_download": True})
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch12:{query}", download=False)

        results = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            vid = entry.get("id")
            dur = entry.get("duration")
            if not vid or not VID_RE.match(vid):
                continue
            if not dur or dur > MAX_DURATION:
                continue
            results.append(
                {
                    "id": vid,
                    "title": entry.get("title") or "بدون عنوان",
                    "duration": dur,
                    "channel": entry.get("channel") or entry.get("uploader") or "",
                }
            )
            if len(results) >= RESULTS_COUNT:
                break
        return results
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def download_media(video_id: str, kind: str, workdir: Path) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = base_opts(workdir)
    opts.update(
        {
            "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "max_filesize": MAX_FILE_BYTES,
        }
    )
    if kind == "a":
        opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
        wanted_ext = ".mp3"
    else:
        opts.update({"format": VIDEO_FORMAT, "merge_output_format": "mp4"})
        wanted_ext = ".mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = info.get("duration") or 0
        if duration > MAX_DURATION:
            raise ValueError("too_long")
        ydl.process_ie_result(info, download=True)

    files = [p for p in workdir.glob(f"{video_id}*") if p.suffix == wanted_ext]
    if not files:
        raise FileNotFoundError("too_big_or_missing")
    path = files[0]
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("too_big")

    return {
        "path": path,
        "title": info.get("title") or "Unknown",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": duration,
    }


# ───────────────────────── النصوص ولوحات الأزرار ─────────────────────────
WELCOME = (
    "🎧 <b>أهلاً بك في بوت الأغاني!</b>\n\n"
    "أرسل لي اسم أي أغنية أو فنان وأنا أجيبلك النتائج.\n"
    "بعدها اضغط <b>تحميل ⬇️</b> واختار تستلمها "
    "<b>ملف صوتي 🎵</b> أو <b>فيديو 🎬</b>.\n\n"
    "⏱ الحد الأقصى للمقطع: 15 دقيقة\n"
    "✍️ جرّب الآن: اكتب اسم أغنية 👇"
)

HELP = (
    "ℹ️ <b>طريقة الاستخدام</b>\n\n"
    "1️⃣ أرسل اسم الأغنية أو الفنان\n"
    "2️⃣ اختر النتيجة من الأزرار\n"
    "3️⃣ اضغط زر التحميل ⬇️ تحت الصورة\n"
    "4️⃣ اختر صوت MP3 أو فيديو MP4\n"
    "5️⃣ استلم الملف وحمّله أو شغّله مباشرة\n\n"
    "⚠️ حمّل فقط المحتوى الذي تملك حق استخدامه (محتواك، "
    "Creative Commons، الملك العام). المسؤولية على المستخدم."
)


def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 كيف أستخدم البوت؟", callback_data="help")],
            [InlineKeyboardButton("🔴 إغلاق", callback_data="close")],
        ]
    )


def kb_card(vid: str) -> InlineKeyboardMarkup:
    """الأزرار تحت صورة المقطع."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 تحميل ⬇️", callback_data=f"ask:{vid}")],
            [InlineKeyboardButton("🔴 إلغاء", callback_data="close")],
        ]
    )


def kb_formats(vid: str) -> InlineKeyboardMarkup:
    """تظهر بعد الضغط على تحميل: اختيار الصيغة."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 ملف صوتي MP3 🎧", callback_data=f"dl:a:{vid}")],
            [InlineKeyboardButton("🔵 فيديو MP4 🎬", callback_data=f"dl:v:{vid}")],
            [InlineKeyboardButton("🟡 رجوع", callback_data=f"back:{vid}")],
        ]
    )


def card_caption(vid: str) -> str:
    info = CACHE.get(vid, {})
    return (
        f"🎵 <b>{esc(info.get('title', 'الأغنية'))}</b>\n"
        f"👤 {esc(info.get('channel', ''))}\n"
        f"⏱ {fmt_dur(info.get('duration'))}"
    )


# ───────────────────────── الأوامر ─────────────────────────
@app.on_message(filters.private & filters.command("start"))
async def cmd_start(client: Client, message: Message):
    await message.reply_text(WELCOME, reply_markup=kb_start())


@app.on_message(filters.private & filters.command("help"))
async def cmd_help(client: Client, message: Message):
    await message.reply_text(HELP)


@app.on_message(filters.private & filters.text & ~filters.command(["start", "help"]))
async def on_text(client: Client, message: Message):
    query = (message.text or "").strip()
    if query.startswith("/") or len(query) < 2:
        return
    query = query[:100]

    status = await message.reply_text("🔎 جاري البحث...", quote=True)
    try:
        results = await asyncio.to_thread(search_youtube, query)
    except Exception:
        log.exception("search failed")
        await status.edit_text("⚠️ ما قدرت أبحث الآن، حاول مرة ثانية بعد شوي.")
        return

    if not results:
        await status.edit_text(
            "😕 ما لقيت نتائج مناسبة (أقل من 15 دقيقة). جرّب اسم ثاني."
        )
        return

    cache_put(results)

    rows = []
    for i, r in enumerate(results):
        label = f"{COLORS[i % len(COLORS)]} {r['title'][:38]} • {fmt_dur(r['duration'])}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pick:{r['id']}")])
    rows.append([InlineKeyboardButton("❌ إغلاق", callback_data="close")])

    await status.edit_text(
        f"🎶 نتائج البحث عن: <b>{esc(query)}</b>\nاختر الأغنية 👇",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ───────────────────────── الأزرار ─────────────────────────
async def safe_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


async def upload_progress(current, total, status: Message, state: dict):
    now = time.monotonic()
    if current < total and now - state["t"] < 4:
        return
    state["t"] = now
    pct = int(current * 100 / total) if total else 0
    try:
        await status.edit_text(f"📤 جاري الرفع... {pct}%")
    except Exception:
        pass


@app.on_callback_query()
async def on_button(client: Client, q: CallbackQuery):
    data = q.data or ""
    if isinstance(data, bytes):
        data = data.decode()

    # إغلاق
    if data == "close":
        await q.answer()
        await safe_delete(q.message)
        return

    # مساعدة
    if data == "help":
        await q.answer()
        await q.message.reply_text(HELP)
        return

    # اختيار أغنية → بطاقة بصورة المقطع + زر تحميل
    if data.startswith("pick:"):
        vid = data.split(":", 1)[1]
        if not VID_RE.match(vid):
            await q.answer()
            return
        await q.answer()
        chat_id = q.message.chat.id
        await safe_delete(q.message)
        caption = card_caption(vid)
        try:
            await client.send_photo(
                chat_id,
                THUMB_URL.format(vid),
                caption=caption,
                reply_markup=kb_card(vid),
            )
        except Exception:
            await client.send_message(chat_id, caption, reply_markup=kb_card(vid))
        return

    # زر تحميل → إظهار اختيار الصيغة تحت الصورة
    if data.startswith("ask:"):
        vid = data.split(":", 1)[1]
        if not VID_RE.match(vid):
            await q.answer()
            return
        await q.answer()
        await q.edit_message_reply_markup(kb_formats(vid))
        return

    # رجوع
    if data.startswith("back:"):
        vid = data.split(":", 1)[1]
        if not VID_RE.match(vid):
            await q.answer()
            return
        await q.answer()
        await q.edit_message_reply_markup(kb_card(vid))
        return

    # تحميل وإرسال
    if data.startswith("dl:"):
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[1] not in ("a", "v") or not VID_RE.match(parts[2]):
            await q.answer()
            return
        _, kind, vid = parts

        uid = q.from_user.id
        if uid in BUSY:
            await q.answer("⏳ عندك تحميل شغال، انتظر لين ينتهي.", show_alert=True)
            return

        await q.answer("بدأ التحميل ⏳")
        BUSY.add(uid)
        chat_id = q.message.chat.id
        await safe_delete(q.message)
        status = await client.send_message(chat_id, "⏳ جاري التحميل... انتظر قليلاً")
        workdir = Path(tempfile.mkdtemp(prefix="ytdl_"))

        try:
            async with DOWNLOAD_SEM:
                await client.send_chat_action(
                    chat_id,
                    ChatAction.UPLOAD_AUDIO if kind == "a" else ChatAction.UPLOAD_VIDEO,
                )
                result = await asyncio.to_thread(download_media, vid, kind, workdir)

            await status.edit_text("📤 جاري الرفع... 0%")
            bot_name = client.me.username if client.me else ""
            caption = (
                f"🎵 <b>{esc(result['title'])}</b>\n"
                f"⏱ {fmt_dur(result['duration'])}\n\n"
                f"🤖 @{bot_name}"
            )
            state = {"t": 0.0}
            if kind == "a":
                await client.send_audio(
                    chat_id,
                    str(result["path"]),
                    caption=caption,
                    title=result["title"][:64],
                    performer=result["uploader"][:64],
                    duration=int(result["duration"]),
                    progress=upload_progress,
                    progress_args=(status, state),
                )
            else:
                await client.send_video(
                    chat_id,
                    str(result["path"]),
                    caption=caption,
                    duration=int(result["duration"]),
                    supports_streaming=True,
                    progress=upload_progress,
                    progress_args=(status, state),
                )
            await safe_delete(status)

        except ValueError as e:
            if str(e) == "too_long":
                text = "⛔ هذا المقطع أطول من 15 دقيقة."
            else:
                text = (
                    f"⛔ الملف أكبر من {MAX_FILE_MB}MB.\n"
                    "جرّب صيغة الصوت MP3 لأنها أخف."
                )
            await status.edit_text(text)
        except FileNotFoundError:
            await status.edit_text(
                f"⛔ الملف أكبر من {MAX_FILE_MB}MB أو ما انحمّل.\n"
                "جرّب صيغة الصوت MP3."
            )
        except yt_dlp.utils.DownloadError as e:
            log.warning("download error: %s", e)
            msg = str(e).lower()
            if "sign in" in msg or "cookies" in msg or "bot" in msg:
                text = "⚠️ يوتيوب طلب تسجيل دخول. حدّث ملف cookies.txt وجرب من جديد."
            else:
                text = "⚠️ ما قدرت أحمّل هذا المقطع (ممكن غير متاح). جرب نتيجة ثانية."
            await status.edit_text(text)
        except Exception:
            log.exception("unexpected error")
            await status.edit_text("⚠️ صار خطأ غير متوقع، حاول مرة ثانية.")
        finally:
            BUSY.discard(uid)
            shutil.rmtree(workdir, ignore_errors=True)
        return

    await q.answer()


# ───────────────────────── التشغيل ─────────────────────────
if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg غير مثبت. ثبّته أولاً (مطلوب للتحويل).")
    if COOKIES_FILE.exists():
        log.info("تم العثور على الكوكيز: %s", COOKIES_FILE)
    else:
        log.warning("ملف الكوكيز غير موجود (%s) - سيعمل البوت بدونه.", COOKIES_FILE)
    log.info("Bot is running...")
    app.run()

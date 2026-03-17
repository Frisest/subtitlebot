import logging
import os
import json
import asyncio
import base64
import urllib.request
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from processor import process_video

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── .env ───────────────────────────────────────────────────
def load_env():
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if not os.getenv(k):
                    os.environ[k] = v
load_env()

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
API_PORT   = int(os.getenv("API_PORT", "8765"))

DOWNLOAD_DIR  = "downloads"
OUTPUT_DIR    = "outputs"
SETTINGS_FILE = "user_settings.json"
FONTS_DIR     = "fonts"
# fonts.json генерируется ботом и должен лежать рядом с webapp.html
# Путь к папке где лежит webapp.html (локальная копия репозитория)
WEBAPP_DIR    = os.getenv("WEBAPP_DIR", ".")  # по умолчанию та же папка

ADMIN_USERS: set[int] = {
    int(x) for x in os.getenv("ADMIN_USERS", "777325110").split(",") if x.strip()
}

for d in [DOWNLOAD_DIR, OUTPUT_DIR, FONTS_DIR]:
    os.makedirs(d, exist_ok=True)

DEFAULT_SETTINGS = {
    "posX": 50, "posY": 88,
    "fontSize": 22,
    "fontWeight": "normal",
    "bgStyle": "none",
    "color": "#ffffff",
    "maxWords": 5,
    "zone": {"left": 5, "right": 5, "top": 5, "bottom": 5},
    "fontName": "Arial",
}

censor_users: set[int] = set()

# ── Настройки ──────────────────────────────────────────────
def load_all() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_all(d: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

all_settings: dict = load_all()

def get_settings(uid: int) -> dict:
    return all_settings.get(str(uid), DEFAULT_SETTINGS)

def set_settings(uid: int, data: dict):
    all_settings[str(uid)] = data
    save_all(all_settings)

# ── Шрифты ─────────────────────────────────────────────────
BUILTIN_FONTS = ["Arial", "Impact", "Times New Roman", "Courier New", "Verdana", "Georgia"]

def get_custom_fonts() -> list[dict]:
    fonts = []
    for fname in sorted(os.listdir(FONTS_DIR)):
        if fname.lower().endswith((".ttf", ".otf")):
            name = os.path.splitext(fname)[0]
            fonts.append({"name": name, "file": fname})
    return fonts

def get_all_fonts() -> list[dict]:
    builtin = [{"name": n, "file": None} for n in BUILTIN_FONTS]
    return builtin + get_custom_fonts()

def rebuild_fonts_json():
    """
    Генерирует fonts.json с base64 кастомных шрифтов.
    Кладёт файл рядом с webapp.html (WEBAPP_DIR).
    После этого нужно сделать git push чтобы обновить GitHub Pages.
    """
    fonts = []
    for fname in sorted(os.listdir(FONTS_DIR)):
        if fname.lower().endswith((".ttf", ".otf")):
            fpath = os.path.join(FONTS_DIR, fname)
            try:
                with open(fpath, "rb") as f:
                    data = base64.b64encode(f.read()).decode("ascii")
                name = os.path.splitext(fname)[0]
                ext  = fname.rsplit(".", 1)[-1].lower()
                mime = "font/otf" if ext == "otf" else "font/ttf"
                fonts.append({
                    "name":    name,
                    "file":    fname,
                    "dataUrl": f"data:{mime};base64,{data}"
                })
            except Exception as e:
                logger.error(f"Font read error {fname}: {e}")

    out_path = os.path.join(WEBAPP_DIR, "fonts.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"fonts": fonts}, f)
    logger.info(f"fonts.json обновлён: {len(fonts)} шрифтов → {out_path}")
    return fonts

def git_push_fonts():
    """Делает git add/commit/push для fonts.json автоматически."""
    import subprocess
    try:
        subprocess.run(["git", "-C", WEBAPP_DIR, "add", "fonts.json"], check=True, capture_output=True)
        subprocess.run(["git", "-C", WEBAPP_DIR, "commit", "-m", "update fonts"], check=True, capture_output=True)
        subprocess.run(["git", "-C", WEBAPP_DIR, "push"], check=True, capture_output=True)
        return True
    except Exception as e:
        logger.error(f"git push error: {e}")
        return False

# ── HTTP API (для /save настроек) ──────────────────────────
async def handle_fonts_api(request: web.Request) -> web.Response:
    fonts = []
    for fname in sorted(os.listdir(FONTS_DIR)):
        if fname.lower().endswith((".ttf", ".otf")):
            fpath = os.path.join(FONTS_DIR, fname)
            try:
                with open(fpath, "rb") as f:
                    data = base64.b64encode(f.read()).decode("ascii")
                name = os.path.splitext(fname)[0]
                ext  = fname.rsplit(".", 1)[-1].lower()
                mime = "font/otf" if ext == "otf" else "font/ttf"
                fonts.append({"name": name, "file": fname,
                               "dataUrl": f"data:{mime};base64,{data}"})
            except Exception:
                pass
    return web.json_response(
        {"fonts": fonts},
        headers={"Access-Control-Allow-Origin": "*"}
    )

async def handle_save(request: web.Request) -> web.Response:
    try:
        body    = await request.json()
        user_id = int(body["user_id"])
        set_settings(user_id, body["settings"])
        logger.info(f"Settings saved via API for {user_id}")
        return web.json_response({"ok": True},
                                 headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400,
                                 headers={"Access-Control-Allow-Origin": "*"})

async def handle_cors(request: web.Request) -> web.Response:
    return web.Response(headers={
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    })

async def start_api_server():
    app = web.Application()
    app.router.add_get( "/fonts",  handle_fonts_api)
    app.router.add_post("/save",   handle_save)
    app.router.add_route("OPTIONS", "/save",  handle_cors)
    app.router.add_route("OPTIONS", "/fonts", handle_cors)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
    logger.info(f"API server on port {API_PORT}")

# ── Команды ────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я добавляю субтитры на видео.\n\n"
        "📤 Отправь видеофайл — получишь его с субтитрами.\n\n"
        "⚙️ /settings — настроить вид субтитров\n"
        "🔞 /cens on/off — цензура матов\n"
        "📋 /list — список шрифтов"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Команды:\n\n"
        "/settings       — позиция, размер, цвет, шрифт субтитров\n"
        "/list           — список доступных шрифтов\n"
        "/load           — загрузить шрифт (только админ)\n"
        "/cens on/off    — цензура матов\n"
        "/applysettings  — применить настройки из WebApp\n"
        "/help           — эта справка"
    )

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    s   = get_settings(uid)
    if not WEBAPP_URL:
        await update.message.reply_text("⚙️ WEBAPP_URL не задан в .env")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎨 Открыть настройки", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text(
        f"⚙️ Настройки:\n"
        f"📍 {s['posX']}% / {s['posY']}%  "
        f"🔤 {s['fontSize']}px  "
        f"🎨 {s['color']}  "
        f"🅰 {s.get('fontName','Arial')}\n\n"
        f"Нажми кнопку 👇",
        reply_markup=keyboard
    )

async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    try:
        data = json.loads(update.message.web_app_data.data)
        set_settings(uid, data)
        await update.message.reply_text(
            f"✅ Настройки сохранены!\n"
            f"📍 {data['posX']}% / {data['posY']}%  "
            f"🔤 {data['fontSize']}px  "
            f"🎨 {data['color']}  "
            f"🅰 {data.get('fontName','Arial')}"
        )
    except Exception as e:
        logger.error(f"WebApp data error: {e}")

async def applysettings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not context.args:
        s = get_settings(uid)
        await update.message.reply_text(
            f"Текущие настройки:\n<code>{json.dumps(s, ensure_ascii=False)}</code>",
            parse_mode="HTML"
        )
        return
    try:
        data = json.loads(" ".join(context.args))
        set_settings(uid, data)
        await update.message.reply_text(
            f"✅ Настройки применены!\n"
            f"📍 {data['posX']}% / {data['posY']}%  🔤 {data['fontSize']}px  🅰 {data.get('fontName','Arial')}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def load_font_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in ADMIN_USERS:
        await update.message.reply_text("❌ Нет прав для загрузки шрифтов.")
        return
    context.user_data["waiting_font"] = True
    await update.message.reply_text(
        "📎 Отправь файл шрифта (.ttf или .otf)\n\n"
        "После загрузки шрифт появится у всех пользователей автоматически."
    )

async def list_fonts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    custom = get_custom_fonts()
    lines  = ["🔤 Доступные шрифты:\n", "Встроенные:"]
    for n in BUILTIN_FONTS:
        lines.append(f"  • {n}")
    if custom:
        lines.append(f"\nЗагруженные ({len(custom)}):")
        for f in custom:
            lines.append(f"  ✨ {f['name']}")
    else:
        lines.append("\nЗагруженных шрифтов пока нет.")
    if update.message.from_user.id in ADMIN_USERS:
        lines.append("\n/load — загрузить новый шрифт")
    await update.message.reply_text("\n".join(lines))

async def handle_font_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in ADMIN_USERS or not context.user_data.get("waiting_font"):
        return
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Отправь файл шрифта.")
        return
    fname = doc.file_name or "font.ttf"
    ext   = os.path.splitext(fname)[1].lower()
    if ext not in (".ttf", ".otf"):
        await update.message.reply_text("❌ Только .ttf и .otf файлы.")
        return
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("❌ Максимум 10 МБ.")
        return

    status = await update.message.reply_text("⏳ Загружаю шрифт...")
    try:
        tg_file   = await context.bot.get_file(doc.file_id)
        save_path = os.path.join(FONTS_DIR, fname)
        await tg_file.download_to_drive(save_path)
        context.user_data["waiting_font"] = False
        font_name = os.path.splitext(fname)[0]

        # Обновляем fonts.json и пушим на GitHub
        await status.edit_text("📦 Обновляю fonts.json...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, rebuild_fonts_json)

        pushed = await loop.run_in_executor(None, git_push_fonts)
        if pushed:
            await status.edit_text(
                f"✅ Шрифт загружен: {font_name}\n\n"
                f"fonts.json обновлён и залит на GitHub.\n"
                f"Через 1-2 минуты шрифт появится в WebApp.\n\n"
                f"/list — посмотреть все шрифты"
            )
        else:
            await status.edit_text(
                f"✅ Шрифт загружен: {font_name}\n\n"
                f"⚠️ Автопуш на GitHub не удался.\n"
                f"Запусти вручную:\n"
                f"<code>git add fonts.json && git commit -m fonts && git push</code>",
                parse_mode="HTML"
            )
        logger.info(f"Font uploaded: {fname} by {uid}")
    except Exception as e:
        logger.error(f"Font upload error: {e}")
        await status.edit_text(f"❌ Ошибка: {e}")

async def cens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.message.from_user.id
    args = context.args
    if not args:
        st = "✅ включена" if uid in censor_users else "❌ выключена"
        await update.message.reply_text(f"🔞 Цензура: {st}\n\n/cens on — включить\n/cens off — выключить")
        return
    if args[0].lower() == "on":
        censor_users.add(uid)
        await update.message.reply_text("✅ Цензура включена!")
    elif args[0].lower() == "off":
        censor_users.discard(uid)
        await update.message.reply_text("❌ Цензура выключена.")
    else:
        await update.message.reply_text("Используй /cens on или /cens off")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message    = update.message
    uid        = message.from_user.id
    use_censor = uid in censor_users
    settings   = get_settings(uid)

    logger.info(f"Video from {uid} | font={settings.get('fontName')} size={settings.get('fontSize')} pos={settings.get('posX')},{settings.get('posY')}")

    if message.video:
        file      = message.video
        file_name = f"{uid}_{file.file_id}.mp4"
    elif message.document:
        file      = message.document
        ext       = os.path.splitext(file.file_name or "video.mp4")[1] or ".mp4"
        file_name = f"{uid}_{file.file_id}{ext}"
    else:
        return

    MAX_SIZE     = 20 * 1024 * 1024
    file_size_mb = round(file.file_size / 1024 / 1024, 1) if file.file_size else "?"

    if file.file_size and file.file_size > MAX_SIZE:
        await message.reply_text(
            f"❌ Файл {file_size_mb} МБ — максимум 20 МБ.\n"
            "Сожми через HandBrake или freeconvert.com"
        )
        return

    note       = f"({file_size_mb} МБ — сожму)" if file.file_size and file.file_size > 15*1024*1024 else f"({file_size_mb} МБ)"
    status_msg = await message.reply_text(f"⏳ Скачиваю видео {note}...")

    input_path  = os.path.join(DOWNLOAD_DIR, file_name)
    output_path = os.path.join(OUTPUT_DIR, f"sub_{file_name}")

    try:
        tg_file = await context.bot.get_file(file.file_id)
        await tg_file.download_to_drive(input_path)
        await status_msg.edit_text("🎙️ Транскрибирую... (1–5 мин)")

        loop = asyncio.get_event_loop()
        success, result = await loop.run_in_executor(
            None, process_video, input_path, output_path, use_censor, settings
        )

        if success:
            await status_msg.edit_text("📤 Отправляю...")
            caption = "✅ Субтитры добавлены."
            if use_censor:
                caption += " (маты зацензурены 🔞)"
            with open(output_path, "rb") as vf:
                await message.reply_video(video=vf, caption=caption, supports_streaming=True)
            await status_msg.delete()
        else:
            await status_msg.edit_text(f"❌ Ошибка: {result}")
    except Exception as e:
        logger.error(f"Error for {uid}: {e}")
        await status_msg.edit_text("❌ Что-то пошло не так. Попробуй ещё раз.")
    finally:
        for p in [input_path, output_path]:
            if os.path.exists(p):
                os.remove(p)

# ── Запуск ─────────────────────────────────────────────────
async def post_init(app):
    await start_api_server()

def main():
    if not BOT_TOKEN:
        print("[ERROR] BOT_TOKEN not set in .env!")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",         start_cmd))
    app.add_handler(CommandHandler("help",          help_cmd))
    app.add_handler(CommandHandler("settings",      settings_cmd))
    app.add_handler(CommandHandler("applysettings", applysettings_cmd))
    app.add_handler(CommandHandler("load",          load_font_cmd))
    app.add_handler(CommandHandler("list",          list_fonts_cmd))
    app.add_handler(CommandHandler("cens",          cens_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("ttf") | filters.Document.FileExtension("otf"),
        handle_font_upload
    ))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

import logging
import os
import json
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from processor import process_video

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")   # URL где лежит webapp.html — см. README

DOWNLOAD_DIR = "downloads"
OUTPUT_DIR   = "outputs"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Настройки по умолчанию (применяются если пользователь не открывал settings)
DEFAULT_SETTINGS = {
    "posX": 50, "posY": 88,
    "fontSize": 22,
    "fontWeight": "normal",
    "bgStyle": "none",
    "color": "#ffffff",
    "maxWords": 5,
    "zone": {"left": 5, "right": 5, "top": 5, "bottom": 5},
}

censor_users:   set[int]  = set()
user_settings:  dict[int, dict] = {}


def get_settings(user_id: int) -> dict:
    return user_settings.get(user_id, DEFAULT_SETTINGS)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я добавляю субтитры на видео.\n\n"
        "📤 Отправь видеофайл — получишь его обратно с субтитрами.\n\n"
        "⚙️ /settings — настроить вид субтитров\n"
        "🔞 /cens on/off — цензура матов\n"
        "❓ /help — справка"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Команды:\n\n"
        "/settings  — настроить позицию, размер, цвет субтитров\n"
        "/cens on   — включить цензуру матов\n"
        "/cens off  — выключить цензуру\n"
        "/help      — эта справка\n\n"
        "📦 Макс. размер файла: 20 МБ"
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WEBAPP_URL:
        await update.message.reply_text(
            "⚙️ Mini App ещё не настроен.\n\n"
            "Для активации нужно выложить webapp.html на хостинг и "
            "прописать WEBAPP_URL в переменных окружения.\n"
            "Подробнее — в README."
        )
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🎨 Настроить субтитры",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]])
    s = get_settings(update.message.from_user.id)
    await update.message.reply_text(
        f"⚙️ Текущие настройки:\n"
        f"📍 Позиция: {s['posX']}% / {s['posY']}%\n"
        f"🔤 Шрифт: {s['fontSize']}px\n"
        f"🎨 Цвет: {s['color']}\n"
        f"📝 Слов в строке: {s['maxWords']}\n\n"
        f"Нажми кнопку чтобы изменить 👇",
        reply_markup=keyboard
    )


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем данные из Mini App когда пользователь нажал Сохранить."""
    user_id = update.message.from_user.id
    try:
        data = json.loads(update.message.web_app_data.data)
        user_settings[user_id] = data
        await update.message.reply_text(
            f"✅ Настройки сохранены!\n\n"
            f"📍 Позиция: {data['posX']}% / {data['posY']}%\n"
            f"🔤 Размер шрифта: {data['fontSize']}px\n"
            f"🎨 Цвет: {data['color']}\n"
            f"📝 Слов в строке: {data['maxWords']}\n"
            f"🖼 Фон текста: {data['bgStyle']}\n\n"
            f"Следующее видео будет обработано с этими настройками."
        )
    except Exception as e:
        logger.error(f"Ошибка WebApp данных: {e}")
        await update.message.reply_text("❌ Не удалось сохранить настройки.")


async def cens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    args = context.args
    if not args:
        status = "✅ включена" if user_id in censor_users else "❌ выключена"
        await update.message.reply_text(f"🔞 Цензура матов: {status}\n\n/cens on — включить\n/cens off — выключить")
        return
    cmd = args[0].lower()
    if cmd == "on":
        censor_users.add(user_id)
        await update.message.reply_text("✅ Цензура включена! Маты заменятся: бл@ть, х#й, п*здец...")
    elif cmd == "off":
        censor_users.discard(user_id)
        await update.message.reply_text("❌ Цензура выключена.")
    else:
        await update.message.reply_text("Используй /cens on или /cens off")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message  = update.message
    user_id  = message.from_user.id
    use_censor = user_id in censor_users
    settings   = get_settings(user_id)

    if message.video:
        file = message.video
        file_name = f"{user_id}_{file.file_id}.mp4"
    elif message.document:
        file = message.document
        ext  = os.path.splitext(file.file_name or "video.mp4")[1] or ".mp4"
        file_name = f"{user_id}_{file.file_id}{ext}"
    else:
        return

    MAX_SIZE = 20 * 1024 * 1024
    file_size_mb = round(file.file_size / 1024 / 1024, 1) if file.file_size else "?"

    if file.file_size and file.file_size > MAX_SIZE:
        await message.reply_text(
            f"❌ Файл слишком большой: {file_size_mb} МБ\n\n"
            "📦 Telegram позволяет скачивать файлы только до 20 МБ.\n\n"
            "💡 Сожми видео:\n"
            "• HandBrake: https://handbrake.fr\n"
            "• Онлайн: https://www.freeconvert.com/video-compressor"
        )
        return

    size_note   = f"({file_size_mb} МБ — сожму перед обработкой)" if file.file_size and file.file_size > 15*1024*1024 else f"({file_size_mb} МБ)"
    censor_note = " | 🔞 цензура" if use_censor else ""
    status_msg  = await message.reply_text(f"⏳ Скачиваю видео {size_note}{censor_note}...")

    input_path  = os.path.join(DOWNLOAD_DIR, file_name)
    output_path = os.path.join(OUTPUT_DIR, f"subtitled_{file_name}")

    try:
        tg_file = await context.bot.get_file(file.file_id)
        await tg_file.download_to_drive(input_path)
        await status_msg.edit_text("🎙️ Распознаю речь и нарезаю субтитры... (1-5 минут)")

        loop = asyncio.get_event_loop()
        success, result = await loop.run_in_executor(
            None, process_video, input_path, output_path, use_censor, settings
        )

        if success:
            await status_msg.edit_text("📤 Отправляю...")
            caption = "✅ Субтитры добавлены."
            if use_censor: caption += " (маты зацензурены 🔞)"
            with open(output_path, "rb") as vf:
                await message.reply_video(video=vf, caption=caption, supports_streaming=True)
            await status_msg.delete()
        else:
            await status_msg.edit_text(f"❌ Ошибка: {result}")

    except Exception as e:
        logger.error(f"Ошибка для {user_id}: {e}")
        await status_msg.edit_text("❌ Что-то пошло не так. Попробуй ещё раз.")
    finally:
        for path in [input_path, output_path]:
            if os.path.exists(path): os.remove(path)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("cens",     cens_command))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

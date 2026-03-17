# 🎬 Telegram Subtitle Bot

Бот автоматически добавляет субтитры на видео через Whisper (распознавание речи) и FFmpeg.

---

## 📋 Требования

- Python 3.10+
- FFmpeg установлен в системе
- Токен Telegram бота (получить у @BotFather)

---

## 🚀 Установка

### 1. Установи FFmpeg

**Windows:**
```
winget install ffmpeg
```
или скачай с https://ffmpeg.org/download.html и добавь в PATH

**macOS:**
```
brew install ffmpeg
```

**Linux:**
```
sudo apt install ffmpeg
```

---

### 2. Создай виртуальное окружение и установи зависимости

```bash
python -m venv venv

# Windows:
venv\Scripts\activate

# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

При первом запуске Whisper сам скачает модель (~500 МБ для small).

---

### 3. Получи токен бота

1. Открой @BotFather в Telegram
2. Напиши `/newbot`
3. Следуй инструкциям
4. Скопируй токен вида `123456:ABC-DEF...`

---

### 4. Задай токен

**Способ 1 — переменная окружения (рекомендуется):**
```bash
# Windows:
set BOT_TOKEN=123456:ABC-DEF...

# macOS/Linux:
export BOT_TOKEN=123456:ABC-DEF...
```

**Способ 2 — прямо в коде:**
В файле `bot.py` замени строку:
```python
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
```

---

### 5. Запусти бота

```bash
python bot.py
```

---

## ⚙️ Настройки

В файле `processor.py` можно менять модель Whisper:

| Модель  | Скорость | Качество | RAM     |
|---------|----------|----------|---------|
| tiny    | ⚡⚡⚡⚡   | ★★☆☆☆   | ~1 ГБ   |
| base    | ⚡⚡⚡    | ★★★☆☆   | ~1 ГБ   |
| small   | ⚡⚡      | ★★★★☆   | ~2 ГБ   |
| medium  | ⚡        | ★★★★★   | ~5 ГБ   |
| large   | 🐢        | ★★★★★   | ~10 ГБ  |

Рекомендация: начни с `small`, это хорошее соотношение качества/скорости.

---

## 📁 Структура проекта

```
subtitle_bot/
├── bot.py          — основной файл бота
├── processor.py    — обработка видео (Whisper + FFmpeg)
├── requirements.txt
├── downloads/      — временные входящие файлы (создаётся автоматически)
└── outputs/        — временные выходные файлы (создаётся автоматически)
```

---

## ❓ Частые проблемы

**"ffmpeg not found"** — убедись что FFmpeg добавлен в PATH и перезапусти терминал.

**Бот не реагирует на видео** — попробуй отправить файл как документ (скрепка → файл).

**Медленная обработка** — переключись на модель `tiny` или `base`.

**Плохое качество субтитров** — переключись на модель `medium` или `large`.


---

## ⚙️ Mini App (настройки субтитров)

Чтобы кнопка /settings открывала интерфейс — нужно выложить `webapp.html` на HTTPS хостинг.

### Бесплатные варианты:

**GitHub Pages (самый простой):**
1. Создай репозиторий на github.com
2. Закинь туда `webapp.html`
3. Зайди в Settings → Pages → Source: main branch
4. Получишь URL вида `https://username.github.io/repo/webapp.html`
5. Пропиши в переменных окружения:
   ```
   set WEBAPP_URL=https://username.github.io/repo/webapp.html
   ```

**Netlify Drop (ещё проще):**
1. Зайди на netlify.com/drop
2. Перетащи `webapp.html`
3. Получишь URL вида `https://xxxxxx.netlify.app/webapp.html`

### Важно:
- URL обязательно должен быть HTTPS (не http)
- Telegram не открывает Mini App по http

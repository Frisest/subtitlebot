import os
import subprocess
import logging
from faster_whisper import WhisperModel
from censor import censor_segments

logger = logging.getLogger(__name__)

WHISPER_MODEL    = os.getenv("WHISPER_MODEL", "medium")
MAX_WORDS_PER_SUB = int(os.getenv("MAX_WORDS_PER_SUB", "5"))
COMPRESS_THRESHOLD = 20 * 1024 * 1024

logger.info(f"Загружаю модель Whisper: {WHISPER_MODEL}...")
model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
logger.info("Модель загружена.")


# ── Сжатие ─────────────────────────────────────────────────
def compress_video(input_path: str, output_path: str, target_mb: int = 18) -> bool:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    if probe.returncode != 0:
        return False
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        return False

    target_bits  = target_mb * 8 * 1024 * 1024
    audio_bitrate = 128 * 1024
    video_bitrate = max(100_000, int((target_bits / duration) - audio_bitrate))
    passlog = output_path + "_passlog"

    r1 = subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-b:v", str(video_bitrate),
        "-pass", "1", "-passlogfile", passlog,
        "-an", "-f", "null", os.devnull
    ], capture_output=True, text=True)
    if r1.returncode != 0:
        return False

    r2 = subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-b:v", str(video_bitrate),
        "-pass", "2", "-passlogfile", passlog,
        "-c:a", "aac", "-b:a", "128k", output_path
    ], capture_output=True, text=True)

    for ext in ["-0.log", "-0.log.mbtree"]:
        p = passlog + ext
        if os.path.exists(p):
            os.remove(p)

    if r2.returncode != 0:
        logger.error(f"Сжатие не удалось: {r2.stderr[-200:]}")
        return False

    logger.info(f"Сжато до {os.path.getsize(output_path)/1024/1024:.1f} МБ")
    return True


# ── Извлечение аудио ────────────────────────────────────────
def extract_audio(video_path: str, audio_path: str) -> bool:
    r = subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path
    ], capture_output=True, text=True)
    if r.returncode != 0:
        logger.error(f"FFmpeg audio: {r.stderr[-200:]}")
        return False
    return True


# ── Транскрипция ────────────────────────────────────────────
def transcribe(audio_path: str) -> list[dict]:
    segments, info = model.transcribe(
        audio_path,
        language=None,
        beam_size=5,
        best_of=5,
        patience=1.0,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400, speech_pad_ms=200),
        word_timestamps=True,
        temperature=0.0,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
    )
    logger.info(f"Язык: {info.language} ({info.language_probability:.0%})")

    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                word = w.word.strip()
                if word:
                    words.append({"word": word, "start": w.start, "end": w.end})
    logger.info(f"Слов: {len(words)}")
    return words


# ── Нарезка слов на субтитры ────────────────────────────────
def words_to_segments(words: list[dict], max_words: int = 5) -> list[dict]:
    if not words:
        return []
    segments, chunk = [], []
    for i, w in enumerate(words):
        if chunk and (w["start"] - words[i-1]["end"]) > 0.8:
            segments.append({
                "start": chunk[0]["start"],
                "end":   chunk[-1]["end"],
                "text":  " ".join(x["word"] for x in chunk)
            })
            chunk = []
        chunk.append(w)
        if len(chunk) >= max_words:
            segments.append({
                "start": chunk[0]["start"],
                "end":   chunk[-1]["end"],
                "text":  " ".join(x["word"] for x in chunk)
            })
            chunk = []
    if chunk:
        segments.append({
            "start": chunk[0]["start"],
            "end":   chunk[-1]["end"],
            "text":  " ".join(x["word"] for x in chunk)
        })
    return segments


# ── Конвертация цвета ───────────────────────────────────────
def hex_to_ass(hex_color: str) -> str:
    """#RRGGBB → &H00BBGGRR (ASS формат)"""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H00{b}{g}{r}".upper()
    return "&H00FFFFFF"


# ── Получение размеров видео ────────────────────────────────
def get_video_size(video_path: str) -> tuple[int, int]:
    """Возвращает (ширину, высоту) видео через ffprobe."""
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        video_path
    ], capture_output=True, text=True)
    if r.returncode == 0:
        try:
            parts = r.stdout.strip().split(",")
            return int(parts[0]), int(parts[1])
        except Exception:
            pass
    return 1080, 1920  # fallback


# ── Генерация ASS файла ─────────────────────────────────────
def segments_to_ass(segments: list[dict], settings: dict, vid_w: int, vid_h: int) -> str:
    r"""
    Генерирует ASS субтитры с точными координатами \pos(x,y).
    Это единственный способ задать произвольную позицию в FFmpeg.
    """
    s         = settings or {}
    pos_x_pct = float(s.get("posX", 50))   # % от ширины
    pos_y_pct = float(s.get("posY", 88))   # % от высоты
    font_size = int(s.get("fontSize", 22))
    font_name = s.get("fontName", "Arial")
    bold      = 1 if s.get("fontWeight") == "bold" else 0
    color     = hex_to_ass(s.get("color", "#ffffff"))
    bg_style  = s.get("bgStyle", "none")
    zone      = s.get("zone", {})

    # Учитываем зону безопасности
    zone_l = float(zone.get("left",   5)) / 100
    zone_r = float(zone.get("right",  5)) / 100
    zone_t = float(zone.get("top",    5)) / 100
    zone_b = float(zone.get("bottom", 5)) / 100

    # Рабочая область внутри зоны
    work_x0 = vid_w * zone_l
    work_y0 = vid_h * zone_t
    work_w  = vid_w * (1 - zone_l - zone_r)
    work_h  = vid_h * (1 - zone_t - zone_b)

    # Финальные координаты субтитра в пикселях видео
    sub_x = work_x0 + work_w * (pos_x_pct / 100)
    sub_y = work_y0 + work_h * (pos_y_pct / 100)

    # Выравнивание ASS: 1=низ-лево 2=низ-центр 3=низ-право
    #                   4=сред-лево 5=центр 6=сред-право
    #                   7=верх-лево 8=верх-центр 9=верх-право
    # Используем 2 (низ-центр) — \pos задаёт точку якоря
    alignment = 2

    shadow_strength = int(s.get("shadowStrength", 3))
    bg_opacity      = int(s.get("bgOpacity", 65))

    # В ASS: 00=непрозрачный, FF=полностью прозрачный
    alpha_hex = format(int((100 - bg_opacity) / 100 * 255), '02X').upper()

    if bg_style == "box":
        back_colour = f"&H{alpha_hex}000000"  # чёрный с настраиваемой прозрачностью
        border_style = 3
        outline      = 0
        shadow       = 0
    elif bg_style == "shadow":
        back_colour  = "&H00000000"
        border_style = 1
        outline      = 0
        shadow       = shadow_strength  # 1-10
    else:
        back_colour  = "&H00000000"
        border_style = 1
        outline      = 2
        shadow       = 1

    # MarginL/R для ограничения ширины текста зоной безопасности
    margin_l = int(work_x0)
    margin_r = int(vid_w - work_x0 - work_w)
    margin_v = 0  # вертикальный отступ — не нужен, используем \pos

    def fmt(sec: float) -> str:
        h   = int(sec // 3600)
        m   = int((sec % 3600) // 60)
        s_  = int(sec % 60)
        cs  = int((sec % 1) * 100)  # ASS использует сотые секунды
        return f"{h}:{m:02d}:{s_:02d}.{cs:02d}"

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {vid_w}",
        f"PlayResY: {vid_h}",
        "Collisions: Normal",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_name},{font_size},{color},&H00FFFFFF,&HFF000000,{back_colour},"
        f"{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},"
        f"{alignment},{margin_l},{margin_r},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # Если кастомный шрифт — добавляем его через [Fonts] секцию
    font_path = None
    fonts_dir = "fonts"
    if os.path.isdir(fonts_dir):
        for fname in os.listdir(fonts_dir):
            if os.path.splitext(fname)[0].lower() == font_name.lower():
                font_path = os.path.join(fonts_dir, fname)
                break

    if font_path and os.path.exists(font_path):
        import base64
        with open(font_path, "rb") as f_:
            encoded = base64.b64encode(f_.read()).decode("ascii")
        # Разбиваем на строки по 80 символов (стандарт ASS)
        chunks = [encoded[i:i+80] for i in range(0, len(encoded), 80)]
        lines += ["", "[Fonts]", f"fontname: {font_name}"] + chunks

    lines.append("")

    # Каждый субтитр с точными координатами \pos(x,y)
    pos_tag = f"{{\\pos({sub_x:.1f},{sub_y:.1f})}}"
    for seg in segments:
        text = seg["text"].replace("\n", "\\N")
        lines.append(
            f"Dialogue: 0,{fmt(seg['start'])},{fmt(seg['end'])},Default,,0,0,0,,{pos_tag}{text}"
        )

    return "\n".join(lines)




# ── Вшивание субтитров ──────────────────────────────────────
def extract_frame(video_path: str, output_path: str, time_sec: float = 1.0) -> bool:
    """Извлекает один кадр из видео в нужный момент времени."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(time_sec),
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0


def extract_frame_with_text(video_path: str, output_path: str,
                             text: str, settings: dict) -> bool:
    """Извлекает кадр и накладывает на него текст субтитра для превью."""
    # Сначала достаём кадр
    frame_path = output_path + "_raw.jpg"
    if not extract_frame(video_path, frame_path):
        return False

    s          = settings or {}
    vid_w, vid_h = get_video_size(video_path)
    font_size  = int(s.get("fontSize", 22))
    font_name  = s.get("fontName", "Arial")
    bold       = 1 if s.get("fontWeight") == "bold" else 0
    color      = hex_to_ass(s.get("color", "#ffffff"))
    bg_style   = s.get("bgStyle", "none")
    shadow_strength = int(s.get("shadowStrength", 3))
    bg_opacity = int(s.get("bgOpacity", 65))
    alpha_hex  = format(int((100 - bg_opacity) / 100 * 255), '02X').upper()

    zone   = s.get("zone", {})
    work_x0 = vid_w * float(zone.get("left", 5)) / 100
    work_y0 = vid_h * float(zone.get("top",  5)) / 100
    work_w  = vid_w * (1 - float(zone.get("left",5))/100 - float(zone.get("right",5))/100)
    work_h  = vid_h * (1 - float(zone.get("top",5))/100 - float(zone.get("bottom",5))/100)
    sub_x   = work_x0 + work_w * float(s.get("posX", 50)) / 100
    sub_y   = work_y0 + work_h * float(s.get("posY", 88)) / 100

    if bg_style == "box":
        back_colour = f"&H{alpha_hex}000000"
        border_style = 3; outline = 0; shadow = 0
    elif bg_style == "shadow":
        back_colour = "&H00000000"
        border_style = 1; outline = 0; shadow = shadow_strength
    else:
        back_colour = "&H00000000"
        border_style = 1; outline = 2; shadow = 1

    margin_l = int(work_x0)
    margin_r = int(vid_w - work_x0 - work_w)
    pos_tag  = "{\\pos(" + f"{sub_x:.1f},{sub_y:.1f}" + ")}"

    ass_lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {vid_w}", f"PlayResY: {vid_h}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_name},{font_size},{color},&H00FFFFFF,"
        f"&HFF000000,{back_colour},"
        f"{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},"
        f"2,{margin_l},{margin_r},0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{pos_tag}{text}"
    ]
    ass = "\n".join(ass_lines)

    ass_path = output_path + "_prev.ass"
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass)

    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg", "-y", "-i", frame_path,
        "-vf", f"ass={ass_esc}",
        "-q:v", "2", output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)

    for p in [frame_path, ass_path]:
        if os.path.exists(p): os.remove(p)

    return r.returncode == 0


def burn_subtitles(video_path: str, ass_path: str, output_path: str) -> bool:
    """Вшивает ASS субтитры через FFmpeg."""
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"ass={ass_escaped}",
        "-c:a", "copy",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        logger.error(f"FFmpeg ass: {r.stderr[-300:]}")
        return False
    return True


# ── Основной пайплайн ───────────────────────────────────────
# Запоминаем путь к последнему обработанному видео для /previewframe
_last_video_path: str = ""

def get_last_video_path() -> str:
    return _last_video_path

def process_video(
    input_path: str,
    output_path: str,
    use_censor: bool = False,
    settings: dict = None
) -> tuple[bool, str]:
    global _last_video_path

    base             = os.path.splitext(input_path)[0]
    audio_path       = base + "_audio.wav"
    ass_path         = base + ".ass"
    compressed_path  = base + "_compressed.mp4"
    working_path     = input_path

    try:
        logger.info(f"Файл: {input_path}")
        logger.info(f"Настройки: posX={settings.get('posX') if settings else 'default'} "
                    f"posY={settings.get('posY') if settings else 'default'} "
                    f"fontSize={settings.get('fontSize') if settings else 'default'}")

        # 0. Сжимаем если > 20 МБ
        if os.path.getsize(input_path) > COMPRESS_THRESHOLD:
            logger.info("Сжимаю видео...")
            if compress_video(input_path, compressed_path):
                working_path = compressed_path
            else:
                logger.warning("Сжатие не удалось")

        # Запоминаем путь для /previewframe
        _last_video_path = working_path

        # 1. Размеры видео — нужны для точного \pos(x,y)
        vid_w, vid_h = get_video_size(working_path)
        logger.info(f"Размер видео: {vid_w}x{vid_h}")

        # 2. Аудио
        logger.info("1/3 Извлекаю аудио...")
        if not extract_audio(working_path, audio_path):
            return False, "Не удалось извлечь аудио"

        # 3. Транскрипция
        logger.info("2/3 Транскрибирую...")
        words = transcribe(audio_path)
        if not words:
            return False, "Речь не обнаружена"

        max_w    = int((settings or {}).get("maxWords", MAX_WORDS_PER_SUB))
        segments = words_to_segments(words, max_words=max_w)

        if use_censor:
            segments = censor_segments(segments)
            logger.info("Цензура применена")

        logger.info(f"Субтитров: {len(segments)}")

        # 4. Генерируем ASS с точными координатами
        ass_content = segments_to_ass(segments, settings or {}, vid_w, vid_h)
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        # 5. Вшиваем
        logger.info("3/3 Вшиваю субтитры...")
        if not burn_subtitles(working_path, ass_path, output_path):
            return False, "Не удалось вшить субтитры"

        return True, ""

    except Exception as e:
        logger.exception(e)
        return False, f"Внутренняя ошибка: {str(e)}"

    finally:
        for p in [audio_path, ass_path, compressed_path]:
            if os.path.exists(p):
                os.remove(p)

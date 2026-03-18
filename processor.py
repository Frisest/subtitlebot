import os
import subprocess
import logging
from faster_whisper import WhisperModel
from censor import censor_segments

logger = logging.getLogger(__name__)

WHISPER_MODEL     = os.getenv("WHISPER_MODEL", "medium")
MAX_WORDS_PER_SUB = int(os.getenv("MAX_WORDS_PER_SUB", "5"))
COMPRESS_THRESHOLD = 20 * 1024 * 1024
FONTS_DIR         = "fonts"

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

    target_bits   = target_mb * 8 * 1024 * 1024
    video_bitrate = max(100_000, int((target_bits / duration) - 128 * 1024))
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
        logger.error(f"Compress error: {r2.stderr[-200:]}")
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
        logger.error(f"Audio extract: {r.stderr[-200:]}")
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


# ── Нарезка слов ────────────────────────────────────────────
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


# ── Вспомогательные функции ─────────────────────────────────
def hex_to_ass(hex_color: str) -> str:
    """#RRGGBB → &H00BBGGRR"""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H00{b}{g}{r}".upper()
    return "&H00FFFFFF"


def get_video_size(video_path: str) -> tuple[int, int]:
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
    return 1080, 1920


def sanitize_font_name(name: str) -> str:
    """Убирает проблемные символы из имени шрифта для ASS."""
    # ASS не любит запятые и некоторые спецсимволы в имени шрифта
    return name.replace(",", "").replace(";", "").strip()


def find_font_file(font_name: str) -> str | None:
    """Ищет файл шрифта в папке fonts/ по имени."""
    if not os.path.isdir(FONTS_DIR):
        return None
    name_lower = font_name.lower()
    for fname in os.listdir(FONTS_DIR):
        if fname.lower().endswith((".ttf", ".otf")):
            # Сравниваем имя файла без расширения
            file_base = os.path.splitext(fname)[0].lower()
            if file_base == name_lower or file_base in name_lower or name_lower in file_base:
                return os.path.join(FONTS_DIR, fname)
    return None


def calc_position(settings: dict, vid_w: int, vid_h: int) -> tuple[float, float, int, int]:
    """
    Вычисляет координаты субтитра и отступы.
    Возвращает (sub_x, sub_y, margin_l, margin_r).
    """
    s       = settings or {}
    zone    = s.get("zone", {})
    pos_x   = float(s.get("posX", 50))
    pos_y   = float(s.get("posY", 88))

    zone_l  = float(zone.get("left",   5)) / 100
    zone_r  = float(zone.get("right",  5)) / 100
    zone_t  = float(zone.get("top",    5)) / 100
    zone_b  = float(zone.get("bottom", 5)) / 100

    work_x0 = vid_w * zone_l
    work_y0 = vid_h * zone_t
    work_w  = vid_w * (1 - zone_l - zone_r)
    work_h  = vid_h * (1 - zone_t - zone_b)

    sub_x   = work_x0 + work_w * (pos_x / 100)
    sub_y   = work_y0 + work_h * (pos_y / 100)

    # Ограничиваем чтобы не выходило за края
    sub_x   = max(0, min(vid_w, sub_x))
    sub_y   = max(0, min(vid_h, sub_y))

    margin_l = int(work_x0)
    margin_r = int(vid_w - work_x0 - work_w)

    return sub_x, sub_y, margin_l, margin_r


def build_ass_style(settings: dict, font_name: str, vid_w: int, vid_h: int) -> tuple[str, str]:
    """
    Строит строку стиля ASS и pos_tag.
    Возвращает (style_line, pos_tag).
    """
    s           = settings or {}
    font_size   = int(s.get("fontSize", 22))
    bold        = 1 if s.get("fontWeight") == "bold" else 0
    color       = hex_to_ass(s.get("color", "#ffffff"))
    bg_style    = s.get("bgStyle", "none")
    shadow_str  = max(1, min(10, int(s.get("shadowStrength", 3))))
    bg_opacity  = max(10, min(100, int(s.get("bgOpacity", 65))))
    outline_w   = max(0, min(10, int(s.get("outlineWidth", 2))))

    # В ASS: 00=непрозрачный, FF=прозрачный
    alpha_hex = format(int((100 - bg_opacity) / 100 * 255), '02X').upper()

    # Масштабируем outline и shadow относительно размера шрифта
    # ASS значения — в пикселях видео, поэтому нужны реальные числа
    scaled_outline = round(font_size * outline_w * 0.05, 1)   # 0-5px при fontSize=22
    scaled_shadow  = round(font_size * shadow_str * 0.08, 1)  # масштабированная тень

    if bg_style == "box":
        back_colour  = f"&H{alpha_hex}000000"
        border_style = 3
        outline      = scaled_outline if outline_w > 0 else 0
        shadow       = 0
    elif bg_style == "shadow":
        back_colour  = "&HFF000000"
        border_style = 1
        outline      = scaled_outline if outline_w > 0 else 0
        shadow       = scaled_shadow
    else:  # none — контурный текст
        back_colour  = "&HFF000000"
        border_style = 1
        outline      = scaled_outline if outline_w > 0 else round(font_size * 0.08, 1)
        shadow       = round(font_size * 0.04, 1)

    sub_x, sub_y, margin_l, margin_r = calc_position(settings, vid_w, vid_h)

    # Alignment 8 = верх-центр, 2 = низ-центр, 5 = центр
    # Используем 5 (точный центр по \pos) чтобы текст не уходил за края
    alignment = 5

    pos_tag = "{\\an5\\pos(" + f"{sub_x:.0f},{sub_y:.0f}" + ")}"

    safe_font = sanitize_font_name(font_name)
    # OutlineColour &H00000000 = непрозрачный чёрный (00 = opaque в ASS)
    # BackColour используется для плашки (BorderStyle=3)
    style = (
        f"Style: Default,{safe_font},{font_size},"
        f"{color},&H00FFFFFF,&H00000000,{back_colour},"
        f"{bold},0,0,0,100,100,0,0,"
        f"{border_style},{outline},{shadow},"
        f"{alignment},{margin_l},{margin_r},0,1"
    )

    return style, pos_tag


def build_ass(segments: list[dict], settings: dict, vid_w: int, vid_h: int) -> str:
    """Строит полный ASS файл."""
    s         = settings or {}
    font_name = s.get("fontName", "Arial")
    style, pos_tag = build_ass_style(settings, font_name, vid_w, vid_h)

    def fmt(sec: float) -> str:
        h  = int(sec // 3600)
        m  = int((sec % 3600) // 60)
        s_ = int(sec % 60)
        cs = int((sec % 1) * 100)
        return f"{h}:{m:02d}:{s_:02d}.{cs:02d}"

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {vid_w}",
        f"PlayResY: {vid_h}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for seg in segments:
        text = seg["text"].replace("\n", "\\N")
        lines.append(
            f"Dialogue: 0,{fmt(seg['start'])},{fmt(seg['end'])},Default,,0,0,0,,{pos_tag}{text}"
        )

    return "\n".join(lines)


def burn_subtitles(video_path: str, ass_path: str, output_path: str,
                   font_name: str = "Arial") -> bool:
    """Вшивает ASS субтитры через FFmpeg."""
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")

    cmd = ["ffmpeg", "-y", "-i", video_path]

    # Если есть кастомный шрифт — передаём папку через fontsdir
    font_file = find_font_file(font_name)
    if font_file:
        # Устанавливаем FONTCONFIG через env переменную — самый надёжный способ на Windows
        import shutil, tempfile
        tmp_dir  = tempfile.mkdtemp()
        font_ext = os.path.splitext(font_file)[1]
        shutil.copy2(font_file, os.path.join(tmp_dir, "font" + font_ext))
        env = os.environ.copy()
        env["FONTCONFIG_PATH"] = tmp_dir
        logger.info(f"Шрифт в FONTCONFIG_PATH: {tmp_dir}")
    else:
        tmp_dir = None
        env     = None

    vf = f"ass={ass_escaped}"

    cmd += [
        "-vf", vf,
        "-c:a", "copy",
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "fast",
        output_path
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, env=env)

    if tmp_dir and os.path.exists(tmp_dir):
        import shutil as _shutil
        _shutil.rmtree(tmp_dir, ignore_errors=True)

    if r.returncode != 0:
        logger.error(f"FFmpeg ass error: {r.stderr[-500:]}")
        return False
    return True


def render_preset_preview(output_path: str, text: str, settings: dict,
                          width: int = 1080, height: int = 1920) -> bool:
    """Рендерит превью субтитра на зелёном фоне без видео."""
    s         = settings or {}
    font_name = s.get("fontName", "Arial")
    style, pos_tag = build_ass_style(settings, font_name, width, height)

    ass_lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{pos_tag}{text}"
    ]
    ass_path = output_path + ".ass"
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(ass_lines))

    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:")

    font_file = find_font_file(font_name)
    if font_file:
        import shutil, tempfile
        tmp_dir  = tempfile.mkdtemp()
        font_ext = os.path.splitext(font_file)[1]
        shutil.copy2(font_file, os.path.join(tmp_dir, "font" + font_ext))
        env = os.environ.copy()
        env["FONTCONFIG_PATH"] = tmp_dir
    else:
        tmp_dir = None
        env     = None

    vf = f"ass={ass_esc}"

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=0x2D6A4F:size={width}x{height}:duration=1:rate=1",
        "-vf", vf,
        "-vframes", "1", "-q:v", "2",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if os.path.exists(ass_path):
        os.remove(ass_path)
    if tmp_dir and os.path.exists(tmp_dir):
        import shutil as _shutil
        _shutil.rmtree(tmp_dir, ignore_errors=True)
    if r.returncode != 0:
        logger.error(f"render_preset_preview: {r.stderr[-300:]}")
        return False
    return True


def process_video(
    input_path: str,
    output_path: str,
    use_censor: bool = False,
    settings: dict = None
) -> tuple[bool, str]:
    """Полный пайплайн обработки видео."""
    s               = settings or {}
    base            = os.path.splitext(input_path)[0]
    audio_path      = base + "_audio.wav"
    ass_path        = base + ".ass"
    compressed_path = base + "_compressed.mp4"
    working_path    = input_path

    try:
        logger.info(f"Файл: {input_path}")
        logger.info(f"Настройки: posX={s.get('posX')} posY={s.get('posY')} "
                    f"fontSize={s.get('fontSize')} font={s.get('fontName')}")

        # 0. Сжимаем если > 20 МБ
        if os.path.getsize(input_path) > COMPRESS_THRESHOLD:
            logger.info("Сжимаю видео...")
            if compress_video(input_path, compressed_path):
                working_path = compressed_path
            else:
                logger.warning("Сжатие не удалось")

        # 1. Размеры видео
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

        max_w    = int(s.get("maxWords", MAX_WORDS_PER_SUB))
        segments = words_to_segments(words, max_words=max_w)

        if use_censor:
            segments = censor_segments(segments)
            logger.info("Цензура применена")

        logger.info(f"Субтитров: {len(segments)}")

        # 4. Генерируем ASS
        ass_content = build_ass(segments, s, vid_w, vid_h)
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        logger.info(f"ASS файл создан: {ass_path}")

        # 5. Вшиваем
        logger.info("3/3 Вшиваю субтитры...")
        font_name = s.get("fontName", "Arial")
        if not burn_subtitles(working_path, ass_path, output_path, font_name):
            return False, "Не удалось вшить субтитры"

        logger.info("Готово!")
        return True, ""

    except Exception as e:
        logger.exception(e)
        return False, f"Внутренняя ошибка: {str(e)}"

    finally:
        for p in [audio_path, ass_path, compressed_path]:
            if os.path.exists(p):
                os.remove(p)

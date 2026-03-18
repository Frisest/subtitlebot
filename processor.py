import os
import subprocess
import logging
from faster_whisper import WhisperModel
from censor import censor_segments

logger = logging.getLogger(__name__)

WHISPER_MODEL      = os.getenv("WHISPER_MODEL", "medium")
MAX_WORDS_PER_SUB  = int(os.getenv("MAX_WORDS_PER_SUB", "5"))
COMPRESS_THRESHOLD = 20 * 1024 * 1024
FONTS_DIR          = "fonts"

# Хранилище последних сегментов для /changeword
# { user_id: { 'segments': [...], 'video_path': str, 'settings': dict } }
_last_session: dict = {}

def save_session(user_id: int, segments: list, video_path: str, settings: dict):
    _last_session[str(user_id)] = {
        'segments': segments,
        'video_path': video_path,
        'settings': settings
    }

def get_session(user_id: int) -> dict | None:
    return _last_session.get(str(user_id))


logger.info(f"Загружаю модель Whisper: {WHISPER_MODEL}...")
model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
logger.info("Модель загружена.")


def compress_video(input_path: str, output_path: str, target_mb: int = 18) -> bool:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True)
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
        "-pass", "1", "-passlogfile", passlog, "-an", "-f", "null", os.devnull
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
        logger.error(f"Compress: {r2.stderr[-200:]}")
        return False
    logger.info(f"Сжато до {os.path.getsize(output_path)/1024/1024:.1f} МБ")
    return True


def extract_audio(video_path: str, audio_path: str) -> bool:
    r = subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path
    ], capture_output=True, text=True)
    if r.returncode != 0:
        logger.error(f"Audio: {r.stderr[-200:]}")
        return False
    return True


def transcribe(audio_path: str) -> list[dict]:
    segments, info = model.transcribe(
        audio_path, language=None, beam_size=5, best_of=5, patience=1.0,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400, speech_pad_ms=200),
        word_timestamps=True, temperature=0.0,
        compression_ratio_threshold=2.4, log_prob_threshold=-1.0,
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


def words_to_segments(words: list[dict], max_words: int = 5) -> list[dict]:
    if not words:
        return []
    segments, chunk = [], []
    for i, w in enumerate(words):
        if chunk and (w["start"] - words[i-1]["end"]) > 0.8:
            segments.append({"start": chunk[0]["start"], "end": chunk[-1]["end"],
                              "text": " ".join(x["word"] for x in chunk)})
            chunk = []
        chunk.append(w)
        if len(chunk) >= max_words:
            segments.append({"start": chunk[0]["start"], "end": chunk[-1]["end"],
                              "text": " ".join(x["word"] for x in chunk)})
            chunk = []
    if chunk:
        segments.append({"start": chunk[0]["start"], "end": chunk[-1]["end"],
                          "text": " ".join(x["word"] for x in chunk)})
    return segments


def hex_to_ass(hex_color: str) -> str:
    """#RRGGBB → &H00BBGGRR (ASS: alpha=00 = непрозрачный)"""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H00{b}{g}{r}".upper()
    return "&H00FFFFFF"


def get_video_size(video_path: str) -> tuple[int, int]:
    r = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path
    ], capture_output=True, text=True)
    if r.returncode == 0:
        try:
            parts = r.stdout.strip().split(",")
            return int(parts[0]), int(parts[1])
        except Exception:
            pass
    return 1080, 1920


def find_font_file(font_name: str) -> str | None:
    if not os.path.isdir(FONTS_DIR):
        return None
    name_lower = font_name.lower()
    for fname in os.listdir(FONTS_DIR):
        if fname.lower().endswith((".ttf", ".otf")):
            base = os.path.splitext(fname)[0].lower()
            if base == name_lower or base in name_lower or name_lower in base:
                return os.path.join(FONTS_DIR, fname)
    return None


def sanitize_font_name(name: str) -> str:
    return name.replace(",", "").replace(";", "").strip()


def build_ass(segments: list[dict], settings: dict, vid_w: int, vid_h: int) -> str:
    """Строит ASS файл с субтитрами."""
    s           = settings or {}
    font_size   = int(s.get("fontSize", 22))
    font_name   = sanitize_font_name(s.get("fontName", "Arial"))
    bold        = 1 if s.get("fontWeight") == "bold" else 0
    color       = hex_to_ass(s.get("color", "#ffffff"))
    bg_style    = s.get("bgStyle", "none")
    shadow_str  = max(0, min(10, int(s.get("shadowStrength", 3))))
    bg_opacity  = max(10, min(100, int(s.get("bgOpacity", 65))))
    outline_w   = max(0, min(10, int(s.get("outlineWidth", 2))))

    # Зона безопасности
    zone    = s.get("zone", {})
    zone_l  = float(zone.get("left",   5)) / 100
    zone_r  = float(zone.get("right",  5)) / 100
    zone_t  = float(zone.get("top",    5)) / 100
    zone_b  = float(zone.get("bottom", 5)) / 100

    work_x0 = vid_w * zone_l
    work_y0 = vid_h * zone_t
    work_w  = vid_w * (1 - zone_l - zone_r)
    work_h  = vid_h * (1 - zone_t - zone_b)

    sub_x = max(0, min(vid_w, work_x0 + work_w * float(s.get("posX", 50)) / 100))
    sub_y = max(0, min(vid_h, work_y0 + work_h * float(s.get("posY", 88)) / 100))

    margin_l = int(work_x0)
    margin_r = int(vid_w - work_x0 - work_w)

    # ASS цвета: &HAABBGGRR где AA=00 непрозрачный, FF=прозрачный
    # OutlineColour — цвет обводки (чёрный непрозрачный)
    outline_colour = "&H00000000"
    # BackColour — цвет фона для плашки (BorderStyle=3)
    alpha_hex  = format(int((100 - bg_opacity) / 100 * 255), '02X').upper()
    back_colour = f"&H{alpha_hex}000000"
    # ShadowColour — задаём через тег в тексте
    shadow_alpha = format(max(0, int((1 - shadow_str / 10) * 200)), '02X').upper()

    # Масштаб: ASS единицы = пиксели видео
    # Обводка: пропорционально шрифту
    outline_px = round(font_size * outline_w / 10 * 0.6, 1)
    # Тень: жёсткий drop shadow
    shadow_px  = round(font_size * shadow_str / 10 * 0.5, 1)

    if bg_style == "box":
        border_style = 3  # непрозрачный прямоугольный фон
        outline      = outline_px
        shadow       = 0
    elif bg_style == "shadow":
        border_style = 1
        outline      = outline_px
        shadow       = shadow_px
    else:  # none
        border_style = 1
        # Если outline не задан — ставим минимальную обводку для читаемости
        outline = outline_px if outline_w > 0 else round(font_size * 0.06, 1)
        shadow  = round(font_size * 0.03, 1)

    alignment = 5  # центр по \pos
    pos_tag   = "{\\an5\\pos(" + f"{sub_x:.0f},{sub_y:.0f}" + ")}"

    # Для тени добавляем цвет тени через тег \4a (alpha тени)
    shadow_tag = ""
    if bg_style == "shadow" and shadow_str > 0:
        shadow_tag = "{\\4a&H" + shadow_alpha + "&}"

    def fmt(sec: float) -> str:
        h  = int(sec // 3600)
        m  = int((sec % 3600) // 60)
        s_ = int(sec % 60)
        cs = int((sec % 1) * 100)
        return f"{h}:{m:02d}:{s_:02d}.{cs:02d}"

    style_line = (
        f"Style: Default,{font_name},{font_size},"
        f"{color},&H00FFFFFF,{outline_colour},{back_colour},"
        f"{bold},0,0,0,100,100,0,0,"
        f"{border_style},{outline},{shadow},"
        f"{alignment},{margin_l},{margin_r},0,1"
    )

    lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {vid_w}", f"PlayResY: {vid_h}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style_line, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for seg in segments:
        text = seg["text"].replace("\n", "\\N")
        lines.append(
            f"Dialogue: 0,{fmt(seg['start'])},{fmt(seg['end'])},Default,,0,0,0,,"
            f"{pos_tag}{shadow_tag}{text}"
        )

    return "\n".join(lines)


def burn_subtitles(video_path: str, ass_path: str, output_path: str,
                   font_name: str = "Arial") -> bool:
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
    font_file   = find_font_file(font_name)

    if font_file:
        import shutil, tempfile
        tmp_dir  = tempfile.mkdtemp()
        font_ext = os.path.splitext(font_file)[1]
        shutil.copy2(font_file, os.path.join(tmp_dir, "font" + font_ext))
        env      = os.environ.copy()
        env["FONTCONFIG_PATH"] = tmp_dir
        logger.info(f"Шрифт: {font_file}")
    else:
        tmp_dir = None
        env     = None

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"ass={ass_escaped}",
        "-c:a", "copy", "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)

    if tmp_dir:
        import shutil as _s
        _s.rmtree(tmp_dir, ignore_errors=True)

    if r.returncode != 0:
        logger.error(f"FFmpeg: {r.stderr[-500:]}")
        return False
    return True


def render_preset_preview(output_path: str, text: str, settings: dict,
                           width: int = 1080, height: int = 1920) -> bool:
    s       = settings or {}
    ass_content = build_ass(
        [{"start": 0.0, "end": 5.0, "text": text}],
        settings, width, height
    )
    ass_path = output_path + ".ass"
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_content)

    ass_esc  = ass_path.replace("\\", "/").replace(":", "\\:")
    font_name = s.get("fontName", "Arial")
    font_file = find_font_file(font_name)

    if font_file:
        import shutil, tempfile
        tmp_dir  = tempfile.mkdtemp()
        font_ext = os.path.splitext(font_file)[1]
        shutil.copy2(font_file, os.path.join(tmp_dir, "font" + font_ext))
        env      = os.environ.copy()
        env["FONTCONFIG_PATH"] = tmp_dir
    else:
        tmp_dir = None
        env     = None

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=0x2D6A4F:size={width}x{height}:duration=1:rate=1",
        "-vf", f"ass={ass_esc}",
        "-vframes", "1", "-q:v", "2",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)

    if os.path.exists(ass_path):
        os.remove(ass_path)
    if tmp_dir:
        import shutil as _s
        _s.rmtree(tmp_dir, ignore_errors=True)

    if r.returncode != 0:
        logger.error(f"render_preset: {r.stderr[-300:]}")
        return False
    return True


def parse_timing_text(text: str) -> list[dict]:
    """
    Парсит текст с таймингами формата:
    "привет 2сек сегодня поговорим 3сек как работает 1сек бот"

    Цифра перед "сек" = длительность предыдущего блока текста в секундах.
    Последний блок без цифры получает 2 секунды по умолчанию.

    Возвращает список сегментов с таймингами.
    """
    import re
    # Разбиваем по паттерну "число+сек"
    parts = re.split(r'(\d+(?:\.\d+)?\s*сек)', text.strip(), flags=re.IGNORECASE)

    segments = []
    current_time = 0.0
    pending_text = ""

    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Это таймер?
        m = re.match(r'^(\d+(?:\.\d+)?)\s*сек$', part, re.IGNORECASE)
        if m:
            duration = float(m.group(1))
            if pending_text:
                segments.append({
                    "start": current_time,
                    "end":   current_time + duration,
                    "text":  pending_text.strip()
                })
                current_time += duration
                pending_text = ""
        else:
            # Текст — добавляем к ожидающему
            if pending_text:
                pending_text += " " + part
            else:
                pending_text = part

    # Последний блок без таймера — 2 сек по умолчанию
    if pending_text.strip():
        segments.append({
            "start": current_time,
            "end":   current_time + 2.0,
            "text":  pending_text.strip()
        })

    return segments


def rebuild_with_custom_text(user_id: int, custom_text: str,
                              output_path: str) -> tuple[bool, str, list]:
    """
    Перегенерирует видео с пользовательским текстом.

    Поддерживает два формата:
    1. Простой текст — тайминги из оригинала
    2. Текст с таймингами: "привет 2сек пока 3сек" — явные длительности
    """
    import re
    session = get_session(user_id)
    if not session:
        return False, "Нет сохранённого видео. Сначала отправь видео боту.", []

    orig_segments = session['segments']
    video_path    = session['video_path']
    settings      = session['settings']

    if not os.path.exists(video_path):
        return False, "Исходное видео не найдено. Отправь видео заново.", []

    if not custom_text.strip():
        return False, "Текст пустой.", []

    # Определяем формат: есть ли тайминги?
    has_timings = bool(re.search(r'\d+\s*сек', custom_text, re.IGNORECASE))

    if has_timings:
        # Парсим тайминги из текста
        new_segments = parse_timing_text(custom_text)
        if not new_segments:
            return False, "Не удалось распознать тайминги. Проверь формат: текст 2сек текст 3сек", []
    else:
        # Распределяем слова по оригинальным таймингам
        words = custom_text.split()
        if not words:
            return False, "Текст пустой.", []

        max_w       = int(settings.get('maxWords', 5))
        total_words = len(words)
        n_segs      = len(orig_segments)
        new_segments = []
        word_idx = 0

        for i, seg in enumerate(orig_segments):
            remaining_words = total_words - word_idx
            remaining_segs  = n_segs - i
            words_for_seg   = max(1, round(remaining_words / remaining_segs))
            words_for_seg   = min(words_for_seg, max_w)
            chunk = words[word_idx : word_idx + words_for_seg]
            if not chunk:
                break
            word_idx += len(chunk)
            new_segments.append({
                'start': seg['start'],
                'end':   seg['end'],
                'text':  ' '.join(chunk)
            })

        if word_idx < total_words:
            leftover = ' '.join(words[word_idx:])
            if new_segments:
                new_segments[-1]['text'] += ' ' + leftover

    vid_w, vid_h = get_video_size(video_path)
    ass_content  = build_ass(new_segments, settings, vid_w, vid_h)
    ass_path     = output_path + ".ass"

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_content)

    font_name = settings.get('fontName', 'Arial')
    ok = burn_subtitles(video_path, ass_path, output_path, font_name)

    if os.path.exists(ass_path):
        os.remove(ass_path)

    if not ok:
        return False, "Не удалось вшить субтитры.", []

    return True, "", new_segments


def process_video(input_path: str, output_path: str,
                  use_censor: bool = False, settings: dict = None) -> tuple[bool, str, list]:
    s               = settings or {}
    base            = os.path.splitext(input_path)[0]
    audio_path      = base + "_audio.wav"
    ass_path        = base + ".ass"
    compressed_path = base + "_compressed.mp4"
    working_path    = input_path

    try:
        logger.info(f"Файл: {input_path}")
        logger.info(f"posX={s.get('posX')} posY={s.get('posY')} "
                    f"fontSize={s.get('fontSize')} font={s.get('fontName')} "
                    f"outline={s.get('outlineWidth')} shadow={s.get('shadowStrength')} "
                    f"bg={s.get('bgStyle')}")

        if os.path.getsize(input_path) > COMPRESS_THRESHOLD:
            logger.info("Сжимаю...")
            if compress_video(input_path, compressed_path):
                working_path = compressed_path

        vid_w, vid_h = get_video_size(working_path)
        logger.info(f"Видео: {vid_w}x{vid_h}")

        logger.info("1/3 Аудио...")
        if not extract_audio(working_path, audio_path):
            return False, "Не удалось извлечь аудио", []

        logger.info("2/3 Транскрипция...")
        words = transcribe(audio_path)
        if not words:
            return False, "Речь не обнаружена", []

        max_w    = int(s.get("maxWords", MAX_WORDS_PER_SUB))
        segments = words_to_segments(words, max_words=max_w)
        if use_censor:
            segments = censor_segments(segments)
        logger.info(f"Субтитров: {len(segments)}")

        ass_content = build_ass(segments, s, vid_w, vid_h)
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        logger.info(f"ASS: {ass_path}")

        logger.info("3/3 Вшиваю...")
        if not burn_subtitles(working_path, ass_path, output_path, s.get("fontName", "Arial")):
            return False, "Не удалось вшить субтитры", []

        return True, "", segments

    except Exception as e:
        logger.exception(e)
        return False, f"Ошибка: {str(e)}", []

    finally:
        for p in [audio_path, ass_path, compressed_path]:
            if os.path.exists(p):
                os.remove(p)

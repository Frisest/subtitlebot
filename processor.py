import os
import subprocess
import logging
from faster_whisper import WhisperModel
from censor import censor_segments

logger = logging.getLogger(__name__)

# Модель: tiny/base/small/medium/large-v3
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")

# Сколько слов максимум в одном субтитре
MAX_WORDS_PER_SUB = int(os.getenv("MAX_WORDS_PER_SUB", "5"))

# Порог сжатия (20 МБ = лимит Telegram Bot API)
COMPRESS_THRESHOLD = 20 * 1024 * 1024

logger.info(f"Загружаю модель Whisper: {WHISPER_MODEL}...")
model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
logger.info("Модель загружена.")


def compress_video(input_path: str, output_path: str, target_mb: int = 18) -> bool:
    """Сжимает видео до target_mb МБ через двухпроходное кодирование."""
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

    target_bits = target_mb * 8 * 1024 * 1024
    audio_bitrate = 128 * 1024
    video_bitrate = int((target_bits / duration) - audio_bitrate)
    if video_bitrate < 100_000:
        video_bitrate = 300_000

    logger.info(f"Сжимаю: {duration:.1f}с, битрейт={video_bitrate//1000}кбит/с")
    passlog = output_path + "_passlog"

    pass1 = subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-b:v", str(video_bitrate),
        "-pass", "1", "-passlogfile", passlog,
        "-an", "-f", "null", os.devnull
    ], capture_output=True, text=True)
    if pass1.returncode != 0:
        return False

    pass2 = subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-b:v", str(video_bitrate),
        "-pass", "2", "-passlogfile", passlog,
        "-c:a", "aac", "-b:a", "128k",
        output_path
    ], capture_output=True, text=True)

    for ext in ["-0.log", "-0.log.mbtree"]:
        p = passlog + ext
        if os.path.exists(p):
            os.remove(p)

    if pass2.returncode != 0:
        logger.error(f"Сжатие не удалось: {pass2.stderr[-300:]}")
        return False

    logger.info(f"Сжато до {os.path.getsize(output_path)/1024/1024:.1f} МБ")
    return True


def extract_audio(video_path: str, audio_path: str) -> bool:
    """Извлекает аудио из видео через FFmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"FFmpeg audio extract: {result.stderr[-300:]}")
        return False
    return True


def transcribe(audio_path: str) -> list[dict]:
    """
    Транскрибирует аудио с пословными таймстампами.
    Автоопределение языка — поддерживает русский и украинский одновременно.
    """
    segments, info = model.transcribe(
        audio_path,
        language=None,          # авто — распознает и ru и uk
        beam_size=5,
        best_of=5,
        patience=1.0,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=400,
            speech_pad_ms=200,
        ),
        word_timestamps=True,   # пословные таймстампы — ключевое для точных субтитров
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

    logger.info(f"Распознано слов: {len(words)}")
    return words


def words_to_segments(words: list[dict], max_words: int = 5) -> list[dict]:
    """
    Нарезает слова на субтитры по max_words слов.
    Также разрывает субтитр при паузе > 0.8 секунды.
    """
    if not words:
        return []

    segments = []
    chunk = []

    for i, w in enumerate(words):
        # Пауза > 0.8с — начинаем новый субтитр
        if chunk and (w["start"] - words[i - 1]["end"]) > 0.8:
            segments.append({
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"],
                "text": " ".join(x["word"] for x in chunk)
            })
            chunk = []

        chunk.append(w)

        if len(chunk) >= max_words:
            segments.append({
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"],
                "text": " ".join(x["word"] for x in chunk)
            })
            chunk = []

    if chunk:
        segments.append({
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
            "text": " ".join(x["word"] for x in chunk)
        })

    return segments


def segments_to_srt(segments: list[dict]) -> str:
    def fmt(s: float) -> str:
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        ms = int((s % 1) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{fmt(seg['start'])} --> {fmt(seg['end'])}", seg["text"], ""]
    return "\n".join(lines)


def hex_to_ass(hex_color: str) -> str:
    """Конвертирует #RRGGBB в формат ASS &HBBGGRR."""
    h = hex_color.lstrip('#')
    if len(h) == 6:
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H00{b}{g}{r}".upper()
    return "&H00FFFFFF"


def alignment_from_pos(pos_x: float, pos_y: float) -> int:
    """Возвращает ASS Alignment (1-9) по позиции в процентах."""
    col = 1 if pos_x < 33 else (2 if pos_x < 66 else 3)
    row = 1 if pos_y > 66 else (4 if pos_y > 33 else 7)
    return row + col - 1


def burn_subtitles(video_path: str, srt_path: str, output_path: str, settings: dict = None) -> bool:
    """Вшивает субтитры в видео через FFmpeg с настройками пользователя."""
    s = settings or {}
    srt_escaped = srt_path.replace("\\", "/").replace(":", "\\:")

    font_size  = int(s.get("fontSize", 22))
    color      = hex_to_ass(s.get("color", "#ffffff"))
    bold       = 1 if s.get("fontWeight") == "bold" else 0
    pos_x      = float(s.get("posX", 50))
    pos_y      = float(s.get("posY", 88))
    alignment  = alignment_from_pos(pos_x, pos_y)
    bg_style   = s.get("bgStyle", "none")

    # Отступ от края на основе зоны безопасности
    zone       = s.get("zone", {})
    margin_l   = int(float(zone.get("left",   5)) * 19.2)   # % от 1920px
    margin_r   = int(float(zone.get("right",  5)) * 19.2)
    margin_v   = int(float(zone.get("bottom", 5)) * 10.8)   # % от 1080px

    if bg_style == "box":
        back_colour  = "&H99000000"   # полупрозрачный чёрный
        border_style = 3             # непрозрачный фон (box)
    else:
        back_colour  = "&H00000000"
        border_style = 1             # стандартный (outline)

    outline = 0 if bg_style == "box" else 2
    shadow  = 1 if bg_style in ("none", "shadow") else 0

    style = (
        f"FontName=Arial,FontSize={font_size},Bold={bold},"
        f"PrimaryColour={color},"
        f"BackColour={back_colour},"
        f"OutlineColour=&H00000000,"
        f"Outline={outline},Shadow={shadow},"
        f"Alignment={alignment},"
        f"MarginL={margin_l},MarginR={margin_r},MarginV={margin_v},"
        f"BorderStyle={border_style}"
    )

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"subtitles={srt_escaped}:force_style=\'{style}\'",
        "-c:a", "copy", "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"FFmpeg subtitles: {result.stderr[-300:]}")
        return False
    return True


def process_video(input_path: str, output_path: str, use_censor: bool = False, settings: dict = None) -> tuple[bool, str]:
    """Полный пайплайн обработки видео."""
    base = os.path.splitext(input_path)[0]
    audio_path      = base + "_audio.wav"
    srt_path        = base + ".srt"
    compressed_path = base + "_compressed.mp4"
    working_path    = input_path

    try:
        logger.info(f"Файл: {input_path}")

        # 0. Сжимаем если > 20 МБ
        file_size = os.path.getsize(input_path)
        if file_size > COMPRESS_THRESHOLD:
            logger.info(f"Размер {file_size/1024/1024:.1f} МБ > лимит, сжимаю...")
            if compress_video(input_path, compressed_path):
                working_path = compressed_path
            else:
                logger.warning("Сжатие не удалось, продолжаю с оригиналом")

        # 1. Аудио
        logger.info("1/3 Извлечение аудио...")
        if not extract_audio(working_path, audio_path):
            return False, "Не удалось извлечь аудио"

        # 2. Транскрипция
        logger.info("2/3 Транскрипция...")
        words = transcribe(audio_path)
        if not words:
            return False, "Речь не обнаружена в видео"

        max_w = int((settings or {}).get('maxWords', MAX_WORDS_PER_SUB))
        segments = words_to_segments(words, max_words=max_w)
        if use_censor:
            segments = censor_segments(segments)
            logger.info("Цензура применена")
        logger.info(f"Субтитров: {len(segments)}")

        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(segments_to_srt(segments))

        # 3. Вшиваем
        logger.info("3/3 Вшиваю субтитры...")
        if not burn_subtitles(working_path, srt_path, output_path, settings or {}):
            return False, "Не удалось вшить субтитры"

        return True, ""

    except Exception as e:
        logger.exception(e)
        return False, f"Внутренняя ошибка: {str(e)}"

    finally:
        for path in [audio_path, srt_path, compressed_path]:
            if os.path.exists(path):
                os.remove(path)

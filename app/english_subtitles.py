from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .cancel import run_cancellable
from .video_utils import ffmpeg_bin


_MODEL_LOCK = threading.Lock()
_MODELS: dict[str, Any] = {}
SUBTITLE_FILL = (255, 222, 0, 255)
SUBTITLE_OUTLINE = (0, 0, 0, 255)

def translate_chinese_speech(
    video_path: Path,
    work_dir: Path,
    task_id: str,
    model_name: str = "base",
) -> list[dict[str, Any]]:
    """Transcribe Chinese speech and translate it directly to timed English text."""
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_path = work_dir / f"{task_id}_zh_audio.wav"
    command = [
        ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(audio_path),
    ]
    result = run_cancellable(command, task_id=task_id)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"提取中文语音失败：{detail[-800:]}")
    safe_model = model_name if model_name in {"tiny", "base", "small", "medium"} else "base"
    try:
        # Transcription is serialized because a cached CTranslate2 model is not
        # safe to drive concurrently from several directory groups.
        with _MODEL_LOCK:
            model = _MODELS.get(safe_model)
            if model is None:
                import faster_whisper  # type: ignore

                model = faster_whisper.WhisperModel(safe_model, device="cpu", compute_type="int8")
                _MODELS[safe_model] = model
            raw_segments, _ = model.transcribe(
                str(audio_path),
                language="zh",
                task="translate",
                vad_filter=True,
                # Greedy decoding is substantially faster for short-form
                # subtitle translation and avoids spending most batch time on
                # five nearly identical beam candidates.
                beam_size=1,
                best_of=1,
                condition_on_previous_text=False,
            )
            return [
                {"start": float(item.start), "end": float(item.end), "text": str(item.text or "").strip()}
                for item in raw_segments
                if str(item.text or "").strip()
            ]
    finally:
        audio_path.unlink(missing_ok=True)


def slice_segments(
    segments: list[dict[str, Any]],
    start: float,
    duration: float,
) -> list[dict[str, Any]]:
    end = start + duration
    sliced: list[dict[str, Any]] = []
    for item in segments:
        cue_start = float(item.get("start") or 0)
        cue_end = float(item.get("end") or cue_start)
        if cue_end <= start or cue_start >= end:
            continue
        local_start = max(0.0, cue_start - start)
        local_end = min(duration, cue_end - start)
        if local_end > local_start:
            sliced.append({"start": local_start, "end": local_end, "text": str(item.get("text") or "")})
    return sliced


def create_subtitle_overlays(
    output_dir: Path,
    task_id: str,
    segments: list[dict[str, Any]],
    speed: float = 1.0,
    width: int = 720,
    height: int = 1280,
) -> list[dict[str, Any]]:
    if not segments:
        return []
    from PIL import Image, ImageDraw, ImageFont

    speed = max(0.1, float(speed or 1.0))
    output_dir.mkdir(parents=True, exist_ok=True)
    font_candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    font_path = next((item for item in font_candidates if item.exists()), None)
    font_size = max(32, int(width * 0.058))
    font = ImageFont.truetype(str(font_path), font_size) if font_path else ImageFont.load_default()
    max_width = width - 76
    outline_width = max(3, int(width * 0.006))
    overlays: list[dict[str, Any]] = []
    for index, item in enumerate(segments, start=1):
        start = float(item.get("start") or 0)
        end = max(start + 0.15, float(item.get("end") or start))
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        probe = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(probe)
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            box = draw.textbbox((0, 0), candidate, font=font, stroke_width=outline_width)
            if current and box[2] - box[0] > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        lines = lines[:3]
        boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=outline_width) for line in lines]
        line_height = max((box[3] - box[1] for box in boxes), default=font_size)
        subtitle_height = line_height * len(lines) + 8 * max(0, len(lines) - 1)
        y = height - max(72, int(height * 0.09)) - subtitle_height
        for line, box in zip(lines, boxes):
            text_width = box[2] - box[0]
            x = (width - text_width) // 2
            draw.text(
                (x, y),
                line,
                font=font,
                fill=SUBTITLE_FILL,
                stroke_width=outline_width,
                stroke_fill=SUBTITLE_OUTLINE,
            )
            y += line_height + 8
        path = output_dir / f"{task_id}_subtitle_{index:04d}.png"
        probe.save(path)
        overlays.append({"path": path, "start": start / speed, "end": end / speed})
    return overlays

from __future__ import annotations

import os
import random
import shutil
from pathlib import Path
from typing import Any, Union

from .cancel import CancelledTask, is_cancel_requested, run_cancellable
from .models import VariantOptions, VideoInfo
from .video_utils import ffmpeg_bin, get_video_info


def _run(command: list[str], *, task_id: str | None = None) -> None:
    result = run_cancellable(command, task_id=task_id)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"FFmpeg 执行失败，code={result.returncode}: {stderr[-1600:]}")


def _quote_concat_path(path: Path) -> str:
    return "file '" + str(path).replace("'", "'\\''") + "'"


def _even(value: float) -> int:
    number = max(2, int(value))
    return number if number % 2 == 0 else number - 1


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


EXPORT_WIDTH = _even(_int_env("VIDEO_VARIANT_EXPORT_WIDTH", 480, 320, 2160))
EXPORT_HEIGHT = _even(_int_env("VIDEO_VARIANT_EXPORT_HEIGHT", 854, 480, 3840))
EXPORT_FPS = _int_env("VIDEO_VARIANT_EXPORT_FPS", 30, 15, 60)
EXPORT_CRF = str(_int_env("VIDEO_VARIANT_EXPORT_CRF", 30, 18, 38))
EXPORT_PRESET = os.getenv("VIDEO_VARIANT_X264_PRESET", "ultrafast")
EXPORT_AUDIO_BITRATE = os.getenv("VIDEO_VARIANT_AUDIO_BITRATE", "192k")
EXPORT_AUDIO_SAMPLE_RATE = str(_int_env("VIDEO_VARIANT_AUDIO_SAMPLE_RATE", 44100, 8000, 96000))
FOREGROUND_WIDTH = _even(EXPORT_WIDTH * 0.9)
FOREGROUND_HEIGHT = _even(EXPORT_HEIGHT * 0.9)
HOOK_FONT_CANDIDATES = [
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("/System/Library/Fonts/STHeiti Light.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
]
DEFAULT_HOOK_TEXTS = [
    "She thought no one saw it",
    "Everything changed after this",
    "He had no idea what was coming",
    "Watch until the final twist",
    "This secret was never meant to surface",
    "One choice changed their lives",
    "Nobody expected what happened next",
    "The truth comes out in seconds",
    "This is where it all falls apart",
    "She finally stopped pretending",
]


def _fit_pad_filter(input_label: str = "0:v", output_label: str = "v") -> str:
    return (
        f"[{input_label}]scale={EXPORT_WIDTH}:{EXPORT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={EXPORT_WIDTH}:{EXPORT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={EXPORT_FPS},setsar=1,format=yuv420p[{output_label}]"
    )


def _mp4_compat_args() -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        EXPORT_PRESET,
        "-crf",
        EXPORT_CRF,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        EXPORT_AUDIO_BITRATE,
        "-ar",
        EXPORT_AUDIO_SAMPLE_RATE,
        "-ac",
        "2",
        "-movflags",
        "+faststart",
    ]


def _profile_ranges(intensity: str) -> dict[str, Any]:
    if intensity == "light":
        return {
            "zoom": (1.01, 1.025),
            "offset": 12,
            "noise": (2, 4),
            "saturation": (1.02, 1.08),
            "contrast": (1.01, 1.06),
            "speed": (0.99, 1.01),
            "blur": (20, 28),
        }
    if intensity == "strong":
        return {
            "zoom": (1.035, 1.06),
            "offset": 34,
            "noise": (5, 9),
            "saturation": (1.08, 1.18),
            "contrast": (1.05, 1.13),
            "speed": (0.97, 1.03),
            "blur": (28, 40),
        }
    return {
        "zoom": (1.02, 1.045),
        "offset": 24,
        "noise": (3, 7),
        "saturation": (1.04, 1.13),
        "contrast": (1.03, 1.1),
        "speed": (0.98, 1.02),
        "blur": (22, 36),
    }


def build_effects(task_id: str, options: VariantOptions) -> dict[str, Any]:
    rng = random.Random(task_id)
    profile = options.intensity if options.intensity in {"light", "balanced", "strong"} else "balanced"
    ranges = _profile_ranges(profile)

    zoom_min, zoom_max = ranges["zoom"] if options.effect_zoom else (1.0, 1.0)
    speed_min, speed_max = ranges["speed"] if options.effect_speed else (1.0, 1.0)
    saturation_min, saturation_max = ranges["saturation"] if options.effect_color else (1.0, 1.0)
    contrast_min, contrast_max = ranges["contrast"] if options.effect_color else (1.0, 1.0)
    noise_min, noise_max = ranges["noise"] if options.effect_texture else (0, 0)
    offset = ranges["offset"] if options.effect_zoom else 0
    blur_min, blur_max = ranges["blur"] if options.effect_background else (0, 0)

    hook_texts = [line.strip() for line in options.hook_texts if line.strip()]
    hook_text = rng.choice(hook_texts or DEFAULT_HOOK_TEXTS) if options.effect_hook_caption else ""

    return {
        "profile": profile,
        "zoom": round(rng.uniform(zoom_min, zoom_max), 4),
        "x_offset": rng.randint(-offset, offset),
        "y_offset": rng.randint(-offset, offset),
        "speed": round(rng.uniform(speed_min, speed_max), 4),
        "saturation": round(rng.uniform(saturation_min, saturation_max), 3),
        "contrast": round(rng.uniform(contrast_min, contrast_max), 3),
        "brightness": round(rng.uniform(-0.018, 0.018), 3) if options.effect_color else 0,
        "hue": round(rng.uniform(-3.0, 3.0), 2) if options.effect_color else 0,
        "noise": rng.randint(noise_min, noise_max),
        "audio_noise": round(rng.uniform(0.0012, 0.003), 4) if options.effect_texture else 0,
        "audio_volume": round(rng.uniform(0.965, 0.995), 3),
        "audio_lowpass": rng.randint(16800, 18800),
        "background_blur": rng.randint(blur_min, blur_max) if blur_max else 0,
        "foreground_width": FOREGROUND_WIDTH if options.effect_background else EXPORT_WIDTH,
        "foreground_height": FOREGROUND_HEIGHT if options.effect_background else EXPORT_HEIGHT,
        "effect_background": options.effect_background,
        "effect_zoom": options.effect_zoom,
        "effect_color": options.effect_color,
        "effect_texture": options.effect_texture,
        "scratch_x_offset": rng.randint(-8, 8),
        "scratch_alpha": round(rng.uniform(0.18, 0.34), 3),
        "sweep_direction": rng.choice(["left-to-right", "top-to-bottom", "diagonal"]),
        "sweep_speed_name": (sweep_speed_name := rng.choice(["slow", "medium", "fast"])),
        "sweep_velocity": int({"slow": 170, "medium": 285, "fast": 430}[sweep_speed_name] * rng.uniform(0.95, 1.05)),
        "sweep_opacity_pct": rng.randint(10, 18),
        "sweep_line_width": rng.randint(3, 6),
        "sweep_glow_width": rng.randint(8, 16),
        "sweep_color": rng.choice(["white", "gold", "blue"]),
        "film_grain_intensity_pct": (grain_intensity := rng.randint(3, 15)),
        "film_grain_size": (grain_size := rng.choice(["small", "medium"])),
        "film_grain_opacity_pct": (grain_opacity := rng.randint(5, 20)),
        "film_grain_dynamic": True,
        "film_grain": max(1, int(grain_intensity * (grain_opacity / 20) * (1.0 if grain_size == "small" else 1.35))),
        "effect_speed": options.effect_speed,
        "effect_vignette": options.effect_vignette,
        "effect_center_scratch": options.effect_center_scratch,
        "effect_light_sweep": options.effect_light_sweep,
        "effect_film_grain": options.effect_film_grain,
        "effect_hook_caption": options.effect_hook_caption,
        "hook_text": hook_text,
        "hook_duration": max(1.0, min(float(options.hook_duration or 3.0), 8.0)),
    }




def _alpha(percent: int | float, multiplier: float = 1.0) -> float:
    return round(max(0.0, min(1.0, float(percent) / 100 * multiplier)), 3)


def _light_sweep_filters(effects: dict[str, Any]) -> list[str]:
    color_map = {
        "white": "white",
        "gold": "0xFFD166",
        "blue": "0x60A5FA",
    }
    color = color_map.get(str(effects.get("sweep_color", "white")), "white")
    direction = str(effects.get("sweep_direction", "left-to-right"))
    velocity = max(80, int(effects.get("sweep_velocity", 285)))
    line_width = max(1, min(8, int(effects.get("sweep_line_width", 3))))
    glow_width = max(line_width + 4, min(28, int(effects.get("sweep_glow_width", 14))))
    alpha = _alpha(effects.get("sweep_opacity_pct", 10))
    glow_alpha = _alpha(effects.get("sweep_opacity_pct", 10), 0.12)

    if direction == "top-to-bottom":
        y = f"mod(t*{velocity}\,ih+{glow_width * 2})-{glow_width * 2}"
        return [
            f"drawbox=x=0:y={y}:w=iw:h={glow_width}:color={color}@{glow_alpha}:t=fill",
            f"drawbox=x=0:y={y}+{max(0, (glow_width - line_width) // 2)}:w=iw:h={line_width}:color={color}@{alpha}:t=fill",
        ]

    x = f"mod(t*{velocity}\,iw+{glow_width * 2})-{glow_width * 2}"
    return [
        f"drawbox=x={x}:y=0:w={glow_width}:h=ih:color={color}@{glow_alpha}:t=fill",
        f"drawbox=x={x}+{max(0, (glow_width - line_width) // 2)}:y=0:w={line_width}:h=ih:color={color}@{alpha}:t=fill",
    ]



def _quote_filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _text_size(draw: Any, text: str, font: Any, stroke_width: int = 2) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    return box[2] - box[0], box[3] - box[1]


def _wrap_text(draw: Any, text: str, font: Any, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        width, _ = _text_size(draw, candidate, font)
        if width <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _caption_font_and_lines(draw: Any, text: str, font_file: Path | None) -> tuple[Any, list[str]]:
    from PIL import ImageFont

    max_text_width = EXPORT_WIDTH - 72
    min_size = 22
    start_size = max(30, int(EXPORT_WIDTH * 0.075))
    for font_size in range(start_size, min_size - 1, -2):
        font = ImageFont.truetype(str(font_file), font_size) if font_file else ImageFont.load_default()
        lines = _wrap_text(draw, text, font, max_text_width)
        if len(lines) <= 3 and all(_text_size(draw, line, font)[0] <= max_text_width for line in lines):
            return font, lines

    font = ImageFont.truetype(str(font_file), min_size) if font_file else ImageFont.load_default()
    lines = _wrap_text(draw, text, font, max_text_width)
    return font, lines[:3]


def _hook_caption_image(effects: dict[str, Any], output_root: Path, task_id: str) -> Path | None:
    text = str(effects.get("hook_text") or "").strip()
    if not effects.get("effect_hook_caption") or not text:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:
        raise RuntimeError("字幕钩子需要 Pillow 依赖，请重新安装 requirements。") from exc

    font_file = next((path for path in HOOK_FONT_CANDIDATES if path.exists()), None)
    padding_x = 26
    padding_y = 18
    line_gap = 8
    probe = Image.new("RGBA", (EXPORT_WIDTH, EXPORT_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    font, lines = _caption_font_and_lines(draw, text, font_file)
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
    text_width = min(EXPORT_WIDTH - padding_x * 2, max(box[2] - box[0] for box in boxes))
    line_heights = [box[3] - box[1] for box in boxes]
    text_height = sum(line_heights) + line_gap * (len(lines) - 1)
    panel_width = min(EXPORT_WIDTH - 36, text_width + padding_x * 2)
    panel_height = text_height + padding_y * 2

    image = Image.new("RGBA", (EXPORT_WIDTH, panel_height + 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    left = (EXPORT_WIDTH - panel_width) // 2
    draw.rounded_rectangle(
        (left, 4, left + panel_width, 4 + panel_height),
        radius=18,
        fill=(0, 0, 0, 118),
        outline=(255, 255, 255, 72),
        width=2,
    )
    y = 4 + padding_y
    for line, line_height in zip(lines, line_heights):
        line_box = draw.textbbox((0, 0), line, font=font, stroke_width=2)
        x = (EXPORT_WIDTH - (line_box[2] - line_box[0])) // 2
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 170), stroke_width=2, stroke_fill=(0, 0, 0, 200))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(20, 20, 20, 235))
        y += line_height + line_gap

    image_path = output_root / f"{task_id}_hook.png"
    image.save(image_path)
    return image_path


def _video_filter(effects: dict[str, Any], *, include_texture: bool = True, hook_image: Path | None = None) -> str:
    zoom = effects["zoom"]
    speed = effects["speed"]
    saturation = effects["saturation"]
    contrast = effects["contrast"]
    brightness = effects["brightness"]
    hue = effects["hue"]
    blur = effects["background_blur"]
    foreground_width = _even(effects["foreground_width"] * zoom)
    foreground_height = _even(effects["foreground_height"] * zoom)

    if effects.get("effect_background"):
        base = (
            f"[0:v]setpts=PTS/{speed},split=2[bg][fg];"
            f"[bg]scale={EXPORT_WIDTH}:{EXPORT_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={EXPORT_WIDTH}:{EXPORT_HEIGHT},gblur=sigma={blur},"
            f"eq=saturation={saturation}:contrast={contrast}:brightness={brightness},"
            f"hue=h={hue},fps={EXPORT_FPS},setsar=1[bgv];"
            f"[fg]scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=increase,"
            f"crop={foreground_width}:{foreground_height},"
            f"eq=saturation={saturation}:contrast={contrast}:brightness={brightness},"
            f"hue=h={hue},fps={EXPORT_FPS},setsar=1[fgv];"
            f"[bgv][fgv]overlay=(W-w)/2+{effects['x_offset']}:(H-h)/2+{effects['y_offset']}"
        )
    else:
        base = (
            f"[0:v]setpts=PTS/{speed},"
            f"scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=increase,"
            f"crop={EXPORT_WIDTH}:{EXPORT_HEIGHT},"
            f"eq=saturation={saturation}:contrast={contrast}:brightness={brightness},"
            f"hue=h={hue},fps={EXPORT_FPS},setsar=1"
        )

    texture_filters = []
    if include_texture and effects.get("effect_texture") and effects.get("noise", 0) > 0:
        texture_filters.append(f"noise=alls={effects['noise']}:allf=t+u")
    if include_texture and effects.get("effect_film_grain"):
        texture_filters.append(f"noise=alls={effects['film_grain']}:allf=t+u")
    if include_texture and effects.get("effect_center_scratch"):
        scratch_x = f"(w/2)+{effects['scratch_x_offset']}"
        texture_filters.append(
            f"drawbox=x={scratch_x}:y=0:w=2:h=ih:color=white@{effects['scratch_alpha']}:t=fill"
        )
        texture_filters.append(
            f"drawbox=x={scratch_x}-5:y=0:w=12:h=ih:color=white@0.035:t=fill"
        )
    if include_texture and effects.get("effect_light_sweep"):
        texture_filters.extend(_light_sweep_filters(effects))
    if include_texture and effects.get("effect_vignette"):
        texture_filters.append("vignette=PI/7")
    suffix = "," + ",".join(texture_filters) if texture_filters else ""
    filtered = f"{base}{suffix}"
    if hook_image:
        duration = max(1.0, min(float(effects.get("hook_duration") or 3.0), 8.0))
        return (
            f"{filtered},format=rgba[vbase];"
            f"movie='{_quote_filter_path(hook_image)}',format=rgba[hook];"
            f"[vbase][hook]overlay=x=0:y=h*0.14:enable='between(t,0,{duration})',format=yuv420p[v]"
        )
    return f"{filtered},format=yuv420p[v]"


def _render(input_path: Path, temp_path: Path, effects: dict[str, Any], info: VideoInfo, *, include_texture: bool, hook_image: Path | None = None, cancel_task_id: str | None = None) -> None:
    command = [
        ffmpeg_bin(),
        "-y",
        "-fflags",
        "+discardcorrupt",
        "-i",
        str(input_path),
    ]
    filter_complex = _video_filter(effects, include_texture=include_texture, hook_image=hook_image)
    if info.has_audio:
        filter_complex += (
            f";[0:a]aresample={EXPORT_AUDIO_SAMPLE_RATE},atempo={effects['speed']},"
            f"volume={effects['audio_volume']},highpass=f=35,"
            f"lowpass=f={effects['audio_lowpass']}[a0];"
            f"[1:a]volume={effects['audio_noise']}[noise];"
            f"[a0][noise]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
        command += [
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=pink:sample_rate={EXPORT_AUDIO_SAMPLE_RATE}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-shortest",
        ]
    else:
        command += [
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=channel_layout=stereo:sample_rate={EXPORT_AUDIO_SAMPLE_RATE}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-shortest",
        ]
    command += [*_mp4_compat_args(), str(temp_path)]
    _run(command, task_id=cancel_task_id)


def _render_with_original_audio(input_path: Path, temp_path: Path, effects: dict[str, Any], info: VideoInfo, *, include_texture: bool, hook_image: Path | None = None, cancel_task_id: str | None = None) -> None:
    command = [
        ffmpeg_bin(),
        "-y",
        "-fflags",
        "+discardcorrupt",
        "-i",
        str(input_path),
        "-filter_complex",
        _video_filter(effects, include_texture=include_texture, hook_image=hook_image),
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-shortest",
        *_mp4_compat_args(),
        str(temp_path),
    ]
    _run(command, task_id=cancel_task_id)


def render_variant(
    *,
    input_video: Union[str, Path],
    output_dir: Union[str, Path],
    task_id: str,
    options: VariantOptions,
    output_stem: str,
    cancel_task_id: str | None = None,
) -> tuple[Path, dict[str, Any], VideoInfo]:
    input_path = Path(input_video)
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"{output_stem}.mp4"
    temp_path = output_root / f"{task_id}_tmp.mp4"
    info = get_video_info(input_path)
    effects = build_effects(task_id, options)
    hook_image = _hook_caption_image(effects, output_root, task_id)

    try:
        _render(input_path, temp_path, effects, info, include_texture=True, hook_image=hook_image, cancel_task_id=cancel_task_id or task_id)
    except CancelledTask:
        if temp_path.exists():
            temp_path.unlink()
        raise
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        try:
            _render(input_path, temp_path, effects, info, include_texture=False, hook_image=hook_image, cancel_task_id=cancel_task_id or task_id)
        except CancelledTask:
            if temp_path.exists():
                temp_path.unlink()
            raise
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            if info.has_audio:
                _render_with_original_audio(input_path, temp_path, effects, info, include_texture=False, hook_image=hook_image, cancel_task_id=cancel_task_id or task_id)
            else:
                raise

    if output_path.exists():
        output_path.unlink()
    shutil.move(str(temp_path), str(output_path))
    if hook_image and hook_image.exists():
        hook_image.unlink()
    return output_path, effects, info


def merge_videos(
    *,
    input_paths: list[Union[str, Path]],
    work_dir: Union[str, Path],
    task_id: str,
) -> Path:
    """Normalize uploaded clips, then concat them in upload order."""
    if not input_paths:
        raise ValueError("没有可合并的视频。")
    paths = [Path(path) for path in input_paths]
    if len(paths) == 1:
        return paths[0]

    root = Path(work_dir) / f"{task_id}_merge_work"
    root.mkdir(parents=True, exist_ok=True)
    normalized_paths: list[Path] = []

    for index, path in enumerate(paths, start=1):
        normalized = root / f"clip_{index:03d}.mp4"
        info = get_video_info(path)
        command = [
            ffmpeg_bin(),
            "-y",
            "-fflags",
            "+discardcorrupt",
            "-i",
            str(path),
        ]
        if info.has_audio:
            command += [
                "-filter_complex",
                _fit_pad_filter("0:v", "v") + ";"
                f"[0:a]aformat=sample_rates={EXPORT_AUDIO_SAMPLE_RATE}:channel_layouts=stereo[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
            ]
        else:
            command += [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={EXPORT_AUDIO_SAMPLE_RATE}",
                "-filter_complex",
                _fit_pad_filter("0:v", "v"),
                "-map",
                "[v]",
                "-map",
                "1:a",
                "-shortest",
            ]
        command += [
            *_mp4_compat_args(),
            str(normalized),
        ]
        if is_cancel_requested(task_id):
            raise CancelledTask("任务已取消。")
        _run(command, task_id=task_id)
        normalized_paths.append(normalized)

    concat_file = root / "concat.txt"
    concat_file.write_text("\n".join(_quote_concat_path(path) for path in normalized_paths), encoding="utf-8")
    merged_path = root / f"{task_id}_merged.mp4"
    if is_cancel_requested(task_id):
        raise CancelledTask("任务已取消。")
    _run(
        [
            ffmpeg_bin(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(merged_path),
        ],
        task_id=task_id,
    )
    return merged_path



def _segment_lengths(total_seconds: float, min_seconds: float, max_seconds: float, seed: str) -> list[float]:
    rng = random.Random(seed)
    total = max(0.0, float(total_seconds))
    low = max(1.0, float(min_seconds))
    high = max(low, float(max_seconds))
    if total <= 0:
        return []
    if total <= high:
        return [round(total, 3)]

    min_count = int((total + high - 0.000001) // high)
    if total % high:
        min_count += 1
    max_count = int(total // low)

    if min_count <= max_count and max_count > 0:
        count = rng.randint(min_count, max_count)
        lengths: list[float] = []
        remaining = total
        for index in range(count):
            left = count - index
            if left == 1:
                length = remaining
            else:
                min_allowed = max(low, remaining - high * (left - 1))
                max_allowed = min(high, remaining - low * (left - 1))
                length = rng.uniform(min_allowed, max_allowed)
            length = round(length, 3)
            lengths.append(length)
            remaining = round(remaining - length, 3)
        if lengths and abs(sum(lengths) - total) > 0.01:
            lengths[-1] = round(lengths[-1] + total - sum(lengths), 3)
        return lengths

    lengths = []
    remaining = total
    while remaining > 0.001:
        if remaining <= high:
            lengths.append(round(remaining, 3))
            break
        length = round(rng.uniform(low, high), 3)
        lengths.append(length)
        remaining = round(remaining - length, 3)
    return lengths


def split_video_by_random_range(
    *,
    input_video: Union[str, Path],
    output_dir: Union[str, Path],
    task_id: str,
    min_seconds: float,
    max_seconds: float,
    output_stem: str,
) -> list[dict[str, Any]]:
    input_path = Path(input_video)
    output_root = Path(output_dir) / f"{task_id}_split"
    output_root.mkdir(parents=True, exist_ok=True)
    info = get_video_info(input_path)
    lengths = _segment_lengths(info.duration, min_seconds, max_seconds, task_id)
    if not lengths:
        raise ValueError("视频时长无效，无法切分。")

    results: list[dict[str, Any]] = []
    cursor = 0.0
    for index, duration in enumerate(lengths, start=1):
        output_path = output_root / f"{output_stem}_part_{index:03d}_{int(round(duration))}s.mp4"
        command = [
            ffmpeg_bin(),
            "-y",
            "-ss",
            f"{cursor:.3f}",
            "-i",
            str(input_path),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            _fit_pad_filter("0:v", "v").removeprefix("[0:v]").removesuffix("[v]"),
            *_mp4_compat_args(),
            str(output_path),
        ]
        if is_cancel_requested(task_id):
            raise CancelledTask("任务已取消。")
        _run(command, task_id=task_id)
        results.append({"path": str(output_path), "start": round(cursor, 3), "duration": round(duration, 3)})
        cursor = round(cursor + duration, 3)
    return results

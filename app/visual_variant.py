from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path
from typing import Any, Union

from .models import VariantOptions, VideoInfo
from .video_utils import ffmpeg_bin, get_video_info


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"FFmpeg 执行失败，code={result.returncode}: {stderr[-1600:]}")


def _even(value: float) -> int:
    number = max(2, int(value))
    return number if number % 2 == 0 else number - 1


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
        "background_blur": rng.randint(blur_min, blur_max) if blur_max else 0,
        "foreground_width": 970 if options.effect_background else 1080,
        "foreground_height": 1724 if options.effect_background else 1920,
        "effect_background": options.effect_background,
        "effect_zoom": options.effect_zoom,
        "effect_color": options.effect_color,
        "effect_texture": options.effect_texture,
        "effect_speed": options.effect_speed,
        "effect_vignette": options.effect_vignette,
    }


def _video_filter(effects: dict[str, Any], *, include_texture: bool = True) -> str:
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
            f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,gblur=sigma={blur},"
            f"eq=saturation={saturation}:contrast={contrast}:brightness={brightness},"
            f"hue=h={hue},fps=30,setsar=1[bgv];"
            f"[fg]scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=decrease,"
            f"eq=saturation={saturation}:contrast={contrast}:brightness={brightness},"
            f"hue=h={hue},fps=30,setsar=1[fgv];"
            f"[bgv][fgv]overlay=(W-w)/2+{effects['x_offset']}:(H-h)/2+{effects['y_offset']}"
        )
    else:
        base = (
            f"[0:v]setpts=PTS/{speed},"
            f"scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,"
            f"eq=saturation={saturation}:contrast={contrast}:brightness={brightness},"
            f"hue=h={hue},fps=30,setsar=1"
        )

    texture_filters = []
    if include_texture and effects.get("effect_texture") and effects.get("noise", 0) > 0:
        texture_filters.append(f"noise=alls={effects['noise']}:allf=t+u")
    if include_texture and effects.get("effect_vignette"):
        texture_filters.append("vignette=PI/7")
    suffix = "," + ",".join(texture_filters) if texture_filters else ""
    return f"{base}{suffix},format=yuv420p[v]"


def _render(input_path: Path, temp_path: Path, effects: dict[str, Any], info: VideoInfo, *, include_texture: bool) -> None:
    command = [
        ffmpeg_bin(),
        "-y",
        "-fflags",
        "+discardcorrupt",
        "-i",
        str(input_path),
    ]
    filter_complex = _video_filter(effects, include_texture=include_texture)
    if info.has_audio:
        filter_complex += f";[0:a]atempo={effects['speed']},volume=0.98[a]"
        command += [
            "-filter_complex",
            filter_complex,
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
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-shortest",
        ]
    command += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(temp_path),
    ]
    _run(command)


def render_variant(
    *,
    input_video: Union[str, Path],
    output_dir: Union[str, Path],
    task_id: str,
    options: VariantOptions,
    output_stem: str,
) -> tuple[Path, dict[str, Any], VideoInfo]:
    input_path = Path(input_video)
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"{output_stem}_visual_variant_{task_id[:6]}.mp4"
    temp_path = output_root / f"{task_id}_tmp.mp4"
    info = get_video_info(input_path)
    effects = build_effects(task_id, options)

    try:
        _render(input_path, temp_path, effects, info, include_texture=True)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        _render(input_path, temp_path, effects, info, include_texture=False)

    shutil.move(str(temp_path), str(output_path))
    return output_path, effects, info

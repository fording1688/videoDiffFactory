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


def _quote_concat_path(path: Path) -> str:
    return "file '" + str(path).replace("'", "'\\''") + "'"


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
        "audio_noise": round(rng.uniform(0.0012, 0.003), 4) if options.effect_texture else 0,
        "audio_volume": round(rng.uniform(0.965, 0.995), 3),
        "audio_lowpass": rng.randint(16800, 18800),
        "background_blur": rng.randint(blur_min, blur_max) if blur_max else 0,
        "foreground_width": 972 if options.effect_background else 1080,
        "foreground_height": 1728 if options.effect_background else 1920,
        "effect_background": options.effect_background,
        "effect_zoom": options.effect_zoom,
        "effect_color": options.effect_color,
        "effect_texture": options.effect_texture,
        "scratch_x_offset": rng.randint(-8, 8),
        "scratch_alpha": round(rng.uniform(0.18, 0.34), 3),
        "sweep_width": rng.randint(170, 260),
        "sweep_alpha": round(rng.uniform(0.10, 0.18), 3),
        "sweep_speed": round(rng.uniform(0.75, 1.25), 3),
        "film_grain": rng.randint(6, 12),
        "effect_speed": options.effect_speed,
        "effect_vignette": options.effect_vignette,
        "effect_center_scratch": options.effect_center_scratch,
        "effect_light_sweep": options.effect_light_sweep,
        "effect_film_grain": options.effect_film_grain,
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
            f"[fg]scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=increase,"
            f"crop={foreground_width}:{foreground_height},"
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
        sweep_width = effects["sweep_width"]
        sweep_alpha = effects["sweep_alpha"]
        sweep_speed = effects["sweep_speed"]
        sweep_x = f"mod(t*360*{sweep_speed},w+{sweep_width * 2})-{sweep_width * 2}"
        texture_filters.append(
            f"drawbox=x={sweep_x}:y=0:w={sweep_width}:h=ih:color=white@{sweep_alpha}:t=fill"
        )
        texture_filters.append(
            f"drawbox=x={sweep_x}+{sweep_width}:y=0:w={max(30, sweep_width // 4)}:h=ih:color=white@0.06:t=fill"
        )
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
        filter_complex += (
            f";[0:a]aresample=44100,atempo={effects['speed']},"
            f"volume={effects['audio_volume']},highpass=f=35,"
            f"lowpass=f={effects['audio_lowpass']}[a0];"
            f"[1:a]volume={effects['audio_noise']}[noise];"
            f"[a0][noise]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
        command += [
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=pink:sample_rate=44100",
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


def _render_with_original_audio(input_path: Path, temp_path: Path, effects: dict[str, Any], info: VideoInfo, *, include_texture: bool) -> None:
    command = [
        ffmpeg_bin(),
        "-y",
        "-fflags",
        "+discardcorrupt",
        "-i",
        str(input_path),
        "-filter_complex",
        _video_filter(effects, include_texture=include_texture),
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-shortest",
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
        try:
            _render(input_path, temp_path, effects, info, include_texture=False)
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            if info.has_audio:
                _render_with_original_audio(input_path, temp_path, effects, info, include_texture=False)
            else:
                raise

    shutil.move(str(temp_path), str(output_path))
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
                "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
                "fps=30,setsar=1,format=yuv420p[v];"
                "[0:a]aformat=sample_rates=44100:channel_layouts=stereo[a]",
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
                "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
                "fps=30,setsar=1,format=yuv420p[v]",
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
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(normalized),
        ]
        _run(command)
        normalized_paths.append(normalized)

    concat_file = root / "concat.txt"
    concat_file.write_text("\n".join(_quote_concat_path(path) for path in normalized_paths), encoding="utf-8")
    merged_path = root / f"{task_id}_merged.mp4"
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
        ]
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
            str(output_path),
        ]
        _run(command)
        results.append({"path": str(output_path), "start": round(cursor, 3), "duration": round(duration, 3)})
        cursor = round(cursor + duration, 3)
    return results

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Union

from .models import VideoInfo


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def asset_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


def user_data_root() -> Path:
    configured = os.getenv("VIDEO_VARIANT_DATA_DIR")
    if configured:
        return Path(configured).expanduser()

    if getattr(sys, "frozen", False):
        if platform.system().lower() == "darwin":
            return Path.home() / "Movies" / "VideoVariantStudio"
        return Path.home() / "VideoVariantStudio"

    return app_root() / "data"


def runtime_platform_dir() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    is_arm = machine in {"arm64", "aarch64"} or machine.startswith("arm")
    is_x64 = machine in {"x86_64", "amd64"} or "64" in machine

    if system == "darwin":
        return "mac-arm64" if is_arm else "mac-x64"
    if system == "windows":
        return "windows-x64" if is_x64 else "windows"
    if system == "linux":
        return "linux-arm64" if is_arm else "linux-x64"
    return f"{system}-{machine}".strip("-") or "unknown"


def _candidate_binaries(name: str) -> list[Path]:
    runtime = asset_root() / "runtime" / "ffmpeg"
    suffixes = [".exe", ""] if os.name == "nt" else [""]
    platform_runtime = runtime / runtime_platform_dir()
    candidates: list[Path] = []
    for suffix in suffixes:
        candidates.append(platform_runtime / f"{name}{suffix}")
        # Backward-compatible single-folder layout.
        candidates.append(runtime / f"{name}{suffix}")
    return candidates


def find_binary(name: str) -> str:
    env_name = f"VIDEO_VARIANT_{name.upper()}"
    env_value = os.getenv(env_name)
    if env_value and Path(env_value).exists():
        return env_value
    for candidate in _candidate_binaries(name):
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    target_dir = asset_root() / "runtime" / "ffmpeg" / runtime_platform_dir()
    raise RuntimeError(
        f"找不到 {name}。请把 {name} 可执行文件放到 {target_dir}，"
        f"或先安装 FFmpeg 并确保命令可在终端中运行。"
    )


def ffmpeg_bin() -> str:
    return find_binary("ffmpeg")


def ffprobe_bin() -> str:
    return find_binary("ffprobe")


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=True)


def check_runtime() -> dict[str, str | bool]:
    try:
        ffmpeg = ffmpeg_bin()
        ffprobe = ffprobe_bin()
        run_command([ffmpeg, "-version"])
        run_command([ffprobe, "-version"])
        return {"ok": True, "platform": runtime_platform_dir(), "ffmpeg": ffmpeg, "ffprobe": ffprobe}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_video_info(video_path: Union[str, Path]) -> VideoInfo:
    command = [
        ffprobe_bin(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(video_path),
    ]
    payload = json.loads(run_command(command).stdout or "{}")
    streams = payload.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    duration = float(payload.get("format", {}).get("duration") or video_stream.get("duration") or 0)
    fps_text = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = fps_text.split("/")
        fps = float(numerator) / max(float(denominator), 1)
    except Exception:
        fps = 0
    return VideoInfo(
        duration=round(duration, 3),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=round(fps, 3),
        has_audio=bool(audio_stream),
        video_codec=video_stream.get("codec_name"),
        audio_codec=audio_stream.get("codec_name"),
    )


def safe_stem(filename: str) -> str:
    stem = Path(filename).stem.strip() or "video"
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in stem)
    return safe[:90] or "video"

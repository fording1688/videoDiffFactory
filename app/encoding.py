from __future__ import annotations

import platform
import subprocess
from functools import lru_cache
from typing import Any, Mapping

from .video_utils import ffmpeg_bin


def _hardware_encoder_works(encoder: str) -> bool:
    """执行一帧真实编码；比只检查 ``ffmpeg -encoders`` 更可靠。"""
    try:
        result = subprocess.run(
            [
                ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:size=1280x720:rate=30",
                "-frames:v", "1", "-an", "-pix_fmt", "yuv420p",
                "-c:v", encoder, "-b:v", "4M", "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _hardware_candidates(codec_family: str) -> list[str]:
    prefix = "hevc" if codec_family == "hevc" else "h264"
    system = platform.system().lower()
    if system == "darwin":
        return [f"{prefix}_videotoolbox"]
    if system == "windows":
        return [f"{prefix}_nvenc", f"{prefix}_qsv", f"{prefix}_amf"]
    if system == "linux":
        return [f"{prefix}_nvenc", f"{prefix}_qsv"]
    return []


@lru_cache(maxsize=4)
def select_video_encoder(requested: str = "auto", codec_family: str = "h264") -> str:
    """选择可工作的硬件编码器；无硬件时自动降级到 CPU。"""
    requested = str(requested or "auto").strip().lower()
    codec_family = "hevc" if str(codec_family).lower() == "hevc" else "h264"
    cpu_fallback = "libx265" if codec_family == "hevc" else "libx264"
    if requested not in {"", "auto"}:
        if requested in {"libx264", "libx265"}:
            return requested
        return requested if _hardware_encoder_works(requested) else cpu_fallback
    for candidate in _hardware_candidates(codec_family):
        if _hardware_encoder_works(candidate):
            return candidate
    return cpu_fallback


def video_encode_args(config: Mapping[str, Any]) -> tuple[str, list[str]]:
    """返回编码器名称及与该编码器兼容的 FFmpeg 输出参数。"""
    family = "hevc" if str(config.get("codec_family") or "h264").lower() == "hevc" else "h264"
    encoder = select_video_encoder(str(config.get("video_codec") or "auto"), family)
    pixel_format = str(config.get("pixel_format") or "yuv420p")
    bitrate = str(config.get("video_bitrate") or "4M")
    quality = max(22, min(int(config.get("crf") or 25), 26))
    common = ["-pix_fmt", pixel_format, "-threads", "0"]

    if encoder.endswith("_videotoolbox"):
        return encoder, ["-c:v", encoder, "-b:v", bitrate, "-realtime", "true", "-allow_sw", "1", *common]
    if encoder.endswith("_nvenc"):
        return encoder, [
            "-c:v", encoder, "-preset", "p4", "-tune", "hq",
            "-rc", "vbr", "-cq", str(quality), "-b:v", "0", *common,
        ]
    if encoder.endswith("_qsv"):
        return encoder, [
            "-c:v", encoder, "-preset", "veryfast", "-global_quality", str(quality), *common,
        ]
    if encoder.endswith("_amf"):
        return encoder, [
            "-c:v", encoder, "-quality", "speed", "-qp_i", str(quality), "-qp_p", str(quality), *common,
        ]

    preset = str(config.get("preset") or "ultrafast").lower()
    if preset not in {"ultrafast", "superfast"}:
        preset = "ultrafast"
    return encoder, ["-c:v", encoder, "-preset", preset, "-crf", str(quality), *common]

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .cancel import run_cancellable
from .encoding import video_encode_args
from .video_utils import ffmpeg_bin, get_video_info


DEFAULT_VIDEO_PARAMETERS: dict[str, Any] = {
    "metadata": {
        "strip_all": True,
        "project_name": "VideoVariantStudio",
        "project_version": "1.0",
        "title": None,
        "comment": "Processed with an authorized video editing workflow",
    },
    "region": {
        "enabled": False,
        "mode": "blur",  # blur | crop_bottom
        # 归一化坐标，(0, 0) 是左上角；默认处理底部 15%。
        "x": 0.0,
        "y": 0.85,
        "width": 1.0,
        "height": 0.15,
        "blur_sigma": 18.0,
        # crop_bottom 模式移除底部比例，然后拉伸回原分辨率。
        "crop_bottom_ratio": 0.15,
    },
    "composition": {
        "style_overlay": {
            "enabled": True,
            "mode": "film",  # film | warm | cool | contrast | vignette
            "opacity": 0.18,
            "grain_strength": 2.0,
        },
        "watermark": {
            "path": None,
            "opacity": 0.25,
            "width_ratio": 0.18,
            "position": "top_right",
            "margin": 24,
        },
        "pip": {
            "enabled": False,
            "path": None,
            "width_ratio": 0.30,
            "position": "bottom_right",
            "margin": 24,
            "start": 0.0,
            "end": None,
        },
        "border": {
            "enabled": False,
            "width": 4,
            "color": "white@0.9",
        },
        "intro": {"path": None, "duration": 0.6, "fade": 0.2},
        "outro": {"path": None, "duration": 0.6, "fade": 0.2},
    },
    "temporal": {
        "trim_head_seconds": 0.0,
        "trim_tail_seconds": 0.0,
        "start_time": None,
        "end_time": None,
        "target_fps": 30,
    },
    "quality": {
        "sharpen": 0.30,
        "contrast": 1.04,
        "brightness": 0.0,
        "saturation": 1.03,
        "color_smoothing": 0.8,
    },
    "output": {
        "video_codec": "auto",
        "codec_family": "h264",
        "video_bitrate": "4M",
        "preset": "ultrafast",
        "crf": 25,
        "pixel_format": "yuv420p",
        "audio_codec": "aac",
        "audio_bitrate": "192k",
        "audio_sample_rate": 48000,
    },
}


@dataclass(frozen=True)
class ProcessingPlan:
    width: int
    height: int
    source_duration: float
    start_time: float
    end_time: float
    output_duration: float
    target_fps: int

    def as_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


class VideoProcessor:
    """隐私元数据清理、常规剪辑、品牌合成与画质优化流水线。"""

    def __init__(self, parameters: Mapping[str, Any] | None = None) -> None:
        self.parameters = copy.deepcopy(DEFAULT_VIDEO_PARAMETERS)
        if parameters:
            self._deep_update(self.parameters, parameters)
        self._validate()

    @staticmethod
    def _deep_update(target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                VideoProcessor._deep_update(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    @staticmethod
    def _even(value: float) -> int:
        result = max(2, int(value))
        return result if result % 2 == 0 else result - 1

    def _validate(self) -> None:
        region = self.parameters["region"]
        if region["mode"] not in {"blur", "crop_bottom"}:
            raise ValueError("region.mode 仅支持 blur 或 crop_bottom")
        for name in ("x", "y", "width", "height"):
            value = float(region[name])
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"region.{name} 必须在 0-1 之间")
        if float(region["x"]) + float(region["width"]) > 1.0 or float(region["y"]) + float(region["height"]) > 1.0:
            raise ValueError("局部处理区域不能超出画面")
        if not 0.0 <= float(region["crop_bottom_ratio"]) < 0.8:
            raise ValueError("region.crop_bottom_ratio 必须在 0-0.8 之间")
        composition = self.parameters["composition"]
        style = composition["style_overlay"]
        if style["mode"] not in {"film", "warm", "cool", "contrast", "vignette"}:
            raise ValueError("style_overlay.mode 不支持")
        if not 0.0 <= float(style["opacity"]) <= 1.0:
            raise ValueError("style_overlay.opacity 必须在 0-1 之间")
        if composition["watermark"].get("path") and float(composition["watermark"]["opacity"]) < 0.15:
            raise ValueError("品牌水印必须明确可见，opacity 不能低于 0.15")
        if not 1 <= int(self.parameters["temporal"]["target_fps"]) <= 120:
            raise ValueError("target_fps 必须在 1-120 之间")

    def create_plan(self, input_path: str | Path) -> ProcessingPlan:
        info = get_video_info(input_path)
        if not info.width or not info.height or info.duration <= 0:
            raise ValueError(f"无法读取有效的视频信息: {input_path}")
        temporal = self.parameters["temporal"]
        explicit_start = temporal.get("start_time")
        explicit_end = temporal.get("end_time")
        start = float(explicit_start) if explicit_start is not None else float(temporal["trim_head_seconds"])
        end = float(explicit_end) if explicit_end is not None else info.duration - float(temporal["trim_tail_seconds"])
        start, end = max(0.0, start), min(info.duration, end)
        if end <= start:
            raise ValueError(f"无效剪辑区间：start={start:.3f}, end={end:.3f}")
        return ProcessingPlan(
            width=info.width,
            height=info.height,
            source_duration=info.duration,
            start_time=start,
            end_time=end,
            output_duration=end - start,
            target_fps=int(temporal["target_fps"]),
        )

    @staticmethod
    def _position(position: str, margin: int) -> tuple[str, str]:
        choices = {
            "top_left": (str(margin), str(margin)),
            "top_right": (f"W-w-{margin}", str(margin)),
            "bottom_left": (str(margin), f"H-h-{margin}"),
            "bottom_right": (f"W-w-{margin}", f"H-h-{margin}"),
            "center": ("(W-w)/2", "(H-h)/2"),
        }
        return choices.get(position, choices["top_right"])

    def _base_video_filters(self, plan: ProcessingPlan) -> tuple[list[str], str]:
        quality = self.parameters["quality"]
        chain = [f"trim=start={plan.start_time:.6f}:end={plan.end_time:.6f}", "setpts=PTS-STARTPTS"]
        smoothing = max(0.0, float(quality["color_smoothing"]))
        if smoothing:
            chain.append(f"hqdn3d={smoothing:.3f}:{smoothing:.3f}:{smoothing * 1.5:.3f}:{smoothing * 1.5:.3f}")
        chain.append(
            f"eq=brightness={float(quality['brightness']):.5f}:"
            f"contrast={float(quality['contrast']):.5f}:saturation={float(quality['saturation']):.5f}"
        )
        sharpen = max(0.0, float(quality["sharpen"]))
        if sharpen:
            chain.append(f"unsharp=5:5:{sharpen:.3f}:5:5:0")
        chain += [f"fps={plan.target_fps}", "setsar=1", "format=yuv420p"]
        return [f"[0:v]{','.join(chain)}[video-base]"], "video-base"

    def _region_filters(self, current: str, plan: ProcessingPlan, filters: list[str]) -> str:
        region = self.parameters["region"]
        if not region.get("enabled"):
            return current
        if region["mode"] == "crop_bottom":
            visible_height = self._even(plan.height * (1.0 - float(region["crop_bottom_ratio"])))
            filters.append(
                f"[{current}]crop={plan.width}:{visible_height}:0:0,"
                f"scale={plan.width}:{plan.height}:flags=lanczos[video-region]"
            )
            return "video-region"
        x = self._even(plan.width * float(region["x"]))
        y = self._even(plan.height * float(region["y"]))
        width = min(self._even(plan.width * float(region["width"])), plan.width - x)
        height = min(self._even(plan.height * float(region["height"])), plan.height - y)
        sigma = max(0.1, float(region["blur_sigma"]))
        filters += [
            f"[{current}]split=2[region-background][region-source]",
            f"[region-source]crop={width}:{height}:{x}:{y},gblur=sigma={sigma:.3f}[region-blurred]",
            f"[region-background][region-blurred]overlay={x}:{y}[video-region]",
        ]
        return "video-region"

    def _composition_filters(self, current: str, plan: ProcessingPlan, filters: list[str], indices: dict[str, int]) -> str:
        composition = self.parameters["composition"]
        if "watermark" in indices:
            cfg = composition["watermark"]
            width = self._even(plan.width * float(cfg["width_ratio"]))
            opacity = min(max(float(cfg["opacity"]), 0.15), 1.0)
            filters.append(f"[{indices['watermark']}:v]scale={width}:-2,format=rgba,colorchannelmixer=aa={opacity:.4f}[brand-watermark]")
            x, y = self._position(str(cfg["position"]), int(cfg["margin"]))
            filters.append(f"[{current}][brand-watermark]overlay={x}:{y}:eof_action=repeat[video-watermarked]")
            current = "video-watermarked"
        if "pip" in indices and composition["pip"].get("enabled"):
            cfg = composition["pip"]
            width = self._even(plan.width * float(cfg["width_ratio"]))
            start = max(0.0, float(cfg["start"]))
            filters.append(f"[{indices['pip']}:v]scale={width}:-2,setpts=PTS-STARTPTS+{start:.6f}/TB[pip-video]")
            x, y = self._position(str(cfg["position"]), int(cfg["margin"]))
            end = cfg.get("end")
            enable = f":enable='between(t,{start:.6f},{float(end):.6f})'" if end is not None else f":enable='gte(t,{start:.6f})'"
            filters.append(f"[{current}][pip-video]overlay={x}:{y}:eof_action=pass:shortest=0{enable}[video-pip]")
            current = "video-pip"
        border = composition["border"]
        if border.get("enabled"):
            width = max(1, int(border["width"]))
            filters.append(f"[{current}]drawbox=x=0:y=0:w=iw:h=ih:color={border['color']}:t={width}[video-border]")
            current = "video-border"
        return current

    def _style_overlay_filters(self, current: str, filters: list[str]) -> str:
        """从主画面生成全屏风格层，再按 alpha 与原画面混合。"""
        style = self.parameters["composition"]["style_overlay"]
        if not style.get("enabled") or float(style["opacity"]) <= 0:
            return current
        mode = str(style["mode"])
        grain = max(0.0, float(style["grain_strength"]))
        presets = {
            "film": f"eq=contrast=1.06:saturation=0.98,curves=all='0/0 0.25/0.23 0.75/0.78 1/1',noise=alls={grain:.3f}:allf=t+u",
            "warm": "colorbalance=rs=.035:gs=.010:bs=-.025,eq=saturation=1.03",
            "cool": "colorbalance=rs=-.020:gs=.005:bs=.035,eq=saturation=1.02",
            "contrast": "eq=contrast=1.10:saturation=1.02",
            "vignette": "vignette=PI/7:eval=frame",
        }
        opacity = float(style["opacity"])
        filters += [
            f"[{current}]split=2[style-original][style-source]",
            f"[style-source]{presets[mode]},format=rgba,colorchannelmixer=aa={opacity:.4f}[style-layer]",
            "[style-original][style-layer]overlay=0:0:eof_action=pass[video-styled]",
        ]
        return "video-styled"

    def _intro_outro_filters(
        self,
        video_label: str,
        audio_label: str | None,
        plan: ProcessingPlan,
        filters: list[str],
        indices: dict[str, int],
    ) -> tuple[str, str | None]:
        composition = self.parameters["composition"]
        active = [name for name in ("intro", "outro") if name in indices]
        if not active:
            return video_label, audio_label

        segment_videos: list[str] = []
        segment_audio_kinds: list[str] = []
        durations: dict[str, float] = {}
        for name in active:
            source_duration = get_video_info(composition[name]["path"]).duration
            configured = min(max(float(composition[name]["duration"]), 0.3), 1.0)
            durations[name] = min(source_duration, configured)

        if "intro" in indices:
            duration = durations["intro"]
            fade = min(max(float(composition["intro"]["fade"]), 0.0), duration / 2)
            filters.append(
                f"[{indices['intro']}:v]scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,"
                f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2,fps={plan.target_fps},"
                f"trim=duration={duration:.6f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p,"
                f"fade=t=out:st={max(0.0, duration - fade):.6f}:d={fade:.4f}[brand-intro]"
            )
            segment_videos.append("brand-intro")
            segment_audio_kinds.append("intro")

        main_fades: list[str] = []
        if "intro" in indices:
            main_fades.append(f"fade=t=in:st=0:d={min(float(composition['intro']['fade']), plan.output_duration / 2):.4f}")
        if "outro" in indices:
            fade = min(float(composition["outro"]["fade"]), plan.output_duration / 2)
            main_fades.append(f"fade=t=out:st={max(0.0, plan.output_duration - fade):.6f}:d={fade:.4f}")
        if main_fades:
            filters.append(f"[{video_label}]{','.join(main_fades)}[brand-main]")
            video_label = "brand-main"
        segment_videos.append(video_label)
        segment_audio_kinds.append("main")

        if "outro" in indices:
            duration = durations["outro"]
            fade = min(max(float(composition["outro"]["fade"]), 0.0), duration / 2)
            filters.append(
                f"[{indices['outro']}:v]scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,"
                f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2,fps={plan.target_fps},"
                f"trim=duration={duration:.6f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p,"
                f"fade=t=in:st=0:d={fade:.4f}[brand-outro]"
            )
            segment_videos.append("brand-outro")
            segment_audio_kinds.append("outro")

        filters.append("".join(f"[{label}]" for label in segment_videos) + f"concat=n={len(segment_videos)}:v=1:a=0[video-structured]")
        if audio_label:
            filters.append(f"[{audio_label}]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[audio-main-normalized]")
            audio_inputs: list[str] = []
            for kind in segment_audio_kinds:
                if kind == "main":
                    audio_inputs.append("[audio-main-normalized]")
                else:
                    label = f"audio-{kind}-silence"
                    filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={durations[kind]:.6f}[{label}]")
                    audio_inputs.append(f"[{label}]")
            filters.append("".join(audio_inputs) + f"concat=n={len(audio_inputs)}:v=0:a=1[audio-structured]")
        return "video-structured", "audio-structured" if audio_label else None

    def build_command(self, source: Path, output: Path, plan: ProcessingPlan) -> list[str]:
        info = get_video_info(source)
        composition = self.parameters["composition"]
        command = [
            ffmpeg_bin(), "-hide_banner", "-y", "-threads", "0",
            "-filter_threads", "0", "-filter_complex_threads", "0", "-i", str(source),
        ]
        indices: dict[str, int] = {}
        for name in ("watermark", "pip", "intro", "outro"):
            if composition[name].get("path") and (name != "pip" or composition[name].get("enabled")):
                indices[name] = len(indices) + 1
                command += ["-i", str(composition[name]["path"])]
        filters, video_label = self._base_video_filters(plan)
        video_label = self._region_filters(video_label, plan, filters)
        video_label = self._style_overlay_filters(video_label, filters)
        video_label = self._composition_filters(video_label, plan, filters, indices)
        audio_label: str | None = None
        if info.has_audio:
            filters.append(
                f"[0:a]atrim=start={plan.start_time:.6f}:end={plan.end_time:.6f},"
                "asetpts=PTS-STARTPTS[audio-output]"
            )
            audio_label = "audio-output"
        video_label, audio_label = self._intro_outro_filters(video_label, audio_label, plan, filters, indices)
        filters.append(f"[{video_label}]null[video-output]")
        command += ["-filter_complex", ";".join(filters), "-map", "[video-output]"]
        if audio_label:
            command += ["-map", f"[{audio_label}]"]
        output_cfg = self.parameters["output"]
        if self.parameters["metadata"].get("strip_all"):
            command += ["-map_metadata", "-1", "-map_chapters", "-1"]
        _, encode_args = video_encode_args(output_cfg)
        command += encode_args
        if audio_label:
            command += [
                "-c:a", str(output_cfg["audio_codec"]), "-b:a", str(output_cfg["audio_bitrate"]),
                "-ar", str(output_cfg["audio_sample_rate"]),
            ]
        metadata = self.parameters["metadata"]
        project_signature = f"{metadata['project_name']}/{metadata['project_version']}"
        command += ["-movflags", "+faststart+use_metadata_tags", "-metadata", f"encoding_tool={project_signature}"]
        for key in ("title", "comment"):
            if metadata.get(key):
                command += ["-metadata", f"{key}={metadata[key]}"]
        command += ["-metadata:s:v:0", "handler_name=VideoHandler"]
        if audio_label:
            command += ["-metadata:s:a:0", "handler_name=SoundHandler"]
        command += ["-shortest", str(output)]
        return command

    def process(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        task_id: str | None = None,
        dry_run: bool = False,
    ) -> ProcessingPlan | list[str]:
        source, output = Path(input_path), Path(output_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        plan = self.create_plan(source)
        command = self.build_command(source, output, plan)
        if dry_run:
            return command
        output.parent.mkdir(parents=True, exist_ok=True)
        result = run_cancellable(command, task_id=task_id)
        if result.returncode:
            raise RuntimeError(f"FFmpeg 视频处理失败（{result.returncode}）：{result.stderr[-3000:]}")
        encoder_name, _ = video_encode_args(self.parameters["output"])
        output.with_suffix(output.suffix + ".processing.json").write_text(
            json.dumps({"input": str(source), "output": str(output), "encoder": encoder_name, "plan": plan.as_dict(), "parameters": self.parameters}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return plan

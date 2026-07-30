from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .audio_processor import AudioProcessor, DEFAULT_AUDIO_PARAMETERS
from .cancel import run_cancellable
from .encoding import video_encode_args
from .video_utils import ffmpeg_bin, get_video_info


ProgressCallback = Callable[[float, str], None]


DEFAULT_PARAMETERS: dict[str, Any] = {
    "profile": "balanced",
    "seed": None,
    "spatial": {
        "crop_percent": [0.02, 0.05],
        "target_resolution": "source",  # source | 720p | 1080p
        "scale_flags": "fast_bilinear",
        "background": {"enabled": False, "mode": "gaussian", "blur_sigma": 24.0, "foreground_scale": 0.90},
        "breathing_zoom": {"enabled": True, "amplitude": 0.018, "period_seconds": 6.0},
    },
    "color": {
        "enabled": True,
        "brightness": [-0.015, 0.015],
        "contrast": [1.02, 1.07],
        "saturation": [1.02, 1.08],
        "hue_degrees": [-2.0, 2.0],
        "dynamic_jitter": 0.012,
    },
    "enhance": {
        "denoise": 1.2,
        "sharpen": 0.35,
        "shadow_lift": 0.015,
        "highlight_compress": 0.025,
        "noise_strength": 1.5,
        "gradient_overlay": 0.025,
    },
    "temporal": {
        "speed": [1.01, 1.05],
        "target_fps": 30,
        "fps_mode": "resample",  # resample | interpolate
        "trim_head_seconds": 0.0,
        "trim_tail_seconds": 0.0,
    },
    "audio": {"enabled": True, **copy.deepcopy(DEFAULT_AUDIO_PARAMETERS)},
    "composition": {
        "watermark": {"path": None, "opacity": 0.65, "width_ratio": 0.16, "position": "top_right", "margin": 24},
        "style_overlay": {"enabled": False, "mode": "film", "opacity": 0.10, "grain_strength": 1.2},
        "pip": {"path": None, "width_ratio": 0.28, "position": "bottom_right", "margin": 24, "start": 0.0, "end": None},
        "intro": {"path": None, "transition": 0.5},
        "outro": {"path": None, "transition": 0.5},
        "border": {"enabled": False, "width": 2, "color": "white@0.9"},
    },
    "region": {"enabled": False, "x": 0.0, "y": 0.88, "width": 1.0, "height": 0.12, "blur_sigma": 18.0},
    "metadata": {"strip_all": True, "project_name": "VideoVariantStudio", "project_version": "1.0", "comment": "Authorized creative variant"},
    "output": {
        "video_codec": "auto", "codec_family": "h264", "video_bitrate": "4M",
        "preset": "ultrafast", "crf": 25, "audio_bitrate": "192k", "pixel_format": "yuv420p",
    },
}


PROFILE_MULTIPLIERS = {"light": 0.65, "balanced": 1.0, "strong": 1.35}


@dataclass(frozen=True)
class AugmentationPlan:
    """可记录/复现的单次增强参数。"""

    seed: int
    source_width: int
    source_height: int
    width: int
    height: int
    source_fps: float
    speed: float
    crop_x: float
    crop_y: float
    brightness: float
    contrast: float
    saturation: float
    hue_degrees: float
    volume: float
    start_time: float
    end_time: float

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class VideoAugmentor:
    """模块化视频增强流水线。

    FFmpeg 负责一次解码/一次编码完成画面、音频和叠加处理。随机参数会写入
    与输出同名的 ``.json`` 文件，方便 A/B 测试复现。调用方也可通过
    ``parameters`` 覆盖 DEFAULT_PARAMETERS 中任意嵌套字段。
    """

    def __init__(self, parameters: Mapping[str, Any] | None = None) -> None:
        self.parameters = copy.deepcopy(DEFAULT_PARAMETERS)
        if parameters:
            self._deep_update(self.parameters, parameters)
        profile = str(self.parameters.get("profile", "balanced"))
        if profile not in PROFILE_MULTIPLIERS:
            raise ValueError(f"未知强度 profile: {profile}")
        self.multiplier = PROFILE_MULTIPLIERS[profile]

    @staticmethod
    def _deep_update(target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                VideoAugmentor._deep_update(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    @staticmethod
    def _sample(rng: random.Random, value: Sequence[float] | float) -> float:
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError("范围参数必须是 [min, max]")
            return rng.uniform(float(value[0]), float(value[1]))
        return float(value)

    @staticmethod
    def _even(value: float) -> int:
        result = max(2, int(value))
        return result if result % 2 == 0 else result - 1

    def create_plan(self, input_path: str | Path) -> AugmentationPlan:
        info = get_video_info(input_path)
        if not info.width or not info.height:
            raise ValueError(f"无法读取视频尺寸: {input_path}")
        configured_seed = self.parameters.get("seed")
        seed = int(configured_seed) if configured_seed is not None else random.SystemRandom().randrange(2**32)
        rng = random.Random(seed)
        spatial, color = self.parameters["spatial"], self.parameters["color"]
        crop = self._sample(rng, spatial["crop_percent"]) * self.multiplier
        crop = min(max(crop, 0.0), 0.15)
        start_time = max(0.0, float(self.parameters["temporal"].get("trim_head_seconds", 0.0)))
        end_time = info.duration - max(0.0, float(self.parameters["temporal"].get("trim_tail_seconds", 0.0)))
        if end_time <= start_time:
            raise ValueError(f"无效剪辑区间：start={start_time:.3f}, end={end_time:.3f}")
        resolution = str(spatial.get("target_resolution", "source"))
        if resolution in {"720p", "1080p"}:
            short_edge = 720 if resolution == "720p" else 1080
            long_edge = 1280 if resolution == "720p" else 1920
            if info.width > info.height:
                output_width, output_height = long_edge, short_edge
            elif info.width < info.height:
                output_width, output_height = short_edge, long_edge
            else:
                output_width = output_height = short_edge
        else:
            output_width, output_height = info.width, info.height
        return AugmentationPlan(
            seed=seed,
            source_width=info.width,
            source_height=info.height,
            width=output_width,
            height=output_height,
            source_fps=info.fps,
            speed=self._sample(rng, self.parameters["temporal"]["speed"]),
            crop_x=crop * rng.uniform(0.9, 1.1),
            crop_y=crop * rng.uniform(0.9, 1.1),
            brightness=self._sample(rng, color["brightness"]) * self.multiplier,
            contrast=1.0 + (self._sample(rng, color["contrast"]) - 1.0) * self.multiplier,
            saturation=1.0 + (self._sample(rng, color["saturation"]) - 1.0) * self.multiplier,
            hue_degrees=self._sample(rng, color["hue_degrees"]) * self.multiplier,
            volume=self._sample(rng, self.parameters["audio"]["volume"]),
            start_time=start_time,
            end_time=end_time,
        )

    def process(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        progress: ProgressCallback | None = None,
        task_id: str | None = None,
        dry_run: bool = False,
    ) -> AugmentationPlan | list[str]:
        """执行增强；dry_run=True 时只返回完整 FFmpeg 参数。"""
        source, output = Path(input_path), Path(output_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        plan = self.create_plan(source)
        command = self.build_command(source, output, plan)
        if dry_run:
            return command
        if progress:
            progress(0.0, "开始视频增强")
        # 复用项目现有可取消子进程机制，Web 任务请求停止时会终止 FFmpeg。
        result = run_cancellable(command, task_id=task_id)
        if result.returncode:
            raise RuntimeError(f"FFmpeg 视频增强失败（{result.returncode}）：{result.stderr[-3000:]}")
        encoder_name, _ = video_encode_args(self.parameters["output"])
        manifest = output.with_suffix(output.suffix + ".augmentation.json")
        manifest.write_text(json.dumps({"input": str(source), "output": str(output), "encoder": encoder_name, "plan": plan.as_dict(), "parameters": self.parameters}, ensure_ascii=False, indent=2), encoding="utf-8")
        if progress:
            progress(1.0, "视频增强完成")
        return plan

    def build_command(self, source: Path, output: Path, plan: AugmentationPlan) -> list[str]:
        info = get_video_info(source)
        composition = self.parameters["composition"]
        command = [
            ffmpeg_bin(), "-hide_banner", "-y", "-threads", "0",
            "-filter_threads", "0", "-filter_complex_threads", "0", "-i", str(source),
        ]
        input_index: dict[str, int] = {}
        for name in ("watermark", "pip", "intro", "outro"):
            path = composition[name].get("path")
            if path:
                input_index[name] = len(input_index) + 1
                command += ["-i", str(Path(path))]
        layering = self.parameters["audio"]["layering"]
        ambient = layering.get("ambient_path")
        if ambient:
            input_index["ambient"] = len(input_index) + 1
            command += ["-stream_loop", "-1", "-i", str(Path(ambient))]
        bgm = layering.get("bgm_path")
        if bgm:
            input_index["bgm"] = len(input_index) + 1
            command += ["-stream_loop", "-1", "-i", str(Path(bgm))]

        filters, video_label = self._video_filters(plan)
        main_duration = (plan.end_time - plan.start_time) / plan.speed
        audio_label = self._audio_filters(info.has_audio, plan, input_index, filters, main_duration)
        video_label = self._region_blur_filters(video_label, plan, filters)
        video_label = self._overlay_filters(video_label, input_index, filters, plan)
        clip_durations = {
            name: get_video_info(composition[name]["path"]).duration
            for name in ("intro", "outro")
            if name in input_index
        }
        video_label, audio_label = self._intro_outro_filters(
            video_label, audio_label, input_index, filters,
            main_duration, clip_durations, plan,
        )

        command += ["-filter_complex", ";".join(filters), "-map", f"[{video_label}]"]
        if audio_label:
            command += ["-map", f"[{audio_label}]"]
        out = self.parameters["output"]
        _, encode_args = video_encode_args(out)
        command += encode_args
        if audio_label:
            command += ["-c:a", "aac", "-b:a", str(out["audio_bitrate"])]
        metadata = self.parameters["metadata"]
        if metadata.get("strip_all"):
            command += ["-map_metadata", "-1", "-map_chapters", "-1"]
        signature = f"{metadata['project_name']}/{metadata['project_version']}"
        command += ["-movflags", "+faststart+use_metadata_tags", "-metadata", f"encoding_tool={signature}"]
        if metadata.get("comment"):
            command += ["-metadata", f"comment={metadata['comment']}"]
        command += ["-shortest", str(output)]
        return command

    def _video_filters(self, plan: AugmentationPlan) -> tuple[list[str], str]:
        p, spatial, color, enhance = self.parameters, self.parameters["spatial"], self.parameters["color"], self.parameters["enhance"]
        crop_w = self._even(plan.source_width * (1.0 - plan.crop_x))
        crop_h = self._even(plan.source_height * (1.0 - plan.crop_y))
        scale_flags = str(spatial.get("scale_flags", "fast_bilinear"))
        if scale_flags not in {"fast_bilinear", "bilinear", "bicubic", "lanczos"}:
            scale_flags = "fast_bilinear"
        chain = [
            f"trim=start={plan.start_time:.6f}:end={plan.end_time:.6f}",
            "setpts=PTS-STARTPTS",
            f"crop={crop_w}:{crop_h}:(iw-{crop_w})/2:(ih-{crop_h})/2",
            f"scale={plan.width}:{plan.height}:flags={scale_flags}",
        ]
        zoom = spatial["breathing_zoom"]
        if zoom.get("enabled"):
            amp = float(zoom["amplitude"]) * self.multiplier
            period = max(float(zoom["period_seconds"]), 0.1)
            # 每帧先动态放大，再居中裁回原尺寸，形成缓慢呼吸感。
            scale_expr = f"1+{amp:.6f}*(0.5+0.5*sin(2*PI*t/{period:.4f}))"
            chain += [f"scale=w='trunc(iw*({scale_expr})/2)*2':h='trunc(ih*({scale_expr})/2)*2':eval=frame", f"crop={plan.width}:{plan.height}:(iw-{plan.width})/2:(ih-{plan.height})/2"]
        if color.get("enabled"):
            jitter = float(color["dynamic_jitter"]) * self.multiplier
            chain += [f"eq=brightness='{plan.brightness:.5f}+{jitter:.5f}*sin(2*PI*t/7)':contrast='{plan.contrast:.5f}+{jitter:.5f}*sin(2*PI*t/9)':saturation='{plan.saturation:.5f}+{jitter:.5f}*cos(2*PI*t/8)':eval=frame", f"hue=h={plan.hue_degrees:.4f}"]
        denoise = float(enhance["denoise"]) * self.multiplier
        if denoise > 0:
            chain.append(f"hqdn3d={denoise:.3f}:{denoise:.3f}:{denoise * 1.5:.3f}:{denoise * 1.5:.3f}")
        sharpen = float(enhance["sharpen"]) * self.multiplier
        if sharpen > 0:
            chain.append(f"unsharp=5:5:{sharpen:.3f}:5:5:0")
        # 曲线轻抬阴影、压低高光，避免强 HDR 风格。
        shadow, highlight = float(enhance["shadow_lift"]) * self.multiplier, float(enhance["highlight_compress"]) * self.multiplier
        if shadow or highlight:
            chain.append(f"curves=all='0/{shadow:.4f} 0.25/{0.25 + shadow:.4f} 0.75/{0.75 - highlight:.4f} 1/{1.0 - highlight:.4f}'")
        noise = float(enhance["noise_strength"]) * self.multiplier
        if noise > 0:
            chain.append(f"noise=alls={noise:.3f}:allf=t+u")
        gradient = float(enhance["gradient_overlay"]) * self.multiplier
        if gradient > 0:
            chain.append(f"geq=r='r(X,Y)*(1-{gradient:.5f}+{gradient:.5f}*X/W)':g='g(X,Y)*(1-{gradient:.5f}/2+{gradient:.5f}*Y/H/2)':b='b(X,Y)*(1-{gradient:.5f}*X/W)' ")

        bg = spatial["background"]
        filters: list[str] = []
        if bg.get("enabled"):
            fg_scale = min(max(float(bg["foreground_scale"]), 0.1), 1.0)
            sigma = float(bg["blur_sigma"])
            blur_filter = f"gblur=sigma={sigma:.3f}" if bg.get("mode") == "gaussian" else f"boxblur={max(1, int(sigma / 3))}"
            filters.append(f"[0:v]{','.join(chain)},split=2[vbg][vfg]")
            filters.append(f"[vbg]{blur_filter},scale={plan.width}:{plan.height}[bg]")
            filters.append(f"[vfg]scale={self._even(plan.width * fg_scale)}:{self._even(plan.height * fg_scale)}[fg]")
            filters.append("[bg][fg]overlay=(W-w)/2:(H-h)/2[vbase]")
        else:
            filters.append(f"[0:v]{','.join(chain)}[vbase]")
        speed = plan.speed
        filters.append(f"[vbase]setpts=PTS/{speed:.8f}[vspd]")
        fps = int(p["temporal"]["target_fps"])
        if p["temporal"].get("fps_mode") == "interpolate":
            filters.append(f"[vspd]minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,setsar=1,format=yuv420p[vfps]")
        else:
            filters.append(f"[vspd]fps={fps},setsar=1,format=yuv420p[vfps]")
        return filters, "vfps"

    def _region_blur_filters(self, current: str, plan: AugmentationPlan, filters: list[str]) -> str:
        region = self.parameters["region"]
        if not region.get("enabled"):
            return current
        values = [float(region[key]) for key in ("x", "y", "width", "height")]
        if any(value < 0 or value > 1 for value in values) or values[0] + values[2] > 1 or values[1] + values[3] > 1:
            raise ValueError("region 坐标必须位于 0-1 画面范围内")
        x, y = self._even(plan.width * values[0]), self._even(plan.height * values[1])
        width = min(self._even(plan.width * values[2]), plan.width - x)
        height = min(self._even(plan.height * values[3]), plan.height - y)
        sigma = max(0.1, float(region["blur_sigma"]))
        filters += [
            f"[{current}]split=2[augment-region-bg][augment-region-source]",
            f"[augment-region-source]crop={width}:{height}:{x}:{y},gblur=sigma={sigma:.3f}[augment-region-blur]",
            f"[augment-region-bg][augment-region-blur]overlay={x}:{y}[vregion]",
        ]
        return "vregion"

    def _audio_filters(
        self,
        has_audio: bool,
        plan: AugmentationPlan,
        indices: dict[str, int],
        filters: list[str],
        output_duration: float,
    ) -> str | None:
        audio = self.parameters["audio"]
        if not audio.get("enabled"):
            return None
        source_label = "0:a"
        if has_audio:
            filters.append(
                f"[0:a]atrim=start={plan.start_time:.6f}:end={plan.end_time:.6f},"
                "asetpts=PTS-STARTPTS[audio-trimmed]"
            )
            source_label = "audio-trimmed"
        else:
            # 无原声视频仍可使用环境音/BGM；先生成与原视频等长的静音基底。
            source_duration = plan.end_time - plan.start_time
            filters.append(f"anullsrc=r={int(audio['sample_rate'])}:cl=stereo,atrim=duration={source_duration:.6f}[audio-silent-source]")
            source_label = "audio-silent-source"
        processor = AudioProcessor(audio)
        audio_plan = processor.create_plan(seed=plan.seed, speed=plan.speed, volume=plan.volume)
        audio_filters, label = processor.build_filter_graph(
            audio_plan,
            source_label=source_label,
            duration=output_duration,
            ambient_input_index=indices.get("ambient"),
            bgm_input_index=indices.get("bgm"),
            output_label="aout",
        )
        filters.extend(audio_filters)
        return label

    @staticmethod
    def _position_expr(position: str, margin: int) -> tuple[str, str]:
        positions = {
            "top_left": (str(margin), str(margin)), "top_right": (f"W-w-{margin}", str(margin)),
            "bottom_left": (str(margin), f"H-h-{margin}"), "bottom_right": (f"W-w-{margin}", f"H-h-{margin}"),
            "center": ("(W-w)/2", "(H-h)/2"),
        }
        return positions.get(position, positions["top_right"])

    def _overlay_filters(self, video_label: str, indices: dict[str, int], filters: list[str], plan: AugmentationPlan) -> str:
        comp = self.parameters["composition"]
        current = self._style_overlay_filters(video_label, filters)
        if "watermark" in indices:
            cfg, label = comp["watermark"], "vwatermark"
            width = self._even(float(cfg["width_ratio"]) * plan.width)
            filters.append(f"[{indices['watermark']}:v]scale={width}:-2,format=rgba,colorchannelmixer=aa={float(cfg['opacity']):.4f}[wm]")
            x, y = self._position_expr(str(cfg["position"]), int(cfg["margin"]))
            filters.append(f"[{current}][wm]overlay={x}:{y}[{label}]")
            current = label
        if "pip" in indices:
            cfg, label = comp["pip"], "vpip"
            width = self._even(float(cfg["width_ratio"]) * plan.width)
            filters.append(f"[{indices['pip']}:v]scale={width}:-2,setpts=PTS-STARTPTS[pip]")
            x, y = self._position_expr(str(cfg["position"]), int(cfg["margin"]))
            end = cfg.get("end")
            enable = f":enable='between(t,{float(cfg['start']):.3f},{float(end):.3f})'" if end is not None else f":enable='gte(t,{float(cfg['start']):.3f})'"
            filters.append(f"[{current}][pip]overlay={x}:{y}:shortest=1{enable}[{label}]")
            current = label
        border = comp["border"]
        if border.get("enabled"):
            width = max(1, min(int(border["width"]), 20))
            filters.append(f"[{current}]drawbox=x=0:y=0:w=iw:h=ih:color={border['color']}:t={width}[vborder]")
            current = "vborder"
        return current

    def _style_overlay_filters(self, current: str, filters: list[str]) -> str:
        """从主视频生成全屏调色层，并按低透明度与原画面混合。"""
        style = self.parameters["composition"].get("style_overlay", {})
        opacity = max(0.0, min(float(style.get("opacity", 0.10)), 1.0))
        if not style.get("enabled") or opacity <= 0:
            return current
        mode = str(style.get("mode", "film"))
        grain = max(0.0, min(float(style.get("grain_strength", 1.2)), 5.0))
        presets = {
            "film": f"eq=contrast=1.06:saturation=0.98,curves=all='0/0 0.25/0.23 0.75/0.78 1/1',noise=alls={grain:.3f}:allf=t+u",
            "warm": "colorbalance=rs=.035:gs=.010:bs=-.025,eq=saturation=1.03",
            "cool": "colorbalance=rs=-.020:gs=.005:bs=.035,eq=saturation=1.02",
            "vignette": "vignette=PI/7:eval=frame",
        }
        preset = presets.get(mode, presets["film"])
        filters.extend([
            f"[{current}]split=2[style-original][style-source]",
            f"[style-source]{preset},format=rgba,colorchannelmixer=aa={opacity:.4f}[style-layer]",
            "[style-original][style-layer]overlay=0:0:eof_action=pass[video-styled]",
        ])
        return "video-styled"

    def _intro_outro_filters(
        self,
        video_label: str,
        audio_label: str | None,
        indices: dict[str, int],
        filters: list[str],
        main_duration: float,
        clip_durations: Mapping[str, float],
        plan: AugmentationPlan,
    ) -> tuple[str, str | None]:
        clips = [name for name in ("intro", "outro") if name in indices]
        if not clips:
            return video_label, audio_label
        # 片头/片尾先标准化；使用淡入淡出 + concat。无配音的结构片段补静音，
        # 从而确保最终音轨与视频总时长一致，不会被 -shortest 提前截断。
        segments: list[tuple[str, str | None]] = []
        for name in ("intro",):
            if name in indices:
                duration = clip_durations[name]
                transition = min(float(self.parameters["composition"][name]["transition"]), duration / 2)
                filters.append(f"[{indices[name]}:v]scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={int(self.parameters['temporal']['target_fps'])},format=yuv420p,trim=duration={duration:.6f},setpts=PTS-STARTPTS,fade=t=out:st={max(0.0, duration - transition):.6f}:d={transition:.3f}[vintro]")
                segments.append(("vintro", "intro"))
        main_fades: list[str] = []
        if "intro" in indices:
            main_fades.append(f"fade=t=in:st=0:d={float(self.parameters['composition']['intro']['transition']):.3f}")
        if "outro" in indices:
            transition = min(float(self.parameters["composition"]["outro"]["transition"]), main_duration / 2)
            main_fades.append(f"fade=t=out:st={max(0.0, main_duration - transition):.6f}:d={transition:.3f}")
        if main_fades:
            filters.append(f"[{video_label}]{','.join(main_fades)}[vmain]")
            video_label = "vmain"
        segments.append((video_label, "main" if audio_label else None))
        if "outro" in indices:
            duration = clip_durations["outro"]
            transition = min(float(self.parameters["composition"]["outro"]["transition"]), duration / 2)
            filters.append(f"[{indices['outro']}:v]scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={int(self.parameters['temporal']['target_fps'])},format=yuv420p,trim=duration={duration:.6f},setpts=PTS-STARTPTS,fade=t=in:st=0:d={transition:.3f}[voutro]")
            segments.append(("voutro", "outro"))
        concat_inputs = "".join(f"[{video}]" for video, _ in segments)
        filters.append(f"{concat_inputs}concat=n={len(segments)}:v=1:a=0[vcomposed]")
        if audio_label:
            filters.append(f"[{audio_label}]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[amain-normalized]")
            audio_segments: list[str] = []
            for _, kind in segments:
                if kind == "main":
                    audio_segments.append("[amain-normalized]")
                else:
                    duration = clip_durations[str(kind)]
                    label = f"asilence-{kind}"
                    filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f}[{label}]")
                    audio_segments.append(f"[{label}]")
            filters.append(f"{''.join(audio_segments)}concat=n={len(audio_segments)}:v=0:a=1[a-composed]")
        return "vcomposed", "a-composed" if audio_label else None

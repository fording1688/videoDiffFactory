from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cancel import run_cancellable
from .video_utils import ffmpeg_bin, get_video_info


DEFAULT_AUDIO_PARAMETERS: dict[str, Any] = {
    "seed": None,
    "sample_rate": 48000,
    "speed": [1.01, 1.05],
    "pitch_semitones": [-0.3, 0.3],
    "volume": [0.98, 1.02],
    "eq": {
        "enabled": True,
        "bands": 5,  # 3 或 5
        "highpass_hz": 70,
        "lowpass_hz": 15500,
        # (中心频率 Hz, Q 值, 增益 dB)。默认轻削浑浊区、轻抬人声存在感。
        "three_band": [(180, 0.8, -0.6), (2500, 1.0, 1.2), (8500, 0.8, 0.5)],
        "five_band": [(120, 0.8, 0.4), (300, 1.0, -0.8), (1200, 1.0, 0.3), (3200, 1.0, 1.2), (9000, 0.8, 0.5)],
    },
    "stereo": {
        "enabled": True,
        "width": 1.08,
        "haas_delay_ms": [6.0, 14.0],
    },
    "reverb": {
        "enabled": True,
        "wet": 0.035,  # 强制限制在 0-0.05
        "delays_ms": [38, 67],
        "decays": [0.22, 0.12],
    },
    "layering": {
        "pink_noise": {"enabled": False, "volume_db": -42.0},
        "ambient_path": None,
        "ambient_volume_db": -40.0,
        "bgm_path": None,
        "bgm_volume_db": -24.0,
        "bgm_fade_in": 1.5,
        "bgm_fade_out": 2.0,
        "source_duck_db": -0.7,
    },
    "output": {"codec": "aac", "bitrate": "192k"},
}


@dataclass(frozen=True)
class AudioPlan:
    seed: int
    speed: float
    pitch_semitones: float
    volume: float
    haas_delay_ms: float

    @property
    def pitch_factor(self) -> float:
        return 2.0 ** (self.pitch_semitones / 12.0)

    def as_dict(self) -> dict[str, float | int]:
        return {**self.__dict__, "pitch_factor": self.pitch_factor}


class AudioProcessor:
    """可独立运行、也可嵌入 VideoAugmentor 滤镜图的声音增强模块。"""

    def __init__(self, parameters: Mapping[str, Any] | None = None) -> None:
        self.parameters = copy.deepcopy(DEFAULT_AUDIO_PARAMETERS)
        if parameters:
            self._deep_update(self.parameters, parameters)
        bands = int(self.parameters["eq"]["bands"])
        if bands not in (3, 5):
            raise ValueError("eq.bands 仅支持 3 或 5")

    @staticmethod
    def _deep_update(target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                AudioProcessor._deep_update(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    @staticmethod
    def _sample(rng: random.Random, value: Sequence[float] | float) -> float:
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError("范围参数必须是 [min, max]")
            return rng.uniform(float(value[0]), float(value[1]))
        return float(value)

    def create_plan(
        self,
        *,
        seed: int | None = None,
        speed: float | None = None,
        volume: float | None = None,
    ) -> AudioPlan:
        configured_seed = self.parameters.get("seed") if seed is None else seed
        actual_seed = int(configured_seed) if configured_seed is not None else random.SystemRandom().randrange(2**32)
        rng = random.Random(actual_seed)
        return AudioPlan(
            seed=actual_seed,
            speed=float(speed) if speed is not None else self._sample(rng, self.parameters["speed"]),
            pitch_semitones=self._sample(rng, self.parameters["pitch_semitones"]),
            volume=float(volume) if volume is not None else self._sample(rng, self.parameters["volume"]),
            haas_delay_ms=self._sample(rng, self.parameters["stereo"]["haas_delay_ms"]),
        )

    @staticmethod
    def db_to_linear(db: float) -> float:
        return 10.0 ** (float(db) / 20.0)

    def build_filter_graph(
        self,
        plan: AudioPlan,
        *,
        source_label: str = "0:a",
        duration: float | None = None,
        ambient_input_index: int | None = None,
        bgm_input_index: int | None = None,
        output_label: str = "audio-processed",
    ) -> tuple[list[str], str]:
        """返回可直接并入 ``-filter_complex`` 的滤镜列表和输出标签。"""
        sample_rate = int(self.parameters["sample_rate"])
        pitch_factor = plan.pitch_factor
        # asetrate 改变音调和时长；后续 atempo=1/factor 抵消时长变化，最后的
        # atempo=speed 才是保持音高的目标变速。范围均落在 atempo 的 0.5-2.0 限制内。
        chain = [
            f"aresample={sample_rate}",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
            f"asetrate={sample_rate * pitch_factor:.6f}",
            f"aresample={sample_rate}",
            f"atempo={1.0 / pitch_factor:.8f}",
            f"atempo={plan.speed:.8f}",
            f"volume={plan.volume:.6f}",
        ]
        eq = self.parameters["eq"]
        if eq.get("enabled"):
            chain += [f"highpass=f={float(eq['highpass_hz']):.3f}", f"lowpass=f={float(eq['lowpass_hz']):.3f}"]
            preset = eq["three_band"] if int(eq["bands"]) == 3 else eq["five_band"]
            for frequency, q_value, gain_db in preset:
                chain.append(f"equalizer=f={float(frequency):.3f}:t=q:w={float(q_value):.3f}:g={float(gain_db):.3f}")

        filters = [f"[{source_label}]{','.join(chain)}[audio-base]"]
        current = "audio-base"
        stereo = self.parameters["stereo"]
        if stereo.get("enabled"):
            delay = max(0.0, float(plan.haas_delay_ms))
            width = min(max(float(stereo["width"]), 0.0), 2.5)
            filters += [
                f"[{current}]channelsplit=channel_layout=stereo[haas-left][haas-right]",
                "[haas-left]anull[haas-left-ready]",
                f"[haas-right]adelay={delay:.3f}[haas-right-delay]",
                f"[haas-left-ready][haas-right-delay]join=inputs=2:channel_layout=stereo,extrastereo=m={width:.4f}[audio-spatial]",
            ]
            current = "audio-spatial"

        reverb = self.parameters["reverb"]
        if reverb.get("enabled"):
            wet = min(max(float(reverb["wet"]), 0.0), 0.05)
            delays = "|".join(str(float(item)) for item in reverb["delays_ms"])
            decays = "|".join(str(float(item)) for item in reverb["decays"])
            filters += [
                f"[{current}]asplit=2[room-dry-in][room-wet-in]",
                f"[room-dry-in]volume={1.0 - wet:.6f}[room-dry]",
                f"[room-wet-in]aecho=0.8:0.7:{delays}:{decays},volume={wet:.6f}[room-wet]",
                "[room-dry][room-wet]amix=inputs=2:duration=first:normalize=0[audio-room]",
            ]
            current = "audio-room"

        layering = self.parameters["layering"]
        layer_labels: list[str] = [f"[{current}]"]
        if layering["pink_noise"].get("enabled"):
            noise_db = min(float(layering["pink_noise"]["volume_db"]), -35.0)
            amplitude = self.db_to_linear(noise_db)
            filters.append(f"anoisesrc=color=pink:amplitude={amplitude:.8f}:sample_rate={sample_rate}[pink-noise]")
            layer_labels.append("[pink-noise]")
        if ambient_input_index is not None:
            ambient_db = min(float(layering["ambient_volume_db"]), -35.0)
            filters.append(f"[{ambient_input_index}:a]aresample={sample_rate},aformat=channel_layouts=stereo,volume={self.db_to_linear(ambient_db):.8f}[ambient-bed]")
            layer_labels.append("[ambient-bed]")
        if bgm_input_index is not None:
            bgm_db = float(layering["bgm_volume_db"])
            fade_in = max(0.0, float(layering["bgm_fade_in"]))
            bgm_chain = [f"aresample={sample_rate}", "aformat=channel_layouts=stereo"]
            if duration is not None:
                fade_out = min(max(0.0, float(layering["bgm_fade_out"])), duration)
                bgm_chain += [f"atrim=duration={duration:.6f}", "asetpts=PTS-STARTPTS"]
                if fade_in:
                    bgm_chain.append(f"afade=t=in:st=0:d={min(fade_in, duration):.4f}")
                if fade_out:
                    bgm_chain.append(f"afade=t=out:st={max(0.0, duration - fade_out):.6f}:d={fade_out:.4f}")
            bgm_chain.append(f"volume={self.db_to_linear(bgm_db):.8f}")
            filters.append(f"[{bgm_input_index}:a]{','.join(bgm_chain)}[bgm-bed]")
            layer_labels.append("[bgm-bed]")

        if len(layer_labels) > 1:
            duck = self.db_to_linear(float(layering["source_duck_db"]))
            filters.append(f"[{current}]volume={duck:.8f}[audio-ducked]")
            layer_labels[0] = "[audio-ducked]"
            filters.append(f"{''.join(layer_labels)}amix=inputs={len(layer_labels)}:duration=first:dropout_transition=2:normalize=0[{output_label}]")
        else:
            filters.append(f"[{current}]anull[{output_label}]")
        return filters, output_label

    def process(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        task_id: str | None = None,
        dry_run: bool = False,
    ) -> AudioPlan | list[str]:
        """独立处理音频文件或视频中的音轨。"""
        source, output = Path(input_path), Path(output_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        info = get_video_info(source)
        if not info.has_audio:
            raise ValueError(f"输入文件没有音轨: {source}")
        plan = self.create_plan()
        duration = info.duration / plan.speed if info.duration else None
        layering = self.parameters["layering"]
        command = [ffmpeg_bin(), "-hide_banner", "-y", "-i", str(source)]
        ambient_index = bgm_index = None
        if layering.get("ambient_path"):
            ambient_index = 1
            command += ["-stream_loop", "-1", "-i", str(layering["ambient_path"])]
        if layering.get("bgm_path"):
            bgm_index = 1 + int(ambient_index is not None)
            command += ["-stream_loop", "-1", "-i", str(layering["bgm_path"])]
        filters, label = self.build_filter_graph(plan, duration=duration, ambient_input_index=ambient_index, bgm_input_index=bgm_index)
        out = self.parameters["output"]
        command += ["-filter_complex", ";".join(filters), "-map", f"[{label}]", "-vn", "-c:a", str(out["codec"]), "-b:a", str(out["bitrate"]), str(output)]
        if dry_run:
            return command
        output.parent.mkdir(parents=True, exist_ok=True)
        result = run_cancellable(command, task_id=task_id)
        if result.returncode:
            raise RuntimeError(f"FFmpeg 声音增强失败（{result.returncode}）：{result.stderr[-3000:]}")
        output.with_suffix(output.suffix + ".audio.json").write_text(
            json.dumps({"input": str(source), "output": str(output), "plan": plan.as_dict(), "parameters": self.parameters}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return plan

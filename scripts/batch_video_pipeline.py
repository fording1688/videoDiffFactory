#!/usr/bin/env python3
"""VideoVariantStudio 单次编码批量处理入口。

示例：
    python scripts/batch_video_pipeline.py ./input ./output --workers 3 --fps random
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# 允许直接执行 scripts/batch_video_pipeline.py，而不要求预先安装项目包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.video_augmentor import VideoAugmentor  # noqa: E402
from app.video_utils import get_video_info  # noqa: E402


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}
PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class BatchJob:
    source: Path
    output: Path
    parameters: dict[str, Any]


def scan_videos(input_dir: Path, recursive: bool) -> list[Path]:
    iterator = input_dir.rglob("*") if recursive else input_dir.iterdir()
    return sorted(path for path in iterator if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS)


def build_random_parameters(source: Path, args: argparse.Namespace, rng: random.Random) -> dict[str, Any]:
    """为一个输入文件创建独立参数；每个范围只在此处采样一次。"""
    info = get_video_info(source)
    head_trim = rng.uniform(0.2, 0.5)
    tail_trim = rng.uniform(0.3, 0.6)
    # 极短素材保留至少 0.2 秒内容，避免生成非法 trim 区间。
    if info.duration <= head_trim + tail_trim + 0.2:
        head_trim = tail_trim = 0.0
    seed = rng.randrange(0, 2**32)
    crop = rng.uniform(0.02, 0.05)
    speed = rng.uniform(1.015, 1.045)
    contrast = rng.uniform(0.98, 1.03)
    saturation = rng.uniform(0.98, 1.03)
    fps = rng.choice((30, 60)) if args.fps == "random" else int(args.fps)
    border_width = rng.choice((1, 2))

    composition: dict[str, Any] = {
        "watermark": {
            "path": str(args.watermark) if args.watermark else None,
            "opacity": args.watermark_opacity,
            "width_ratio": args.watermark_width,
            "position": "top_right",
            "margin": 24,
        },
        "pip": {
            "path": str(args.pip) if args.pip else None,
            "width_ratio": 0.30,
            "position": "bottom_right",
            "margin": 24,
            "start": 1.0,
            "end": None,
        },
        "border": {"enabled": args.border, "width": border_width, "color": args.border_color},
    }
    layering = {
        # 不默认加入不可见噪声；只有用户为正常声音设计明确传入环境音时才混音。
        "pink_noise": {"enabled": False, "volume_db": -42.0},
        "ambient_path": str(args.ambient) if args.ambient else None,
        "ambient_volume_db": args.ambient_db,
        "bgm_path": str(args.bgm) if args.bgm else None,
        "bgm_volume_db": args.bgm_db,
        "bgm_fade_in": 1.5,
        "bgm_fade_out": 2.0,
        "source_duck_db": -0.7,
    }
    return {
        "profile": "balanced",
        "seed": seed,
        "spatial": {"crop_percent": [crop, crop]},
        "color": {
            "brightness": [0.0, 0.0],
            "contrast": [contrast, contrast],
            "saturation": [saturation, saturation],
            "hue_degrees": [0.0, 0.0],
            "dynamic_jitter": 0.0,
        },
        "temporal": {
            "speed": [speed, speed],
            "trim_head_seconds": head_trim,
            "trim_tail_seconds": tail_trim,
            "target_fps": fps,
            "fps_mode": args.fps_mode,
        },
        "audio": {
            "pitch_semitones": [-0.3, 0.3],
            "eq": {"enabled": True, "bands": args.eq_bands},
            "stereo": {"enabled": True, "width": 1.08, "haas_delay_ms": [6.0, 14.0]},
            "reverb": {"enabled": args.reverb, "wet": 0.03},
            "layering": layering,
        },
        "region": {
            "enabled": args.blur_bottom,
            "x": 0.0,
            "y": 0.88,
            "width": 1.0,
            "height": 0.12,
            "blur_sigma": args.blur_sigma,
        },
        "composition": composition,
        "metadata": {
            "strip_all": True,
            "project_name": args.project_name,
            "project_version": args.project_version,
            "comment": "Authorized batch creative variant",
        },
        "output": {"crf": args.crf, "preset": args.preset},
    }


def run_job(job: BatchJob, *, overwrite: bool, dry_run: bool) -> dict[str, Any]:
    if job.output.exists() and not overwrite and not dry_run:
        return {"status": "skipped", "source": str(job.source), "output": str(job.output), "reason": "输出已存在"}
    try:
        augmentor = VideoAugmentor(job.parameters)
        result = augmentor.process(job.source, job.output, dry_run=dry_run)
        if dry_run:
            return {"status": "dry-run", "source": str(job.source), "output": str(job.output), "command": result}
        return {"status": "completed", "source": str(job.source), "output": str(job.output), "plan": result.as_dict()}
    except Exception as exc:  # 单文件失败不终止整个批次。
        return {"status": "failed", "source": str(job.source), "output": str(job.output), "error": str(exc)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="单次 FFmpeg 滤镜图的批量视频编辑与增强")
    parser.add_argument("input_dir", type=Path, help="输入目录")
    parser.add_argument("output_dir", type=Path, help="输出目录")
    parser.add_argument("--workers", type=int, default=3, help="并发数量，默认 3")
    parser.add_argument("--fps", choices=("30", "60", "random"), default="30")
    parser.add_argument("--fps-mode", choices=("resample", "interpolate"), default="resample")
    parser.add_argument("--eq-bands", type=int, choices=(3, 5), default=5)
    parser.add_argument("--blur-bottom", action="store_true", help="模糊底部 12%% 区域")
    parser.add_argument("--blur-sigma", type=float, default=18.0)
    parser.add_argument("--border", action="store_true", help="添加随机 1-2px 可见边框")
    parser.add_argument("--border-color", default="white@0.9")
    parser.add_argument("--watermark", type=Path, help="可见品牌水印图片/视频")
    parser.add_argument("--watermark-opacity", type=float, default=0.75)
    parser.add_argument("--watermark-width", type=float, default=0.18)
    parser.add_argument("--pip", type=Path, help="可见画中画视频")
    parser.add_argument("--ambient", type=Path, help="合法使用的环境声音轨")
    parser.add_argument("--ambient-db", type=float, default=-40.0)
    parser.add_argument("--bgm", type=Path, help="有授权的 BGM 文件")
    parser.add_argument("--bgm-db", type=float, default=-24.0)
    parser.add_argument("--reverb", action="store_true")
    parser.add_argument("--project-name", default="VideoVariantStudio")
    parser.add_argument("--project-version", default="1.0")
    parser.add_argument("--crf", type=int, default=25, help="CPU CRF / GPU质量值，限制在22-26")
    parser.add_argument("--preset", choices=("ultrafast", "superfast"), default="ultrafast")
    parser.add_argument("--seed", type=int, help="用于复现实验批次；省略则使用系统随机源")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只输出命令，不执行 FFmpeg")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input_dir.is_dir():
        print(f"输入目录不存在：{args.input_dir}", file=sys.stderr)
        return 2
    for optional_path in (args.watermark, args.pip, args.ambient, args.bgm):
        if optional_path and not optional_path.is_file():
            print(f"素材文件不存在：{optional_path}", file=sys.stderr)
            return 2
    if not 1 <= args.workers <= 32:
        print("--workers 必须在 1-32 之间", file=sys.stderr)
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sources = scan_videos(args.input_dir, args.recursive)
    if not sources:
        print("没有找到 .mp4 / .mov / .mkv 视频")
        return 0

    # 先顺序生成全部随机参数，再并发执行；即使线程调度变化，--seed 批次仍可复现。
    rng: random.Random = random.Random(args.seed) if args.seed is not None else random.SystemRandom()
    jobs: list[BatchJob] = []
    results: list[dict[str, Any]] = []
    for source in sources:
        relative = source.relative_to(args.input_dir)
        output = args.output_dir / relative.parent / f"{source.stem}_processed.mp4"
        try:
            jobs.append(BatchJob(source=source, output=output, parameters=build_random_parameters(source, args, rng)))
        except Exception as exc:
            # 探测损坏/不支持的视频也只记录当前文件，不阻断其余任务。
            results.append({"status": "failed", "source": str(source), "output": str(output), "error": str(exc)})
            print(f"[failed] {source.name}\n  {exc}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_job, job, overwrite=args.overwrite, dry_run=args.dry_run): job for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            with PRINT_LOCK:
                print(f"[{result['status']}] {Path(result['source']).name}")
                if result.get("error"):
                    print(f"  {result['error']}")
                if result.get("command"):
                    print("  " + " ".join(str(item) for item in result["command"]))

    report_path = args.output_dir / "batch_report.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = sum(result["status"] == "failed" for result in results)
    completed = sum(result["status"] == "completed" for result in results)
    print(f"批次结束：成功 {completed}，失败 {failed}，报告 {report_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import subprocess
import shutil
import mimetypes
import os
import platform
import random
import re
import threading
import traceback
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .ai_provider import AIProvider
from .advanced_settings import load_advanced_settings, save_advanced_settings
from .baidu_pan import (
    BaiduPanError,
    authorization_url as baidu_authorization_url,
    download_directory as download_baidu_directory,
    exchange_code as baidu_exchange_code,
    load_config as load_baidu_config,
    list_directory as list_baidu_directory,
    normalize_remote_dir,
    public_status as baidu_public_status,
    reserve_remote_subdir as reserve_baidu_remote_subdir,
    save_config as save_baidu_config,
    upload_file as upload_baidu_file,
)
from .cancel import CancelledTask, clear_cancel, is_cancel_requested, request_cancel
from .downloader import DownloadError, download_video_url
from .english_subtitles import slice_segments, translate_chinese_speech
from .drama_factory import options_from_tool_options, render_drama_factory
from .drama_reel_analyzer import DramaReelOptions, analyze_drama_reels, generate_reels_from_plan, render_single_reel
from .models import BatchUploadResponse, DownloadUrlRequest, TaskState, UploadResponse, VariantOptions, VariantTask
from .notifications import NotificationError, send_pushplus
from .video_utils import app_root, asset_root, check_runtime, get_video_info, safe_stem, user_data_root
from .video_augmentor import VideoAugmentor
from .visual_variant import merge_videos, render_variant, selected_video_encoder, split_video_by_random_range


APP_ROOT = app_root()
ASSET_ROOT = asset_root()
DATA_DIR = user_data_root()


def _migrate_legacy_state_files() -> None:
    """Move persistent settings out of an older source checkout on first upgrade."""
    legacy_dir = APP_ROOT / "data"
    try:
        if legacy_dir.resolve() == DATA_DIR.resolve() or not legacy_dir.is_dir():
            return
    except OSError:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("baidu_pan.json", "settings.json", "app.db", "app.sqlite", "app.sqlite3"):
        source = legacy_dir / name
        target = DATA_DIR / name
        if not source.is_file() or target.exists():
            continue
        try:
            shutil.copy2(source, target)
            source.unlink()
        except OSError:
            # Keep using the old file as a recoverable backup if migration fails.
            continue


_migrate_legacy_state_files()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
WORK_DIR = DATA_DIR / "work"
STATIC_DIR = ASSET_ROOT / "static"
for directory in (UPLOAD_DIR, OUTPUT_DIR, WORK_DIR):
    directory.mkdir(parents=True, exist_ok=True)

TASKS: dict[str, VariantTask] = {}
TASK_FUTURES: dict[str, Future] = {}
BATCH_LIMITS: dict[str, threading.BoundedSemaphore] = {}
TASK_LOCK = threading.RLock()
DEFAULT_PARALLEL_JOBS = 3


def _worker_cap() -> int:
    try:
        configured = int(os.getenv("VIDEO_VARIANT_MAX_WORKERS", "8") or "8")
    except ValueError:
        configured = 8
    return max(1, min(configured, 8))


MAX_WORKER_CAP = _worker_cap()
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKER_CAP, thread_name_prefix="variant-worker")
CLOUD_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="baidu-upload")
APP_VERSION = "0.9.0"
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
BAIDU_WATCH_STOP = threading.Event()
BAIDU_SCAN_LOCK = threading.Lock()
BAIDU_WATCH_STATE: dict[str, Any] = {"running": False, "message": "未启动", "seen": {}, "current": ""}
AUTO_SHUTDOWN_LOCK = threading.Lock()
AUTO_SHUTDOWN_SCHEDULED = False

app = FastAPI(
    title="Video Variant Studio",
    description="Local visual variant studio for content A/B testing and batch video processing.",
    version=APP_VERSION,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("shutdown")
def _shutdown_background_workers() -> None:
    """Cancel queued work when the local server/terminal is closed."""
    with TASK_LOCK:
        active_task_ids = [
            task.task_id
            for task in TASKS.values()
            if task.status in {TaskState.queued, TaskState.processing}
        ]
        futures = list(TASK_FUTURES.values())
    for task_id in active_task_ids:
        request_cancel(task_id)
    for future in futures:
        future.cancel()
    BAIDU_WATCH_STOP.set()
    EXECUTOR.shutdown(wait=False, cancel_futures=True)
    CLOUD_EXECUTOR.shutdown(wait=False, cancel_futures=True)


def _dump(task: VariantTask) -> dict[str, Any]:
    if hasattr(task, "model_dump"):
        return task.model_dump(mode="json")
    return task.dict()


def _update_timing(task: VariantTask) -> None:
    now = datetime.utcnow()
    task.updated_at = now
    if task.status == TaskState.processing and task.started_at is None:
        task.started_at = now

    start = task.started_at or task.created_at
    task.elapsed_seconds = max(0.0, (now - start).total_seconds())

    if task.status == TaskState.completed:
        task.completed_at = task.completed_at or now
        task.remaining_seconds = 0
        if task.elapsed_seconds > 0:
            task.estimated_total_seconds = task.elapsed_seconds
        return

    if task.status in {TaskState.failed, TaskState.cancelled}:
        task.completed_at = task.completed_at or now
        task.remaining_seconds = None
        return

    if task.status == TaskState.processing and task.progress > 0:
        estimated_total = task.elapsed_seconds / max(task.progress / 100, 0.01)
        # Keep the estimate stable and avoid showing unrealistic tiny numbers at startup.
        task.estimated_total_seconds = max(task.elapsed_seconds, estimated_total)
        task.remaining_seconds = max(0.0, task.estimated_total_seconds - task.elapsed_seconds)


def _set(task: VariantTask, *, status: TaskState | None = None, progress: int | None = None, message: str | None = None) -> None:
    if status is not None:
        task.status = status
    if progress is not None:
        task.progress = progress
    if message is not None:
        task.message = message
    _update_timing(task)
    with TASK_LOCK:
        TASKS[task.task_id] = task


def _sanitize_worker_count(value: int | None) -> int:
    try:
        configured = int(value or DEFAULT_PARALLEL_JOBS)
    except (TypeError, ValueError):
        configured = DEFAULT_PARALLEL_JOBS
    return max(1, min(configured, MAX_WORKER_CAP))


def _resolve_output_dir(value: str | None) -> Path:
    cleaned = (value or "").strip()
    if not cleaned:
        output_dir = OUTPUT_DIR
    else:
        output_dir = Path(cleaned).expanduser()
        if not output_dir.is_absolute():
            output_dir = OUTPUT_DIR if cleaned in {"data/outputs", "outputs"} else DATA_DIR / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise HTTPException(status_code=400, detail="输出文件夹路径无效。")
    return output_dir.resolve()


def _task_output_dir(task: VariantTask) -> Path:
    output_dir = task.tool_options.get("output_dir")
    return _resolve_output_dir(str(output_dir) if output_dir else None)


def _write_intro_file(output_dir: Path, intro_text: str | None) -> None:
    text = (intro_text or "").strip()
    if not text:
        return
    (output_dir / "介绍.txt").write_text(text + "\n", encoding="utf-8")


def _natural_path_key(path: Path) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def _scan_drama_source_root(source_root: Path) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for folder in sorted((path for path in source_root.iterdir() if path.is_dir() and not path.name.startswith(".")), key=_natural_path_key):
        try:
            folder.resolve().relative_to(source_root)
        except (OSError, ValueError):
            continue
        videos = sorted(
            (path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES),
            key=_natural_path_key,
        )
        if videos:
            groups.append({
                "name": folder.name,
                "drama_id": _drama_id_from_group_name(folder.name),
                "path": folder,
                "videos": videos,
            })
    return groups


def _drama_id_from_group_name(group_name: str) -> str:
    """Derive a stable filename-safe drama ID from a source folder name."""
    cleaned = str(group_name or "").strip()
    numeric_id = re.search(r"(?<!\d)(\d{4,})(?!\d)", cleaned)
    return safe_stem(numeric_id.group(1) if numeric_id else cleaned or "drama")


def _normalize_drama_id(value: str | None, fallback: str = "drama") -> str:
    return safe_stem(str(value or "").strip() or fallback)


def _next_drama_sequence(output_dir: Path, drama_id: str) -> int:
    """Continue after existing ``DRAMA_ID_001.mp4`` files instead of overwriting."""
    pattern = re.compile(rf"^{re.escape(drama_id)}_(\d+)\.mp4$", re.IGNORECASE)
    existing = [
        int(match.group(1))
        for path in output_dir.iterdir()
        if path.is_file() and (match := pattern.match(path.name))
    ]
    return max(existing, default=0) + 1


def _build_cross_drama_publish_batches(
    groups: list[dict[str, Any]],
    *,
    batch_size: int = 5,
) -> list[dict[str, Any]]:
    """Round-robin final videos so one publishing batch favours distinct dramas."""
    safe_batch_size = max(2, min(int(batch_size or 5), 20))
    queues = [
        {
            "drama_id": str(group["drama_id"]),
            "group_name": str(group["group_name"]),
            "files": list(group.get("files") or []),
        }
        for group in groups
        if group.get("files")
    ]
    batches: list[dict[str, Any]] = []
    cursor = 0
    while any(queue["files"] for queue in queues):
        active = [queue for queue in queues if queue["files"]]
        if not active:
            break
        offset = cursor % len(active)
        ordered = active[offset:] + active[:offset]
        files = [
            {
                "drama_id": queue["drama_id"],
                "group_name": queue["group_name"],
                "file": queue["files"].pop(0),
            }
            for queue in ordered[:safe_batch_size]
        ]
        batches.append({
            "batch": len(batches) + 1,
            "drama_count": len({item["drama_id"] for item in files}),
            "files": files,
        })
        cursor += 1
    return batches


def _maybe_write_root_publish_plan(task: VariantTask) -> Path | None:
    """Write the cross-drama plan once every group in a root batch has rendered."""
    if not bool(task.tool_options.get("root_batch")):
        return None
    with TASK_LOCK:
        siblings = [
            item
            for item in TASKS.values()
            if item.batch_id == task.batch_id and bool(item.tool_options.get("root_batch"))
        ]
        expected = int(task.tool_options.get("root_group_total") or len(siblings))
        if len(siblings) != expected or any(not item.effects.get("final_dir") for item in siblings):
            return None
        snapshot = sorted(siblings, key=lambda item: int(item.tool_options.get("root_group_position") or 0))

    output_root = Path(str(task.tool_options.get("root_output_dir") or "")).expanduser()
    if not output_root.is_dir():
        return None
    groups: list[dict[str, Any]] = []
    for item in snapshot:
        final_dir = Path(str(item.effects.get("final_dir") or ""))
        drama_id = _normalize_drama_id(
            str(item.tool_options.get("drama_id") or ""),
            str(item.tool_options.get("source_group_name") or "drama"),
        )
        files = [
            str(path.relative_to(output_root))
            for path in sorted(final_dir.glob(f"{drama_id}_*.mp4"), key=_natural_path_key)
            if path.is_file()
        ]
        groups.append({
            "drama_id": drama_id,
            "group_name": str(item.tool_options.get("source_group_name") or drama_id),
            "files": files,
        })

    batch_size = int(task.tool_options.get("publish_batch_size") or 5)
    batches = _build_cross_drama_publish_batches(groups, batch_size=batch_size)
    plan = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batch_size": batch_size,
        "strategy": "每批优先选择不同剧 ID，每个剧集最多一条",
        "group_count": len(groups),
        "video_count": sum(len(group["files"]) for group in groups),
        "batch_count": len(batches),
        "batches": batches,
    }
    json_path = output_root / "发布批次.json"
    text_path = output_root / "发布批次.txt"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    text_lines = [
        f"共 {plan['video_count']} 个视频，{plan['batch_count']} 个发布批次；每批最多 {batch_size} 个不同剧集。",
        "",
    ]
    for batch in batches:
        text_lines.append(f"第 {batch['batch']:03d} 批（{batch['drama_count']} 个剧集）")
        text_lines.extend(f"  {item['drama_id']}  {item['file']}" for item in batch["files"])
        text_lines.append("")
    text_path.write_text("\n".join(text_lines), encoding="utf-8")
    with TASK_LOCK:
        for item in snapshot:
            item.effects["publish_plan_path"] = str(json_path)
            item.effects["publish_plan_text_path"] = str(text_path)
    return json_path


def _folder_intro_text(folder: Path, fallback: str) -> str:
    intro_path = folder / "介绍.txt"
    if not intro_path.is_file():
        return fallback
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return intro_path.read_text(encoding=encoding).strip()
        except (OSError, UnicodeError):
            continue
    return fallback


def _copy_source_text_files(source_folder: Path, output_folder: Path) -> list[str]:
    copied: list[str] = []
    output_folder.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(
        (path for path in source_folder.iterdir() if path.is_file() and path.suffix.lower() == ".txt"),
        key=_natural_path_key,
    ):
        target_path = output_folder / source_path.name
        shutil.copy2(source_path, target_path)
        copied.append(str(target_path))
    return copied


def _payload_bool(payload: dict[str, Any], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _drama_options_from_payload(payload: dict[str, Any]) -> VariantOptions:
    return VariantOptions(
        intensity=str(payload.get("intensity") or "balanced") if str(payload.get("intensity") or "balanced") in {"light", "balanced", "strong"} else "balanced",
        effect_background=_payload_bool(payload, "effect_background", True),
        effect_zoom=_payload_bool(payload, "effect_zoom", True),
        effect_color=_payload_bool(payload, "effect_color", True),
        effect_texture=_payload_bool(payload, "effect_texture", True),
        effect_speed=_payload_bool(payload, "effect_speed", True),
        effect_vignette=_payload_bool(payload, "effect_vignette", True),
        effect_center_scratch=_payload_bool(payload, "effect_center_scratch", True),
        effect_light_sweep=_payload_bool(payload, "effect_light_sweep", True),
        effect_film_grain=_payload_bool(payload, "effect_film_grain", True),
        effect_hook_clip=_payload_bool(payload, "effect_hook_clip", False),
        effect_hook_caption=_payload_bool(payload, "effect_hook_caption", False),
        effect_frame_extract=_payload_bool(payload, "effect_frame_extract", True),
        effect_frame_interpolate=_payload_bool(payload, "effect_frame_interpolate", False),
        effect_md5=_payload_bool(payload, "effect_md5", True),
        effect_mirror=False,
        effect_border=_payload_bool(payload, "effect_border", True),
        effect_random_transition=_payload_bool(payload, "effect_random_transition", True),
        effect_remove_progress=_payload_bool(payload, "effect_remove_progress", True),
        hook_clip_seconds=max(1.0, min(float(payload.get("hook_clip_seconds") or 3.0), 8.0)),
        hook_texts=_parse_hook_texts(str(payload.get("hook_texts") or "")),
        hook_duration=max(1.0, min(float(payload.get("hook_duration") or 3.0), 8.0)),
    )


ADVANCED_PROFILE_KEYS = (
    "advanced_pipeline", "advanced_crop_min", "advanced_crop_max",
    "advanced_speed_min", "advanced_speed_max", "advanced_head_min",
    "advanced_head_max", "advanced_tail_min", "advanced_tail_max",
    "advanced_color_min", "advanced_color_max", "advanced_fps",
    "advanced_resolution", "advanced_interpolate", "advanced_blur_bottom",
    "advanced_blur_sigma_min", "advanced_blur_sigma_max", "advanced_border",
    "advanced_eq_bands", "advanced_reverb", "advanced_watermark_path",
    "advanced_watermark_opacity", "advanced_watermark_width",
    "advanced_style_mode", "advanced_style_opacity", "advanced_style_grain",
    "advanced_pip_path", "advanced_pip_enabled", "advanced_ambient_path",
    "advanced_ambient_db", "advanced_bgm_path", "advanced_bgm_db",
    "advanced_project_name", "advanced_project_version",
)


def _advanced_profile_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in ADVANCED_PROFILE_KEYS if key in payload}


def _variant_output_stem(task: VariantTask, version_index: int) -> str:
    batch_total = max(1, int(task.tool_options.get("batch_total") or 1))
    batch_position = max(1, int(task.tool_options.get("batch_position") or 1))
    output_number = (version_index - 1) * batch_total + batch_position
    return str(output_number)


def _submit_task(task_id: str) -> None:
    with TASK_LOCK:
        task = TASKS[task_id]
    clear_cancel(task_id)
    if bool(task.tool_options.get("root_batch")):
        position = int(task.tool_options.get("root_group_position") or 1)
        total = int(task.tool_options.get("root_group_total") or 1)
        parallelism = int(task.tool_options.get("group_parallelism") or 1)
        message = f"等待目录队列 {position}/{total}，同时处理 {parallelism} 个目录"
    else:
        message = f"等待调度，本批线程数 {task.worker_count} 个视频"
    _set(task, status=TaskState.queued, progress=0, message=message)
    future = EXECUTOR.submit(_process, task_id)
    with TASK_LOCK:
        TASK_FUTURES[task_id] = future


def _baidu_upload_paths(task: VariantTask) -> list[Path]:
    values: list[str] = []
    if task.operation == "drama_batch_reels":
        final_dir = str(task.effects.get("final_dir") or "")
        if final_dir and Path(final_dir).is_dir():
            video_suffixes = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
            values.extend(
                str(path)
                for path in sorted(Path(final_dir).iterdir())
                if path.is_file() and (path.suffix.lower() in video_suffixes or path.suffix.lower() == ".txt")
            )
    else:
        values.extend(task.variant_paths)
        if not values and task.output_path:
            values.append(task.output_path)
        if task.operation == "variant":
            intro_path = _task_output_dir(task) / "介绍.txt"
            if intro_path.is_file() and int(task.tool_options.get("batch_position") or 1) == 1:
                values.append(str(intro_path))
    seen: set[str] = set()
    paths: list[Path] = []
    for value in values:
        path = Path(value)
        key = str(path.resolve()) if path.exists() else str(path)
        if path.is_file() and key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


def _schedule_autodl_shutdown() -> None:
    """Shut down only on an AutoDL-like Linux host and only after a grace period."""
    global AUTO_SHUTDOWN_SCHEDULED
    config = load_baidu_config(DATA_DIR)
    if not config.get("shutdown_when_idle") or platform.system() != "Linux" or not Path("/root/autodl-tmp").exists():
        return
    with AUTO_SHUTDOWN_LOCK:
        if AUTO_SHUTDOWN_SCHEDULED:
            return
        AUTO_SHUTDOWN_SCHEDULED = True

    def shutdown_if_idle() -> None:
        global AUTO_SHUTDOWN_SCHEDULED
        with TASK_LOCK:
            active = any(task.status in {TaskState.queued, TaskState.processing} for task in TASKS.values())
        if active:
            threading.Timer(60, shutdown_if_idle).start()
            return
        BAIDU_WATCH_STATE["message"] = "任务已全部完成，正在关闭 AutoDL 实例"
        try:
            subprocess.run(["shutdown", "-h", "now"], check=False)
        finally:
            with AUTO_SHUTDOWN_LOCK:
                AUTO_SHUTDOWN_SCHEDULED = False

    threading.Timer(90, shutdown_if_idle).start()


def _notify_baidu_result(task: VariantTask, success: bool, remote_dir: str, file_count: int, error: str = "") -> None:
    config = load_baidu_config(DATA_DIR)
    if not config.get("notify_enabled") or not config.get("pushplus_token"):
        return
    drama_name = str(task.tool_options.get("source_group_name") or task.original_filename or task.task_id)
    elapsed = max(0, int(task.elapsed_seconds or 0))
    minutes, seconds = divmod(elapsed, 60)
    status_text = "上传完成" if success else "上传失败"
    icon = "✅" if success else "❌"
    content = "\n".join(
        [
            f"## {icon} 百度网盘{status_text}",
            f"- 剧名：{drama_name}",
            f"- 成品数量：{file_count}",
            f"- 总耗时：{minutes} 分 {seconds} 秒",
            f"- 网盘目录：`{remote_dir}`",
            *( [f"- 错误：{error}"] if error else [] ),
        ]
    )
    send_pushplus(str(config.get("pushplus_token") or ""), f"{icon} {drama_name}：{status_text}", content)


def _finish_task(task: VariantTask, message: str) -> None:
    config = load_baidu_config(DATA_DIR)
    supported = task.operation in {
        "variant",
        "drama_factory",
        "drama_batch_reels",
        "drama_reel_analyzer",
        "drama_reel_generate",
    }
    paths = _baidu_upload_paths(task) if supported else []
    if not config.get("enabled") or not baidu_public_status(config).get("authorized") or not paths:
        _set(task, status=TaskState.completed, progress=100, message=message)
        return

    remote_dir = normalize_remote_dir(str(config.get("remote_dir") or ""))
    cloud_group_id = str(task.tool_options.get("cloud_group_id") or "").strip()
    cloud_folder_name = str(task.tool_options.get("cloud_folder_name") or "").strip()
    task.effects["baidu_upload"] = {
        "status": "queued",
        "remote_dir": remote_dir,
        "file_count": len(paths),
        "uploaded_count": 0,
        "uploaded_bytes": 0,
        "total_bytes": sum(path.stat().st_size for path in paths),
        "error": "",
    }
    _set(task, status=TaskState.processing, progress=96, message=f"视频处理完成，等待上传百度网盘（{len(paths)} 个文件）")

    def worker() -> None:
        state = task.effects["baidu_upload"]
        state["status"] = "uploading"
        try:
            upload_remote_dir = remote_dir
            if cloud_group_id and cloud_folder_name:
                upload_remote_dir = reserve_baidu_remote_subdir(DATA_DIR, remote_dir, cloud_folder_name, cloud_group_id)
                state["remote_dir"] = upload_remote_dir
            completed_bytes = 0
            total_bytes = max(1, int(state["total_bytes"]))
            for index, path in enumerate(paths, start=1):
                file_size = path.stat().st_size

                def update(current: int, total: int) -> None:
                    state["uploaded_bytes"] = completed_bytes + current
                    percent = int((completed_bytes + current) / total_bytes * 100)
                    _set(task, progress=min(99, 96 + int(percent * 0.03)), message=f"正在上传百度网盘：{index}/{len(paths)} · {percent}%")

                last_error: Exception | None = None
                for attempt in range(1, 6):
                    try:
                        upload_baidu_file(DATA_DIR, path, upload_remote_dir, update)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt >= 5:
                            break
                        delay = min(30, 2 ** attempt)
                        _set(
                            task,
                            message=f"百度网盘暂时失败，{delay} 秒后重试：{index}/{len(paths)}（第 {attempt}/5 次）",
                        )
                        time.sleep(delay)
                if last_error is not None:
                    raise last_error
                completed_bytes += file_size
                state["uploaded_count"] = index
                state["uploaded_bytes"] = completed_bytes
            state["status"] = "completed"
            if config.get("cleanup_after_upload"):
                removed = 0
                for path in paths:
                    try:
                        path.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        pass
                state["local_files_removed"] = removed
            _set(task, status=TaskState.completed, progress=100, message=f"{message}；百度网盘上传完成 {len(paths)}/{len(paths)}")
            try:
                _notify_baidu_result(task, True, str(state.get("remote_dir") or remote_dir), len(paths))
                state["notification"] = "sent"
            except Exception as exc:
                state["notification"] = "failed"
                state["notification_error"] = str(exc)
            _schedule_autodl_shutdown()
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = str(exc)
            # Cloud failure never invalidates locally rendered videos.
            _set(task, status=TaskState.completed, progress=100, message=f"{message}；百度网盘上传失败，本地文件已保留")
            try:
                _notify_baidu_result(task, False, str(state.get("remote_dir") or remote_dir), len(paths), str(exc))
                state["notification"] = "sent"
            except Exception as notify_exc:
                state["notification"] = "failed"
                state["notification_error"] = str(notify_exc)

    CLOUD_EXECUTOR.submit(worker)


def _version_info() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "-C", str(APP_ROOT), "log", "-1", "--format=%h|%ci|%s"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        commit, committed_at, subject = (result.stdout.strip().split("|", 2) + ["", "", ""])[:3]
        return {
            "ok": True,
            "version": APP_VERSION,
            "build": commit or "dev",
            "committed_at": committed_at,
            "subject": subject,
        }
    except Exception:
        return {
            "ok": True,
            "version": APP_VERSION,
            "build": "packaged",
            "committed_at": "",
            "subject": "Packaged local build",
        }


def _process(task_id: str) -> None:
    with TASK_LOCK:
        task = TASKS[task_id]
        limiter = BATCH_LIMITS.get(task.batch_id)

    try:
        if task.cancel_requested or is_cancel_requested(task_id):
            _set(task, status=TaskState.cancelled, progress=task.progress, message="任务已停止")
            return

        renderer = _render_task if task.operation == "variant" else _render_tool_task
        if limiter is not None:
            if bool(task.tool_options.get("root_batch")):
                position = int(task.tool_options.get("root_group_position") or 1)
                total = int(task.tool_options.get("root_group_total") or 1)
                parallelism = int(task.tool_options.get("group_parallelism") or 1)
                wait_message = f"等待目录队列 {position}/{total}，同时处理 {parallelism} 个目录"
            else:
                wait_message = f"等待本批空闲线程，本批线程数 {task.worker_count}"
            _set(task, status=TaskState.queued, progress=0, message=wait_message)
            with limiter:
                if task.cancel_requested or is_cancel_requested(task_id):
                    _set(task, status=TaskState.cancelled, progress=task.progress, message="任务已停止")
                    return
                renderer(task)
        else:
            renderer(task)
    finally:
        _cleanup_task_input_copies(task)


def _render_task(task: VariantTask) -> None:
    try:
        runtime = check_runtime()
        if not runtime.get("ok"):
            raise RuntimeError(str(runtime.get("error") or "FFmpeg runtime missing"))
        _set(task, status=TaskState.processing, progress=8, message="正在准备视频素材")
        input_video = task.input_path
        output_dir = _task_output_dir(task)
        english_subtitles: list[dict[str, Any]] = []
        advanced_pipeline = bool(task.tool_options.get("advanced_pipeline"))
        if bool(task.tool_options.get("effect_english_subtitles")) and not advanced_pipeline:
            _set(task, progress=9, message="正在识别中文对白并翻译为英文字幕")
            subtitle_work = WORK_DIR / "english_subtitles" / task.task_id
            english_subtitles = translate_chinese_speech(
                Path(input_video), subtitle_work, task.task_id,
                str(task.tool_options.get("subtitle_model") or "base"),
            )
        variant_paths: list[str] = []
        effects_by_version: dict[str, Any] = {}
        info = None
        total = max(1, min(task.output_count, 20))
        for index in range(1, total + 1):
            if task.cancel_requested or is_cancel_requested(task.task_id):
                raise CancelledTask("任务已取消。")
            progress = 12 + int((index - 1) / total * 78)
            _set(task, progress=progress, message=f"正在生成第 {index}/{total} 个视觉版本")
            variant_task_id = f"{task.task_id}v{index:02d}"
            if advanced_pipeline:
                output_path = output_dir / f"{_variant_output_stem(task, index)}.mp4"
                parameters = _advanced_augmentor_parameters(task, variant_task_id)
                plan = VideoAugmentor(parameters).process(
                    input_video,
                    output_path,
                    task_id=task.task_id,
                )
                info = get_video_info(output_path)
                effects = {"pipeline": "advanced", "plan": plan.as_dict(), "parameters": parameters}
            else:
                output_path, effects, info = render_variant(
                    input_video=input_video,
                    output_dir=output_dir,
                    task_id=variant_task_id,
                    options=task.options,
                    output_stem=_variant_output_stem(task, index),
                    english_subtitles=english_subtitles,
                    cancel_task_id=task.task_id,
                )
            variant_paths.append(str(output_path))
            effects_by_version[f"version_{index:02d}"] = effects

        task.variant_paths = variant_paths
        task.variant_download_urls = []
        output_path = Path(variant_paths[0])
        task.video_info = info
        task.effects = effects_by_version
        task.effects["english_subtitle_count"] = len(english_subtitles)
        task.output_path = str(output_path)
        task.download_url = None
        _finish_task(task, f"处理完成，已生成 {total} 个独立版本")
    except CancelledTask:
        task.cancel_requested = True
        _set(task, status=TaskState.cancelled, progress=task.progress, message="任务已停止")
    except Exception as exc:
        task.error = str(exc)
        task.effects["traceback"] = traceback.format_exc(limit=6)
        _set(task, status=TaskState.failed, progress=100, message="处理失败")


def _advanced_augmentor_parameters(
    task: VariantTask,
    variant_task_id: str,
    input_video: str | Path | None = None,
) -> dict[str, Any]:
    """把网页配置转换为单次 FFmpeg 高级流水线参数。"""
    options = task.tool_options
    rng = random.Random(variant_task_id)
    crop_min = max(0.0, min(float(options.get("advanced_crop_min") or 0.02), 0.15))
    crop_max = max(crop_min, min(float(options.get("advanced_crop_max") or 0.05), 0.15))
    speed_min = max(0.5, min(float(options.get("advanced_speed_min") or 1.015), 2.0))
    speed_max = max(speed_min, min(float(options.get("advanced_speed_max") or 1.045), 2.0))
    head_min = max(0.0, float(options.get("advanced_head_min") or 0.2))
    head_max = max(head_min, float(options.get("advanced_head_max") or 0.5))
    tail_min = max(0.0, float(options.get("advanced_tail_min") or 0.3))
    tail_max = max(tail_min, float(options.get("advanced_tail_max") or 0.6))
    head_trim = rng.uniform(head_min, head_max)
    tail_trim = rng.uniform(tail_min, tail_max)
    source_duration = get_video_info(input_video or task.input_path).duration
    if source_duration <= head_trim + tail_trim + 0.2:
        head_trim = tail_trim = 0.0
    saturation_min = max(0.5, min(float(options.get("advanced_color_min") or 0.98), 2.0))
    saturation_max = max(saturation_min, min(float(options.get("advanced_color_max") or 1.03), 2.0))
    layering = {
        "pink_noise": {"enabled": False, "volume_db": -42.0},
        "ambient_path": str(options.get("advanced_ambient_path") or "").strip() or None,
        "ambient_volume_db": min(float(options.get("advanced_ambient_db") or -40.0), -35.0),
        "bgm_path": str(options.get("advanced_bgm_path") or "").strip() or None,
        "bgm_volume_db": float(options.get("advanced_bgm_db") or -24.0),
        "bgm_fade_in": 1.5,
        "bgm_fade_out": 2.0,
        "source_duck_db": -0.7,
    }
    return {
        "profile": "balanced",
        "seed": rng.randrange(0, 2**32),
        "spatial": {
            "crop_percent": [crop_min, crop_max],
            "target_resolution": (
                str(options.get("advanced_resolution") or "720p")
                if str(options.get("advanced_resolution") or "720p") in {"source", "720p", "1080p"}
                else "720p"
            ),
            "scale_flags": "fast_bilinear",
        },
        "color": {
            "brightness": [-0.01, 0.01],
            "contrast": [saturation_min, saturation_max],
            "saturation": [saturation_min, saturation_max],
            "hue_degrees": [-1.0, 1.0],
            "dynamic_jitter": 0.005,
        },
        "temporal": {
            "speed": [speed_min, speed_max],
            "trim_head_seconds": head_trim,
            "trim_tail_seconds": tail_trim,
            "target_fps": (
                rng.choice((30, 60))
                if int(options.get("advanced_fps") or 0) == 0
                else (60 if int(options.get("advanced_fps") or 30) == 60 else 30)
            ),
            "fps_mode": "interpolate" if bool(options.get("advanced_interpolate")) else "resample",
        },
        "audio": {
            "pitch_semitones": [-0.3, 0.3],
            "eq": {
                "enabled": True,
                "bands": (
                    rng.choice((3, 5))
                    if int(options.get("advanced_eq_bands") or 0) == 0
                    else (3 if int(options.get("advanced_eq_bands") or 5) == 3 else 5)
                ),
            },
            "stereo": {"enabled": True, "width": 1.08, "haas_delay_ms": [6.0, 14.0]},
            "reverb": {"enabled": bool(options.get("advanced_reverb")), "wet": 0.03},
            "layering": layering,
        },
        "region": {
            "enabled": bool(options.get("advanced_blur_bottom")),
            "x": 0.0, "y": 0.88, "width": 1.0, "height": 0.12,
            "blur_sigma": rng.uniform(
                max(0.1, float(options.get("advanced_blur_sigma_min") or 14.0)),
                max(
                    max(0.1, float(options.get("advanced_blur_sigma_min") or 14.0)),
                    float(options.get("advanced_blur_sigma_max") or 22.0),
                ),
            ),
        },
        "composition": {
            "watermark": {
                "path": str(options.get("advanced_watermark_path") or "").strip() or None,
                "opacity": max(0.15, min(float(options.get("advanced_watermark_opacity") or 0.22), 1.0)),
                "width_ratio": max(0.05, min(float(options.get("advanced_watermark_width") or 0.12), 0.4)),
                "position": "top_right", "margin": 24,
            },
            "style_overlay": {
                "enabled": True,
                "mode": (
                    str(options.get("advanced_style_mode") or "film")
                    if str(options.get("advanced_style_mode") or "film") in {"film", "warm", "cool", "vignette"}
                    else "film"
                ),
                "opacity": max(0.05, min(float(options.get("advanced_style_opacity") or 0.10), 0.15)),
                "grain_strength": max(0.0, min(float(options.get("advanced_style_grain") or 1.2), 5.0)),
            },
            "pip": {
                "path": (
                    str(options.get("advanced_pip_path") or "").strip() or None
                    if bool(options.get("advanced_pip_enabled"))
                    else None
                ),
                "width_ratio": 0.30, "position": "bottom_right", "margin": 24,
                "start": 1.0, "end": None,
            },
            "border": {
                "enabled": bool(options.get("advanced_border")),
                "width": rng.choice((1, 2)), "color": "white@0.9",
            },
        },
        "metadata": {
            "strip_all": True,
            "project_name": str(options.get("advanced_project_name") or "VideoVariantStudio"),
            "project_version": str(options.get("advanced_project_version") or APP_VERSION),
            "comment": "Authorized web creative variant",
        },
        "output": {
            "video_codec": "auto", "codec_family": "h264", "video_bitrate": "4M",
            "preset": "ultrafast", "crf": 25, "audio_bitrate": "192k", "pixel_format": "yuv420p",
        },
    }


def _zip_outputs(zip_path: Path, paths: list[Path]) -> Path:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in paths:
            if file_path.exists():
                archive.write(file_path, file_path.name)
    return zip_path


def _render_tool_task(task: VariantTask) -> None:
    try:
        runtime = check_runtime()
        if not runtime.get("ok"):
            raise RuntimeError(str(runtime.get("error") or "FFmpeg runtime missing"))
        output_dir = _task_output_dir(task)

        if task.operation == "merge":
            _set(task, status=TaskState.processing, progress=15, message="正在按选择顺序合并视频")
            output_path = merge_videos(
                input_paths=task.source_paths,
                work_dir=WORK_DIR / "merge",
                task_id=task.task_id,
                output_path=output_dir / f"合并视频_{task.task_id}.mp4",
            )
            task.output_path = str(output_path)
            task.download_url = f"/api/download/{task.task_id}"
            task.video_info = get_video_info(output_path)
            task.effects = {"operation": "merge", "source_count": len(task.source_paths)}
            _set(task, status=TaskState.completed, progress=100, message=f"合并完成，共 {len(task.source_paths)} 个视频")
            return

        if task.operation == "download":
            url = str(task.tool_options.get("url") or "")
            cookies_browser = task.tool_options.get("cookies_browser") or None
            proxy = task.tool_options.get("proxy") or None
            allow_playlist = bool(task.tool_options.get("allow_playlist"))
            max_downloads = task.tool_options.get("max_downloads")

            def update_download_progress(data: dict[str, Any]) -> None:
                if data.get("status") == "finished":
                    _set(task, progress=92, message="视频已下载，正在整理文件")
                    return
                downloaded = int(data.get("downloaded_bytes") or 0)
                total = int(data.get("total_bytes") or 0)
                if total > 0:
                    progress = 10 + int(min(downloaded / total, 1.0) * 80)
                else:
                    progress = max(10, min(task.progress + 1, 88))
                speed = float(data.get("speed") or 0)
                eta = data.get("eta")
                detail = f"{downloaded / 1024 / 1024:.1f} MB"
                if speed > 0:
                    detail += f" · {speed / 1024 / 1024:.1f} MB/s"
                if eta is not None:
                    detail += f" · 剩余约 {int(eta)} 秒"
                _set(task, progress=progress, message=f"正在下载视频：{detail}")

            _set(task, status=TaskState.processing, progress=10, message="正在解析分享链接")
            output_paths, info = download_video_url(
                url=url,
                output_dir=UPLOAD_DIR,
                task_id=task.task_id,
                cookies_browser=str(cookies_browser) if cookies_browser else None,
                proxy=str(proxy) if proxy else None,
                allow_playlist=allow_playlist,
                max_downloads=int(max_downloads) if max_downloads else None,
                progress_callback=update_download_progress,
            )
            output_path = output_paths[0]
            task.original_filename = output_path.name
            task.input_path = str(output_path)
            task.output_path = str(output_path)
            task.source_paths = [str(path) for path in output_paths]
            task.source_filenames = [path.name for path in output_paths]
            if len(output_paths) == 1:
                task.download_url = f"/api/download/{task.task_id}"
            else:
                task.variant_paths = [str(path) for path in output_paths]
                task.variant_download_urls = [
                    f"/api/download/{task.task_id}/variants/{index}" for index in range(1, len(output_paths) + 1)
                ]
            task.effects = {
                "operation": "download",
                "source_url": url,
                "extractor": info.get("extractor_key") or info.get("extractor") or "",
                "title": info.get("title") or "",
                "duration": _safe_float(info.get("duration")),
                "download_count": len(output_paths),
                "allow_playlist": allow_playlist,
                "webpage_url": info.get("webpage_url") or url,
            }
            try:
                task.video_info = get_video_info(output_path)
            except Exception:
                task.video_info = None
            _set(task, status=TaskState.completed, progress=100, message=f"链接视频下载完成，共 {len(output_paths)} 个文件")
            return

        if task.operation == "drama_factory":
            options = options_from_tool_options(task.tool_options)
            _set(task, status=TaskState.processing, progress=12, message="Detecting high-emotion drama clips")
            paths, metadata_path, metadata = render_drama_factory(
                input_video=task.input_path,
                output_dir=output_dir,
                task_id=task.task_id,
                options=options,
            )
            task.variant_paths = [str(path) for path in paths]
            task.variant_download_urls = []
            task.output_path = str(paths[0]) if paths else None
            task.video_info = get_video_info(task.input_path)
            task.effects = {
                "operation": "drama_factory",
                "metadata_path": str(metadata_path),
                "clip_count": len(metadata.get("clips", [])),
                "output_count": len(paths),
                "transcript_source": metadata.get("transcript_source"),
            }
            task.package_path = None
            task.package_url = None
            _finish_task(task, f"Short drama factory completed: {len(paths)} videos")
            return

        if task.operation == "drama_batch_reels":
            _set(task, status=TaskState.processing, progress=5, message="正在准备整集批量 Reel")
            paths, manifest_path, metadata = _render_drama_batch_reels(task=task, output_dir=output_dir)
            task.variant_paths = [str(path) for path in paths]
            task.variant_download_urls = []
            task.output_count = len(paths)
            task.output_path = str(paths[0]) if paths else str(manifest_path)
            task.effects = metadata
            _maybe_write_root_publish_plan(task)
            _finish_task(task, f"整集 Reel 批处理完成，共 {len(paths)} 个视频")
            return

        if task.operation == "drama_reel_analyzer":
            _set(task, status=TaskState.processing, progress=8, message="正在分析剧集字幕和爆点")
            options = drama_reel_options_from_tool(task.tool_options)
            process_variants = bool(task.tool_options.get("process_variants"))
            analysis_output_dir = WORK_DIR / "drama_reel_analyzer" if process_variants else output_dir
            generated, plan_path, metadata = analyze_drama_reels(
                episode_paths=[Path(path) for path in task.source_paths],
                output_dir=analysis_output_dir,
                task_id=task.task_id,
                options=options,
            )
            if process_variants:
                _set(task, progress=45, message="正在把拆条结果交给处理视频生成多版本")
                generated, variant_metadata = _render_drama_reel_variants(
                    plan_path=plan_path,
                    output_dir=output_dir,
                    task_id=task.task_id,
                    options=options,
                    clips_per_episode=int(task.tool_options.get("clips_per_episode") or 3),
                    versions_per_clip=int(task.tool_options.get("versions_per_clip") or 5),
                )
                metadata.update(variant_metadata)
            promo_target_dir = Path(
                str(
                    metadata.get("variant_pipeline", {}).get("final_dir")
                    or metadata.get("reports_dir")
                    or output_dir
                )
            )
            promo_path = _write_promo_copy(
                promo_target_dir,
                str(task.tool_options.get("intro_text") or ""),
                str(task.tool_options.get("promo_link") or ""),
                str(task.tool_options.get("ai_model") or ""),
            )
            if promo_path:
                metadata["promo_copy_path"] = str(promo_path)
            task.variant_paths = [str(path) for path in generated]
            task.variant_download_urls = []
            task.output_path = str(generated[0]) if generated else str(plan_path)
            task.effects = metadata
            task.package_path = None
            task.package_url = None
            _finish_task(task, f"Drama Reel Analyzer 完成：{metadata.get('top20_count', 0)} 条 Top Reel")
            return

        if task.operation == "drama_reel_generate":
            _set(task, status=TaskState.processing, progress=15, message="正在根据 Reel 方案生成视频")
            options = drama_reel_options_from_tool(task.tool_options)
            plan_path = Path(str(task.tool_options.get("plan_path") or ""))
            selected_ids = list(task.tool_options.get("selected_ids") or [])
            generated = generate_reels_from_plan(
                plan_path=plan_path,
                selected_ids=[str(item) for item in selected_ids],
                output_dir=output_dir,
                options=options,
                task_id=task.task_id,
            )
            task.variant_paths = [str(path) for path in generated]
            task.variant_download_urls = []
            task.output_path = str(generated[0]) if generated else None
            task.effects = {"operation": "drama_reel_generate", "generated_count": len(generated), "plan_path": str(plan_path)}
            task.package_path = None
            task.package_url = None
            _finish_task(task, f"Reel 视频生成完成：{len(generated)} 个")
            return

        if task.operation == "split":
            min_seconds = float(task.tool_options.get("min_seconds", 50))
            max_seconds = float(task.tool_options.get("max_seconds", 56))
            _set(task, status=TaskState.processing, progress=15, message=f"正在按 {min_seconds:g}-{max_seconds:g} 秒随机切分视频")
            parts = split_video_by_random_range(
                input_video=task.input_path,
                output_dir=output_dir,
                task_id=task.task_id,
                min_seconds=min_seconds,
                max_seconds=max_seconds,
                output_stem=safe_stem(task.original_filename),
            )
            paths = [Path(item["path"]) for item in parts]
            task.variant_paths = [str(path) for path in paths]
            task.variant_download_urls = [f"/api/download/{task.task_id}/variants/{index}" for index in range(1, len(paths) + 1)]
            task.output_path = str(paths[0]) if paths else None
            task.video_info = get_video_info(task.input_path)
            task.effects = {"operation": "split", "segments": parts, "range": [min_seconds, max_seconds]}
            package_path = output_dir / f"{task.task_id}_split_parts.zip"
            _zip_outputs(package_path, paths)
            task.package_path = str(package_path)
            task.package_url = f"/api/download/{task.task_id}/package"
            _set(task, status=TaskState.completed, progress=100, message=f"切分完成，共 {len(paths)} 个片段")
            return

        raise RuntimeError(f"未知任务类型：{task.operation}")
    except CancelledTask:
        task.cancel_requested = True
        _set(task, status=TaskState.cancelled, progress=task.progress, message="任务已停止")
    except Exception as exc:
        task.error = str(exc)
        task.effects["traceback"] = traceback.format_exc(limit=6)
        _set(task, status=TaskState.failed, progress=100, message="处理失败")


def _validate_video_upload(file: UploadFile) -> str:
    if not file.filename:
        raise HTTPException(status_code=400, detail="请选择视频文件。")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".webm"}:
        raise HTTPException(status_code=400, detail=f"{file.filename} 格式不支持。")
    return suffix


def _parse_split_range(value: str) -> tuple[float, float]:
    cleaned = (value or "").strip().replace("，", "-").replace(",", "-").replace("~", "-")
    parts = [part.strip() for part in cleaned.split("-") if part.strip()]
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="切分时间请输入类似 50-56 的格式。")
    try:
        min_seconds = float(parts[0])
        max_seconds = float(parts[1])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="切分时间必须是数字，例如 50-56。") from exc
    if min_seconds <= 0 or max_seconds <= 0 or min_seconds > max_seconds:
        raise HTTPException(status_code=400, detail="切分时间范围无效，最小值必须大于 0 且不能超过最大值。")
    return min_seconds, max_seconds


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _media_type(path: Path, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or fallback


def _has_active_tasks() -> bool:
    with TASK_LOCK:
        return any(task.status in {TaskState.queued, TaskState.processing} for task in TASKS.values())


def _clear_directory(directory: Path) -> dict[str, int]:
    deleted_files = 0
    deleted_dirs = 0
    deleted_bytes = 0
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.name == ".gitkeep":
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                for child in path.rglob("*"):
                    if child.is_file():
                        deleted_files += 1
                        deleted_bytes += child.stat().st_size
                shutil.rmtree(path)
                deleted_dirs += 1
            else:
                if path.is_file():
                    deleted_files += 1
                    deleted_bytes += path.stat().st_size
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
    return {"files": deleted_files, "dirs": deleted_dirs, "bytes": deleted_bytes}


def _reset_finished_tasks() -> None:
    with TASK_LOCK:
        TASKS.clear()
        TASK_FUTURES.clear()
        BATCH_LIMITS.clear()


def _cleanup_task_input_copies(task: VariantTask) -> None:
    """Remove browser-uploaded source copies after outputs are finalized."""
    if task.operation not in {"variant", "merge", "split", "drama_factory", "drama_batch_reels", "drama_reel_analyzer", "drama_reel_generate"}:
        return
    upload_root = UPLOAD_DIR.resolve()
    protected_values = [task.output_path, task.package_path, *task.variant_paths]
    protected: set[Path] = set()
    for value in protected_values:
        if value:
            try:
                protected.add(Path(value).resolve())
            except OSError:
                continue
    candidates = [task.input_path, *task.source_paths]
    for value in dict.fromkeys(item for item in candidates if item):
        path = Path(value)
        try:
            resolved = path.resolve()
            resolved.relative_to(upload_root)
        except (OSError, ValueError):
            continue
        if resolved in protected:
            continue
        try:
            if resolved.is_file() or resolved.is_symlink():
                resolved.unlink(missing_ok=True)
            elif resolved.is_dir():
                shutil.rmtree(resolved, ignore_errors=True)
        except OSError:
            continue
        try:
            if resolved.parent != upload_root:
                resolved.parent.rmdir()
        except OSError:
            pass
    if task.operation == "variant":
        shutil.rmtree(WORK_DIR / "english_subtitles" / task.task_id, ignore_errors=True)
    elif task.operation == "merge":
        shutil.rmtree(WORK_DIR / "merge" / f"{task.task_id}_merge_work", ignore_errors=True)


def _parse_hook_texts(value: str | None) -> list[str]:
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [line.strip()[:80] for line in lines if line.strip()][:50]


def drama_reel_options_from_tool(values: dict[str, Any]) -> DramaReelOptions:
    def number(name: str, default: float) -> float:
        try:
            return float(values.get(name, default))
        except (TypeError, ValueError):
            return default

    def integer(name: str, default: int) -> int:
        return int(max(1, number(name, default)))

    frame_mode = str(values.get("frame_mode") or "blur").strip().lower()
    if frame_mode not in {"blur", "crop"}:
        frame_mode = "blur"
    subtitle_mode = str(values.get("subtitle_mode") or "english").strip().lower()
    return DramaReelOptions(
        max_episodes=min(50, integer("max_episodes", 10)),
        max_reels=min(50, integer("max_reels", 20)),
        target_seconds=max(10.0, min(number("target_seconds", 30), 90.0)),
        min_seconds=max(3.0, min(number("min_seconds", 18), 90.0)),
        max_seconds=max(5.0, min(number("max_seconds", 55), 120.0)),
        frame_mode=frame_mode,
        burn_subtitles=bool(values.get("burn_subtitles", True)),
        subtitle_mode=subtitle_mode,
        generate_videos=bool(values.get("generate_videos", False)),
        ai_model=str(values.get("ai_model") or "").strip(),
    )


def _select_reels_for_variant_pipeline(plan: dict[str, Any], clips_per_episode: int) -> list[dict[str, Any]]:
    by_episode: dict[int, list[dict[str, Any]]] = {}
    for reel in plan.get("single_reels") or []:
        try:
            episode = int(reel.get("episode") or 1)
        except (TypeError, ValueError):
            episode = 1
        by_episode.setdefault(episode, []).append(reel)

    selected: list[dict[str, Any]] = []
    limit = max(1, min(int(clips_per_episode or 3), 20))
    for episode in sorted(by_episode):
        reels = sorted(by_episode[episode], key=lambda item: float(item.get("overall_score") or 0), reverse=True)
        for local_index, reel in enumerate(reels[:limit], start=1):
            selected.append({**reel, "_episode_order": episode, "_clip_order": local_index})
    return selected


def _variant_options_for_drama_reels() -> VariantOptions:
    options = VariantOptions()
    options.effect_hook_clip = False
    options.effect_hook_caption = False
    profile = os.getenv("DRAMA_REEL_VARIANT_PROFILE", "strong").strip().lower()
    if profile in {"fast", "speed", "极速", "高速"}:
        options.intensity = "light"
        options.effect_background = False
        options.effect_texture = False
        options.effect_vignette = False
        options.effect_center_scratch = False
        options.effect_light_sweep = False
        options.effect_film_grain = False
        options.effect_frame_extract = False
        options.effect_random_transition = False
    elif profile in {"strong", "dedupe", "quality", "强去重"}:
        options.intensity = "balanced"
        options.effect_background = True
        options.effect_texture = True
        options.effect_vignette = True
        options.effect_center_scratch = True
        options.effect_light_sweep = True
        options.effect_film_grain = True
        options.effect_frame_extract = True
        options.effect_random_transition = True
    else:
        options.intensity = "light"
        options.effect_background = False
        options.effect_texture = True
        options.effect_vignette = True
        options.effect_center_scratch = True
        options.effect_light_sweep = True
        options.effect_film_grain = False
        options.effect_frame_extract = True
        options.effect_random_transition = False
    return options


def _render_drama_batch_reels(
    *,
    task: VariantTask,
    output_dir: Path,
) -> tuple[list[Path], Path, dict[str, Any]]:
    version_count = max(1, min(int(task.tool_options.get("versions_per_episode") or 1), 10))
    worker_count = max(1, min(int(task.worker_count or 3), MAX_WORKER_CAP))
    min_seconds = max(5.0, float(task.tool_options.get("min_seconds") or 28))
    max_seconds = max(min_seconds, float(task.tool_options.get("max_seconds") or 30))
    variant_options = task.options
    work_root = WORK_DIR / "drama_batch_reels" / task.task_id
    batch_root = output_dir if bool(task.tool_options.get("direct_output")) else output_dir / f"reels_{task.task_id}"
    merged_dir = work_root / "merged"
    split_dir = work_root / "split"
    final_dir = batch_root
    work_root.mkdir(parents=True, exist_ok=True)
    for directory in (merged_dir, split_dir, final_dir):
        directory.mkdir(parents=True, exist_ok=True)
    drama_id = _normalize_drama_id(
        str(task.tool_options.get("drama_id") or ""),
        str(task.tool_options.get("source_group_name") or Path(final_dir).name or "drama"),
    )
    first_output_sequence = _next_drama_sequence(final_dir, drama_id)
    _write_intro_file(final_dir, str(task.tool_options.get("intro_text") or ""))
    source_text_dir = str(task.tool_options.get("source_text_dir") or "").strip()
    copied_text_files = _copy_source_text_files(Path(source_text_dir), final_dir) if source_text_dir else []
    try:
        if task.cancel_requested or is_cancel_requested(task.task_id):
            raise CancelledTask("任务已取消。")
        _set(task, progress=8, message=f"正在按选择顺序合并 {len(task.source_paths)} 集视频")
        merged_path = merge_videos(
            input_paths=task.source_paths,
            work_dir=work_root,
            task_id=f"{task.task_id}_pipeline",
            output_path=merged_dir / "合并视频.mp4",
        )
        english_subtitles: list[dict[str, Any]] = []
        if bool(task.tool_options.get("effect_english_subtitles")):
            _set(task, progress=16, message="正在识别整组中文对白并翻译为英文字幕")
            english_subtitles = translate_chinese_speech(
                merged_path, work_root, task.task_id,
                str(task.tool_options.get("subtitle_model") or "base"),
            )

        _set(task, progress=22, message=f"正在把合并视频按 {min_seconds:g}-{max_seconds:g} 秒完整切分")
        base_parts = split_video_by_random_range(
            input_video=merged_path,
            output_dir=work_root,
            task_id=f"{task.task_id}_base",
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            output_stem="base",
        )
        for index, part in enumerate(base_parts, start=1):
            source_path = Path(str(part["path"]))
            saved_part_path = split_dir / f"{index:03d}.mp4"
            shutil.move(str(source_path), str(saved_part_path))
            part["path"] = str(saved_part_path)
        segment_count = len(base_parts)
        jobs = [
            (
                (version_index - 1) * segment_count + part_index,
                part_index,
                version_index,
                Path(str(part["path"])),
                part,
            )
            for version_index in range(1, version_count + 1)
            for part_index, part in enumerate(base_parts, start=1)
        ]
        total_jobs = max(1, len(jobs))

        def render_job(job: tuple[int, int, int, Path, dict[str, Any]]) -> dict[str, Any]:
            output_number, part_index, version_index, base_path, part = job
            if task.cancel_requested or is_cancel_requested(task.task_id):
                raise CancelledTask("任务已取消。")
            variant_id = f"{task.task_id}_p{part_index:04d}_v{version_index:02d}"
            output_sequence = first_output_sequence + output_number - 1
            if bool(task.tool_options.get("advanced_pipeline")):
                output_path = final_dir / f"{drama_id}_{output_sequence:03d}.mp4"
                parameters = _advanced_augmentor_parameters(task, variant_id, base_path)
                plan = VideoAugmentor(parameters).process(
                    base_path,
                    output_path,
                    task_id=task.task_id,
                )
                effects = {"pipeline": "advanced", "plan": plan.as_dict(), "parameters": parameters}
            else:
                output_path, effects, _ = render_variant(
                    input_video=base_path,
                    output_dir=final_dir,
                    task_id=variant_id,
                    options=variant_options,
                    output_stem=f"{drama_id}_{output_sequence:03d}",
                    english_subtitles=slice_segments(
                        english_subtitles,
                        float(part.get("start") or 0),
                        float(part.get("duration") or 0),
                    ),
                    cancel_task_id=task.task_id,
                )
            return {
                "output_number": output_number,
                "output_sequence": output_sequence,
                "drama_id": drama_id,
                "part": part_index,
                "version": version_index,
                "start": part["start"],
                "source_duration": part["duration"],
                "filename": output_path.name,
                "path": str(output_path),
                "effects": effects,
            }

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="drama-pipeline") as pool:
            future_map = {pool.submit(render_job, job): job for job in jobs}
            for completed, future in enumerate(as_completed(future_map), start=1):
                results.append(future.result())
                progress = 28 + int(completed / total_jobs * 64)
                _set(task, progress=progress, message=f"{worker_count} 线程处理中：已完成 {completed}/{total_jobs} 个最终视频")

        results.sort(key=lambda item: int(item["output_number"]))
        outputs = [Path(str(item["path"])) for item in results]
        manifest_path = work_root / "reels_manifest.json"
        metadata = {
            "operation": "drama_batch_reels",
            "source_count": len(task.source_paths),
            "segment_count": len(base_parts),
            "versions_per_segment": version_count,
            "worker_count": worker_count,
            "segment_range": [min_seconds, max_seconds],
            "output_count": len(outputs),
            "drama_id": drama_id,
            "first_output_sequence": first_output_sequence,
            "last_output_sequence": first_output_sequence + len(outputs) - 1,
            "english_subtitle_count": len(english_subtitles),
            "batch_root": str(batch_root),
            "final_dir": str(final_dir),
            "intermediate_files_retained": False,
            "copied_text_files": copied_text_files,
            "outputs": results,
        }
        manifest_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return outputs, manifest_path, metadata
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def _drama_variant_worker_count(total_outputs: int) -> int:
    configured = os.getenv("DRAMA_REEL_VARIANT_WORKERS", "").strip()
    if configured:
        try:
            return max(1, min(int(configured), total_outputs, MAX_WORKER_CAP))
        except ValueError:
            pass
    if total_outputs <= 1:
        return 1
    profile = os.getenv("DRAMA_REEL_VARIANT_PROFILE", "strong").strip().lower()
    default_workers = 8 if profile in {"fast", "speed", "极速", "高速"} else 6
    return max(1, min(default_workers, total_outputs, MAX_WORKER_CAP))


PROMO_COPY_PROMPT = """
你是 Facebook Reel 短剧投流文案专家。目标是把用户提供的剧集介绍和推广链接，改写成更吸引点击、评论、追剧的发布文案。

要求：
- 不要虚构剧中不存在的具体情节。
- 只生成一份推广介绍文案，不要多版本。
- 文案要短、强钩子、适合 Facebook Reel。
- 避免夸张违规承诺，不要写“保证爆火”。
- 重点增加互动：让用户评论、站队、猜结局、想继续看。
- 输出严格 JSON。

输出结构：
{
  "title": "",
  "body": "",
  "call_to_action": "",
  "hashtags": [],
  "link": ""
}
"""


def _build_local_promo_copy(intro_text: str, promo_link: str) -> dict[str, Any]:
    intro = (intro_text or "这部短剧反转不断，每一集都有新的冲突和悬念。").strip()
    link = (promo_link or "").strip()
    return {
        "title": "这段反转，越看越上头",
        "body": f"{intro[:180]}\n\n前面越憋屈，后面反击越解气。看到最后你就知道，真正的真相从来没那么简单。",
        "call_to_action": "你站哪一边？评论区说说你的选择。" + (f"\n观看后续：{link}" if link else ""),
        "hashtags": ["#Drama", "#ShortDrama", "#Reels"],
        "link": link,
    }


def _rewrite_promo_copy(intro_text: str, promo_link: str, ai_model: str) -> dict[str, Any]:
    intro = (intro_text or "").strip()
    link = (promo_link or "").strip()
    if not intro and not link:
        return {}
    provider = AIProvider(APP_ROOT, model=ai_model or None)
    if provider.available():
        try:
            payload = provider.analyze_json(
                system_prompt=PROMO_COPY_PROMPT,
                user_payload={
                    "drama_intro": intro,
                    "promo_link": link,
                    "platform": "Facebook Reel",
                    "copy_count": 1,
                    "tone": "high-retention, emotional, interactive, curiosity-driven",
                },
            )
            if isinstance(payload, dict):
                payload["link"] = str(payload.get("link") or link)
                return payload
        except Exception as exc:
            return {**_build_local_promo_copy(intro, link), "ai_error": str(exc)}
    return _build_local_promo_copy(intro, link)


def _format_promo_copy(copy: dict[str, Any]) -> str:
    if not copy:
        return ""

    parts = [
        str(copy.get("title") or "").strip(),
        "",
        str(copy.get("body") or copy.get("short_intro") or "").strip(),
        "",
        str(copy.get("call_to_action") or "").strip(),
        "",
        " ".join(str(item) for item in copy.get("hashtags", []) if str(item).strip()),
    ]
    if copy.get("link") and str(copy.get("link")) not in "\n".join(parts):
        parts.extend(["", str(copy.get("link"))])
    if copy.get("ai_error"):
        parts.extend(["", "AI 生成失败，已使用本地模板兜底：", str(copy["ai_error"])])
    return "\n".join(part for part in parts if part is not None).strip() + "\n"


def _write_promo_copy(output_dir: Path, intro_text: str, promo_link: str, ai_model: str) -> Path | None:
    copy = _rewrite_promo_copy(intro_text, promo_link, ai_model)
    content = _format_promo_copy(copy)
    if not content:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "推广介绍文案.txt"
    target.write_text(content, encoding="utf-8")
    return target


def _render_drama_reel_variants(
    *,
    plan_path: Path,
    output_dir: Path,
    task_id: str,
    options: DramaReelOptions,
    clips_per_episode: int,
    versions_per_clip: int,
) -> tuple[list[Path], dict[str, Any]]:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    selected = _select_reels_for_variant_pipeline(plan, clips_per_episode)
    if not selected:
        return [], {"variant_pipeline": {"enabled": True, "error": "no_selected_reels"}}

    root = WORK_DIR / "drama_reel_variants" / task_id / "output"
    base_dir = root / "pipeline_base_reels"
    final_dir = output_dir / f"reels_{task_id}"
    base_dir.mkdir(parents=True, exist_ok=True)
    if final_dir.exists():
        for child in final_dir.iterdir():
            if child.is_file() and child.suffix.lower() == ".mp4":
                child.unlink()
    final_dir.mkdir(parents=True, exist_ok=True)

    base_reels: list[dict[str, Any]] = []
    total_base = len(selected)
    for index, reel in enumerate(selected, start=1):
        source = Path(str(reel.get("source_file") or ""))
        if not source.exists():
            continue
        base_path = base_dir / f"base_{index:03d}_e{int(reel.get('episode') or 1):02d}_{safe_stem(str(reel.get('id') or index))}.mp4"
        render_single_reel(source, base_path, reel, options, task_id)
        base_reels.append({"index": index, "path": base_path, "reel": reel})

    version_count = max(1, min(int(versions_per_clip or 5), 20))
    variant_options = _variant_options_for_drama_reels()
    outputs: list[Path] = []
    manifest: list[dict[str, Any]] = []
    total_outputs = len(base_reels) * version_count
    jobs: list[dict[str, Any]] = []
    for version_index in range(1, version_count + 1):
        for base in base_reels:
            output_number = (version_index - 1) * len(base_reels) + int(base["index"])
            jobs.append(
                {
                    "output_number": output_number,
                    "version_index": version_index,
                    "base": base,
                }
            )

    def render_job(job: dict[str, Any]) -> dict[str, Any]:
        base = job["base"]
        output_number = int(job["output_number"])
        output_path, effects, _ = render_variant(
                input_video=base["path"],
                output_dir=final_dir,
                task_id=f"{task_id}_p{output_number:04d}",
                options=variant_options,
                output_stem=str(output_number),
                cancel_task_id=task_id,
            )
        return {
            "output_number": output_number,
            "path": output_path,
            "filename": output_path.name,
            "version_index": int(job["version_index"]),
            "base_clip_index": base["index"],
            "episode": base["reel"].get("episode"),
            "source_reel_id": base["reel"].get("id"),
            "start": base["reel"].get("start"),
            "end": base["reel"].get("end"),
            "overall_score": base["reel"].get("overall_score"),
            "effects": effects,
        }

    worker_count = _drama_variant_worker_count(total_outputs)
    if worker_count == 1:
        results = [render_job(job) for job in jobs]
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="drama-reel-variant") as pool:
            futures = [pool.submit(render_job, job) for job in jobs]
            results = [future.result() for future in futures]

    for item in sorted(results, key=lambda value: int(value["output_number"])):
        outputs.append(Path(item.pop("path")))
        manifest.append(item)

    manifest_path = root / "processed_versions_manifest.json"
    manifest_path.write_text(json.dumps({"outputs": manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
    return outputs, {
        "variant_pipeline": {
            "enabled": True,
            "clips_per_episode": clips_per_episode,
            "base_clip_count": len(base_reels),
            "versions_per_clip": version_count,
            "output_count": len(outputs),
            "expected_output_count": total_outputs,
            "worker_count": worker_count,
            "base_dir": str(base_dir),
            "final_dir": str(final_dir),
            "manifest_path": str(manifest_path),
            "naming_rule": "version-major numeric order: v1=1..N, v2=N+1..2N",
        },
    }


def _select_directory() -> str:
    system = platform.system().lower()
    if system == "darwin":
        script = 'POSIX path of (choose folder with prompt "选择输出文件夹")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout.strip()

    if system == "windows":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$owner = New-Object System.Windows.Forms.Form; "
            "$owner.TopMost = $true; "
            "$owner.ShowInTaskbar = $false; "
            "$owner.StartPosition = 'CenterScreen'; "
            "$owner.Width = 1; $owner.Height = 1; $owner.Opacity = 0; "
            "$owner.Show(); $owner.Activate(); "
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$dialog.Description = 'Select output folder'; "
            "$dialog.ShowNewFolderButton = $true; "
            "$result = $dialog.ShowDialog($owner); "
            "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Write-Output $dialog.SelectedPath }; "
            "$owner.Close(); $owner.Dispose(); $dialog.Dispose();"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=300,
        )
        return result.stdout.strip()

    raise HTTPException(status_code=400, detail="当前系统不支持弹出文件夹选择器，请手动输入输出路径。")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    with TASK_LOCK:
        running_count = sum(1 for future in TASK_FUTURES.values() if future.running())
        pending_count = sum(1 for future in TASK_FUTURES.values() if not future.done())
    return {
        "ok": True,
        "runtime": check_runtime(),
        "data_dir": str(DATA_DIR),
        "default_parallel_jobs": DEFAULT_PARALLEL_JOBS,
        "max_parallel_jobs": MAX_WORKER_CAP,
        "active_jobs": running_count,
        "pending_jobs": pending_count,
        "video_encoder": selected_video_encoder(),
    }


@app.get("/api/version")
def version() -> dict[str, Any]:
    return _version_info()


@app.get("/api/advanced-settings")
def advanced_settings() -> dict[str, Any]:
    return {"ok": True, "config_path": str(DATA_DIR / "advanced_pipeline.json"), **load_advanced_settings(DATA_DIR)}


@app.post("/api/advanced-settings")
def update_advanced_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    # 本地桌面应用仅保存路径。路径存在性仍会在创建处理任务时再次校验。
    saved = save_advanced_settings(DATA_DIR, payload)
    return {"ok": True, "config_path": str(DATA_DIR / "advanced_pipeline.json"), **saved}


@app.get("/api/cloud/baidu/status")
def baidu_cloud_status() -> dict[str, Any]:
    return {"ok": True, **baidu_public_status(load_baidu_config(DATA_DIR)), "watcher": dict(BAIDU_WATCH_STATE)}


@app.post("/api/cloud/baidu/settings")
def baidu_cloud_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    current = load_baidu_config(DATA_DIR)
    app_key = str(payload.get("app_key") or current.get("app_key") or "").strip()
    secret_key = str(payload.get("secret_key") or current.get("secret_key") or "").strip()
    remote_value = str(payload.get("remote_dir") or current.get("remote_dir") or "").strip()
    inbox_value = str(payload.get("inbox_dir") or current.get("inbox_dir") or "").strip()
    try:
        remote_dir = normalize_remote_dir(remote_value) if remote_value else ""
        inbox_dir = normalize_remote_dir(inbox_value) if inbox_value else ""
    except BaiduPanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    redirect_uri = str(payload.get("redirect_uri") or current.get("redirect_uri") or "oob").strip()
    enabled = bool(payload.get("enabled"))
    auto_watch = bool(payload.get("auto_watch"))
    pushplus_token = str(payload.get("pushplus_token") or current.get("pushplus_token") or "").strip()
    if not app_key or not secret_key:
        raise HTTPException(status_code=400, detail="请填写百度开放平台 App Key 和 Secret Key。")
    if enabled and not remote_dir:
        raise HTTPException(status_code=400, detail="开启自动上传前，请填写目标目录，例如 /apps/你的应用名称/VideoVariantStudio。")
    if auto_watch and not inbox_dir:
        raise HTTPException(status_code=400, detail="开启自动扫描前，请填写网盘素材目录。")
    local_inbox = str(payload.get("local_inbox") or current.get("local_inbox") or (DATA_DIR / "baidu-inbox")).strip()
    local_output = str(payload.get("local_output") or current.get("local_output") or (DATA_DIR / "baidu-output")).strip()
    config = save_baidu_config(
        DATA_DIR,
        {
            "app_key": app_key,
            "secret_key": secret_key,
            "remote_dir": remote_dir,
            "inbox_dir": inbox_dir,
            "local_inbox": local_inbox,
            "local_output": local_output,
            "auto_watch": auto_watch,
            "watch_interval": max(30, min(int(payload.get("watch_interval") or current.get("watch_interval") or 60), 3600)),
            "cleanup_after_upload": bool(payload.get("cleanup_after_upload")),
            "shutdown_when_idle": bool(payload.get("shutdown_when_idle")),
            "notify_enabled": bool(payload.get("notify_enabled")),
            "pushplus_token": pushplus_token,
            "redirect_uri": redirect_uri,
            "enabled": enabled,
        },
    )
    return {"ok": True, **baidu_public_status(config)}


@app.post("/api/notifications/pushplus/test")
def test_pushplus_notification(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    config = load_baidu_config(DATA_DIR)
    token = str(payload.get("pushplus_token") or config.get("pushplus_token") or "").strip()
    try:
        send_pushplus(
            token,
            "✅ Video Variant Studio 微信通知测试",
            "## 通知配置成功\n- 百度网盘上传完成或失败时，你会收到微信消息。",
        )
    except NotificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.get("pushplus_token"):
        save_baidu_config(DATA_DIR, {"pushplus_token": token})
    return {"ok": True}


def _baidu_processed_path() -> Path:
    return DATA_DIR / "baidu_processed.json"


def _load_baidu_processed() -> dict[str, Any]:
    try:
        return json.loads(_baidu_processed_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_baidu_processed(value: dict[str, Any]) -> None:
    path = _baidu_processed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_baidu_inbox_once_unlocked() -> None:
    config = load_baidu_config(DATA_DIR)
    if not config.get("auto_watch") or not baidu_public_status(config).get("authorized"):
        BAIDU_WATCH_STATE.update(running=False, message="自动扫描未开启或尚未授权")
        return
    inbox_dir = normalize_remote_dir(str(config.get("inbox_dir") or ""))
    entries = [item for item in list_baidu_directory(DATA_DIR, inbox_dir) if int(item.get("isdir") or 0)]
    processed = _load_baidu_processed()
    seen = BAIDU_WATCH_STATE.setdefault("seen", {})
    for entry in entries:
        remote_path = str(entry.get("path") or "")
        signature = f"{entry.get('fs_id')}:{entry.get('server_mtime')}"
        if processed.get(remote_path) == signature:
            continue
        # A folder must look unchanged in two scans. This avoids downloading while
        # the user is still uploading episodes into it.
        if seen.get(remote_path) != signature:
            seen[remote_path] = signature
            BAIDU_WATCH_STATE.update(current="", message=f"已发现 {entry.get('server_filename') or '新文件夹'}，等待下一次稳定扫描")
            continue
        folder_name = safe_stem(str(entry.get("server_filename") or "drama"))
        run_id = uuid.uuid4().hex[:10]
        source_root = Path(str(config.get("local_inbox") or DATA_DIR / "baidu-inbox")) / run_id
        local_group = source_root / folder_name
        output_root = Path(str(config.get("local_output") or DATA_DIR / "baidu-output")) / run_id
        BAIDU_WATCH_STATE.update(current=folder_name, message=f"正在从百度网盘下载：{folder_name}")

        def progress(current: int, total: int, filename: str) -> None:
            percent = int(current / max(1, total) * 100)
            BAIDU_WATCH_STATE["message"] = f"正在下载 {folder_name}：{percent}% · {filename}"

        download_baidu_directory(DATA_DIR, remote_path, local_group, progress)
        auto_task_config = dict(config.get("auto_task_config") or {})
        create_drama_reel_root_batch({
            **auto_task_config,
            "source_root": str(source_root),
            "output_root": str(output_root),
        })
        processed[remote_path] = signature
        _save_baidu_processed(processed)
        BAIDU_WATCH_STATE.update(current="", message=f"已创建自动任务：{folder_name}")


def _run_baidu_inbox_once() -> None:
    # The timer and the manual Scan Now button can fire together. Serialize the
    # whole download/submission transaction so the same remote folder is never
    # submitted twice.
    with BAIDU_SCAN_LOCK:
        _run_baidu_inbox_once_unlocked()


def _baidu_watch_loop() -> None:
    BAIDU_WATCH_STATE.update(running=True, message="百度网盘自动扫描已启动")
    while not BAIDU_WATCH_STOP.is_set():
        try:
            _run_baidu_inbox_once()
        except Exception as exc:
            BAIDU_WATCH_STATE.update(message=f"自动扫描失败：{exc}", current="")
        config = load_baidu_config(DATA_DIR)
        BAIDU_WATCH_STOP.wait(max(30, min(int(config.get("watch_interval") or 60), 3600)))
    BAIDU_WATCH_STATE["running"] = False


@app.on_event("startup")
def _start_baidu_watcher() -> None:
    BAIDU_WATCH_STOP.clear()
    threading.Thread(target=_baidu_watch_loop, name="baidu-inbox-watcher", daemon=True).start()


@app.post("/api/cloud/baidu/scan-now")
def baidu_scan_now() -> dict[str, Any]:
    try:
        _run_baidu_inbox_once()
    except BaiduPanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "watcher": dict(BAIDU_WATCH_STATE)}


@app.post("/api/cloud/baidu/auto-task-settings")
def baidu_auto_task_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    allowed = {
        "versions_per_episode", "worker_count", "group_parallelism",
        "min_seconds", "max_seconds", "publish_batch_size", "intensity",
        "effect_background", "effect_zoom", "effect_color", "effect_texture",
        "effect_speed", "effect_vignette", "effect_center_scratch",
        "effect_light_sweep", "effect_film_grain", "effect_frame_extract",
        "effect_frame_interpolate", "effect_md5", "effect_border",
        "effect_random_transition", "effect_remove_progress", "effect_hook_clip",
        "effect_hook_caption", "effect_english_subtitles", "subtitle_model",
        "hook_clip_seconds", "hook_duration", "hook_texts", "intro_text",
        "advanced_pipeline", "advanced_crop_min", "advanced_crop_max",
        "advanced_speed_min", "advanced_speed_max", "advanced_head_min",
        "advanced_head_max", "advanced_tail_min", "advanced_tail_max",
        "advanced_color_min", "advanced_color_max", "advanced_fps",
        "advanced_resolution", "advanced_interpolate", "advanced_blur_bottom",
        "advanced_blur_sigma_min", "advanced_blur_sigma_max", "advanced_border",
        "advanced_eq_bands", "advanced_reverb", "advanced_watermark_path",
        "advanced_watermark_opacity", "advanced_watermark_width",
        "advanced_style_mode", "advanced_style_opacity", "advanced_style_grain",
        "advanced_pip_path", "advanced_pip_enabled", "advanced_ambient_path",
        "advanced_ambient_db", "advanced_bgm_path", "advanced_bgm_db",
        "advanced_project_name", "advanced_project_version",
    }
    saved = {key: value for key, value in payload.items() if key in allowed}
    saved.setdefault("versions_per_episode", 1)
    saved.setdefault("worker_count", 3)
    saved.setdefault("group_parallelism", 1)
    saved.setdefault("min_seconds", 28)
    saved.setdefault("max_seconds", 30)
    saved.setdefault("publish_batch_size", 5)
    saved.setdefault("intensity", "balanced")
    config = save_baidu_config(DATA_DIR, {"auto_task_config": saved})
    return {"ok": True, "auto_task_config": dict(config.get("auto_task_config") or {})}


@app.get("/api/cloud/baidu/auth-url")
def baidu_cloud_auth_url() -> dict[str, Any]:
    try:
        return {"ok": True, "url": baidu_authorization_url(load_baidu_config(DATA_DIR))}
    except BaiduPanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cloud/baidu/authorize")
def baidu_cloud_authorize(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    code = str(payload.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="请输入百度授权码。")
    try:
        config = baidu_exchange_code(DATA_DIR, code)
    except BaiduPanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **baidu_public_status(config)}


@app.post("/api/cloud/baidu/disconnect")
def baidu_cloud_disconnect() -> dict[str, Any]:
    config = save_baidu_config(DATA_DIR, {"access_token": "", "refresh_token": "", "expires_at": 0, "enabled": False})
    return {"ok": True, **baidu_public_status(config)}


@app.post("/api/select-output-dir")
def select_output_dir() -> dict[str, Any]:
    try:
        selected = _select_directory()
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=408, detail="文件夹选择窗口等待超时，请重试或手动输入路径。") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or exc.stdout or "").strip()
        raise HTTPException(status_code=400, detail=stderr or "没有选择文件夹。") from exc
    if not selected:
        raise HTTPException(status_code=400, detail="没有选择文件夹。")
    return {"ok": True, "path": selected}


@app.post("/api/cleanup-cache")
def cleanup_cache() -> dict[str, Any]:
    if _has_active_tasks():
        raise HTTPException(status_code=409, detail="仍有任务在处理，请完成或停止后再清理缓存。")
    upload_stats = _clear_directory(UPLOAD_DIR)
    work_stats = _clear_directory(WORK_DIR)
    _reset_finished_tasks()
    return {
        "ok": True,
        "uploads": upload_stats,
        "work": work_stats,
        "deleted_files": upload_stats["files"] + work_stats["files"],
        "deleted_dirs": upload_stats["dirs"] + work_stats["dirs"],
        "deleted_bytes": upload_stats["bytes"] + work_stats["bytes"],
    }


@app.post("/api/upload", response_model=UploadResponse)
async def upload_video(
    file: UploadFile = File(...),
    intensity: str = Form("balanced"),
    effect_background: bool = Form(True),
    effect_zoom: bool = Form(True),
    effect_color: bool = Form(True),
    effect_texture: bool = Form(True),
    effect_speed: bool = Form(True),
    effect_vignette: bool = Form(True),
    effect_center_scratch: bool = Form(True),
    effect_light_sweep: bool = Form(True),
    effect_film_grain: bool = Form(True),
    effect_hook_clip: bool = Form(True),
    effect_hook_caption: bool = Form(False),
    effect_frame_extract: bool = Form(True),
    effect_frame_interpolate: bool = Form(False),
    effect_md5: bool = Form(True),
    effect_mirror: bool = Form(False),
    effect_border: bool = Form(True),
    effect_random_transition: bool = Form(True),
    effect_remove_progress: bool = Form(True),
    hook_clip_seconds: float = Form(3.0),
    hook_texts: str = Form(""),
    hook_duration: float = Form(3.0),
    output_count: int = Form(1),
    worker_count: int = Form(DEFAULT_PARALLEL_JOBS),
    output_dir: str = Form(""),
    intro_text: str = Form(""),
) -> UploadResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="请选择视频文件。")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".webm"}:
        raise HTTPException(status_code=400, detail="支持 mp4 / mov / m4v / avi / webm。")

    task_id = uuid.uuid4().hex[:12]
    input_path = UPLOAD_DIR / f"{task_id}_{safe_stem(file.filename)}{suffix}"
    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    options = VariantOptions(
        intensity=intensity,
        effect_background=effect_background,
        effect_zoom=effect_zoom,
        effect_color=effect_color,
        effect_texture=effect_texture,
        effect_speed=effect_speed,
        effect_vignette=effect_vignette,
        effect_center_scratch=effect_center_scratch,
        effect_light_sweep=effect_light_sweep,
        effect_film_grain=effect_film_grain,
        effect_hook_clip=effect_hook_clip,
        effect_hook_caption=effect_hook_caption,
        effect_frame_extract=effect_frame_extract,
        effect_frame_interpolate=effect_frame_interpolate,
        effect_md5=effect_md5,
        effect_mirror=False,
        effect_border=effect_border,
        effect_random_transition=effect_random_transition,
        effect_remove_progress=effect_remove_progress,
        hook_clip_seconds=max(1.0, min(float(hook_clip_seconds or 3.0), 8.0)),
        hook_texts=_parse_hook_texts(hook_texts),
        hook_duration=max(1.0, min(float(hook_duration or 3.0), 8.0)),
    )
    count = max(1, min(int(output_count or 1), 20))
    workers = _sanitize_worker_count(worker_count)
    resolved_output_dir = _resolve_output_dir(output_dir)
    _write_intro_file(resolved_output_dir, intro_text)
    if advanced_pipeline:
        asset_values = {
            "品牌水印": advanced_watermark_path,
            "画中画": advanced_pip_path,
            "环境音": advanced_ambient_path,
            "BGM": advanced_bgm_path,
        }
        resolved_assets: dict[str, str] = {}
        for label, value in asset_values.items():
            cleaned = str(value or "").strip()
            if not cleaned:
                resolved_assets[label] = ""
                continue
            asset_path = Path(cleaned).expanduser()
            if not asset_path.is_file():
                raise HTTPException(status_code=400, detail=f"{label}素材路径不存在：{asset_path}")
            resolved_assets[label] = str(asset_path.resolve())
        advanced_watermark_path = resolved_assets["品牌水印"]
        advanced_pip_path = resolved_assets["画中画"]
        advanced_ambient_path = resolved_assets["环境音"]
        advanced_bgm_path = resolved_assets["BGM"]
    batch_id = uuid.uuid4().hex[:12]
    task = VariantTask(
        task_id=task_id,
        original_filename=file.filename,
        input_path=str(input_path),
        source_paths=[str(input_path)],
        source_filenames=[file.filename],
        batch_id=batch_id,
        worker_count=workers,
        options=options,
        output_count=count,
        tool_options={"output_dir": str(resolved_output_dir), "batch_total": 1, "batch_position": 1},
    )
    with TASK_LOCK:
        BATCH_LIMITS[batch_id] = threading.BoundedSemaphore(workers)
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/download-url", response_model=UploadResponse)
def download_from_url(payload: DownloadUrlRequest) -> UploadResponse:
    task_id = uuid.uuid4().hex[:12]
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请输入视频分享链接。")
    task = VariantTask(
        task_id=task_id,
        operation="download",
        original_filename=url,
        batch_id=uuid.uuid4().hex[:12],
        worker_count=1,
        output_count=1,
        message="等待下载分享链接",
        tool_options={
            "url": url,
            "cookies_browser": payload.cookies_browser,
            "proxy": payload.proxy,
            "allow_playlist": payload.allow_playlist,
            "max_downloads": max(1, min(int(payload.max_downloads or 30), 200)),
        },
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/merge", response_model=UploadResponse)
async def merge_uploaded_videos(
    files: list[UploadFile] = File(...),
    output_dir: str = Form(""),
) -> UploadResponse:
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="请至少选择两个视频进行合并。")

    task_id = uuid.uuid4().hex[:12]
    source_paths: list[str] = []
    source_filenames: list[str] = []
    for index, file in enumerate(files, start=1):
        suffix = _validate_video_upload(file)
        input_path = UPLOAD_DIR / f"{task_id}_merge_{index:03d}_{safe_stem(file.filename)}{suffix}"
        with input_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        source_paths.append(str(input_path))
        source_filenames.append(file.filename or input_path.name)

    resolved_output_dir = _resolve_output_dir(output_dir)
    task = VariantTask(
        task_id=task_id,
        operation="merge",
        original_filename="合并视频",
        input_path=source_paths[0],
        source_paths=source_paths,
        source_filenames=source_filenames,
        batch_id=uuid.uuid4().hex[:12],
        worker_count=1,
        output_count=1,
        tool_options={"output_dir": str(resolved_output_dir)},
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/split", response_model=UploadResponse)
async def split_uploaded_video(
    file: UploadFile = File(...),
    segment_range: str = Form("50-56"),
    output_dir: str = Form(""),
) -> UploadResponse:
    suffix = _validate_video_upload(file)
    min_seconds, max_seconds = _parse_split_range(segment_range)

    task_id = uuid.uuid4().hex[:12]
    input_path = UPLOAD_DIR / f"{task_id}_split_{safe_stem(file.filename)}{suffix}"
    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    resolved_output_dir = _resolve_output_dir(output_dir)
    task = VariantTask(
        task_id=task_id,
        operation="split",
        original_filename=file.filename or input_path.name,
        input_path=str(input_path),
        source_paths=[str(input_path)],
        source_filenames=[file.filename or input_path.name],
        batch_id=uuid.uuid4().hex[:12],
        worker_count=1,
        output_count=1,
        tool_options={
            "min_seconds": min_seconds,
            "max_seconds": max_seconds,
            "output_dir": str(resolved_output_dir),
        },
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/drama-factory", response_model=UploadResponse)
async def short_drama_factory(
    file: UploadFile = File(...),
    max_clips: int = Form(3),
    min_seconds: float = Form(15),
    max_seconds: float = Form(35),
    versions_per_clip: int = Form(5),
    worker_count: int = Form(1),
    whisper_model: str = Form("base"),
) -> UploadResponse:
    suffix = _validate_video_upload(file)
    task_id = uuid.uuid4().hex[:12]
    input_path = UPLOAD_DIR / f"{task_id}_drama_{safe_stem(file.filename)}{suffix}"
    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    safe_max_clips = max(1, min(int(max_clips or 1), 10))
    safe_versions = max(1, min(int(versions_per_clip or 1), 5))
    task = VariantTask(
        task_id=task_id,
        operation="drama_factory",
        original_filename=file.filename or input_path.name,
        input_path=str(input_path),
        source_paths=[str(input_path)],
        source_filenames=[file.filename or input_path.name],
        batch_id=uuid.uuid4().hex[:12],
        worker_count=_sanitize_worker_count(worker_count),
        output_count=safe_max_clips * safe_versions,
        tool_options={
            "max_clips": safe_max_clips,
            "min_seconds": max(5, float(min_seconds or 15)),
            "max_seconds": max(6, float(max_seconds or 35)),
            "versions_per_clip": safe_versions,
            "whisper_model": whisper_model or "base",
        },
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/drama-reels/analyze", response_model=UploadResponse)
async def drama_reels_analyze(
    files: list[UploadFile] = File(...),
    output_dir: str = Form(""),
    max_episodes: int = Form(10),
    max_reels: int = Form(20),
    target_seconds: float = Form(30),
    min_seconds: float = Form(18),
    max_seconds: float = Form(55),
    frame_mode: str = Form("blur"),
    burn_subtitles: bool = Form(True),
    subtitle_mode: str = Form("english"),
    generate_videos: bool = Form(False),
    ai_model: str = Form(""),
    process_variants: bool = Form(False),
    clips_per_episode: int = Form(3),
    versions_per_clip: int = Form(5),
    intro_text: str = Form(""),
    promo_link: str = Form(""),
) -> UploadResponse:
    valid_files = [file for file in files if file.filename]
    if not valid_files:
        raise HTTPException(status_code=400, detail="请至少选择一集视频。")

    task_id = uuid.uuid4().hex[:12]
    batch_dir = UPLOAD_DIR / f"{task_id}_drama_reels"
    batch_dir.mkdir(parents=True, exist_ok=True)
    source_paths: list[str] = []
    source_filenames: list[str] = []
    for index, file in enumerate(valid_files[: max(1, min(int(max_episodes or 10), 50))], start=1):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}:
            raise HTTPException(status_code=400, detail=f"{file.filename} 格式不支持。")
        input_path = batch_dir / f"{index:03d}_{safe_stem(file.filename)}{suffix}"
        with input_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        source_paths.append(str(input_path))
        source_filenames.append(file.filename or input_path.name)

    resolved_output_dir = _resolve_output_dir(output_dir)
    safe_clips_per_episode = max(1, min(int(clips_per_episode or 3), 20))
    safe_versions_per_clip = max(1, min(int(versions_per_clip or 5), 20))
    estimated_output_count = (
        len(source_paths) * safe_clips_per_episode * safe_versions_per_clip
        if process_variants
        else max(1, min(int(max_reels or 20), 50))
    )
    task = VariantTask(
        task_id=task_id,
        operation="drama_reel_analyzer",
        original_filename=f"Drama Reel Analyzer ({len(source_paths)} episodes)",
        input_path=source_paths[0],
        source_paths=source_paths,
        source_filenames=source_filenames,
        batch_id=uuid.uuid4().hex[:12],
        worker_count=_drama_variant_worker_count(estimated_output_count) if process_variants else 1,
        output_count=estimated_output_count,
        message="等待分析剧集爆点",
        tool_options={
            "output_dir": str(resolved_output_dir),
            "max_episodes": max_episodes,
            "max_reels": max_reels,
            "target_seconds": target_seconds,
            "min_seconds": min_seconds,
            "max_seconds": max_seconds,
            "frame_mode": frame_mode,
            "burn_subtitles": burn_subtitles,
            "subtitle_mode": subtitle_mode,
            "generate_videos": generate_videos,
            "ai_model": ai_model,
            "process_variants": process_variants,
            "clips_per_episode": safe_clips_per_episode,
            "versions_per_clip": safe_versions_per_clip,
            "intro_text": intro_text,
            "promo_link": promo_link,
        },
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/drama-reels/root-scan")
def scan_drama_reel_root(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    source_value = str(payload.get("source_root") or "").strip()
    if not source_value:
        raise HTTPException(status_code=400, detail="请选择剧集源根目录。")
    source_root = Path(source_value).expanduser().resolve()
    if not source_root.is_dir():
        raise HTTPException(status_code=400, detail="剧集源根目录不存在或不可读取。")
    groups = _scan_drama_source_root(source_root)
    return {
        "ok": True,
        "source_root": str(source_root),
        "group_count": len(groups),
        "video_count": sum(len(group["videos"]) for group in groups),
        "groups": [
            {
                "name": group["name"],
                "drama_id": group["drama_id"],
                "video_count": len(group["videos"]),
                "text_count": sum(1 for path in group["path"].iterdir() if path.is_file() and path.suffix.lower() == ".txt"),
                "preview": [path.name for path in group["videos"][:5]],
            }
            for group in groups
        ],
    }


@app.post("/api/drama-reels/root-batch", response_model=BatchUploadResponse)
def create_drama_reel_root_batch(payload: dict[str, Any] = Body(default_factory=dict)) -> BatchUploadResponse:
    source_value = str(payload.get("source_root") or "").strip()
    output_value = str(payload.get("output_root") or "").strip()
    if not source_value or not output_value:
        raise HTTPException(status_code=400, detail="请选择源根目录和输出目标根目录。")
    source_root = Path(source_value).expanduser().resolve()
    if not source_root.is_dir():
        raise HTTPException(status_code=400, detail="剧集源根目录不存在或不可读取。")
    output_root = _resolve_output_dir(output_value)
    try:
        output_root.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise HTTPException(status_code=400, detail="输出目录不能放在源根目录内部，否则会被重复扫描。")

    groups = _scan_drama_source_root(source_root)
    if not groups:
        raise HTTPException(status_code=400, detail="没有找到包含视频的一级子文件夹。")
    if len(groups) > 200:
        raise HTTPException(status_code=400, detail="一次最多处理 200 个一级子文件夹。")
    oversized = [group["name"] for group in groups if len(group["videos"]) > 200]
    if oversized:
        raise HTTPException(status_code=400, detail=f"每个文件夹最多 200 个视频：{', '.join(oversized[:5])}")

    try:
        safe_versions = max(1, min(int(payload.get("versions_per_episode") or 1), 10))
        safe_workers = _sanitize_worker_count(int(payload.get("worker_count") or 3))
        group_parallelism = max(1, min(int(payload.get("group_parallelism") or 1), 4))
        safe_min_seconds = max(5.0, min(float(payload.get("min_seconds") or 28), 90.0))
        safe_max_seconds = max(safe_min_seconds, min(float(payload.get("max_seconds") or 30), 120.0))
        publish_batch_size = max(2, min(int(payload.get("publish_batch_size") or 5), 20))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="线程数、版本数或切分秒数格式不正确。") from exc

    root_batch_id = uuid.uuid4().hex[:12]
    common_intro = str(payload.get("intro_text") or "")
    options = _drama_options_from_payload(payload)
    subtitle_model = str(payload.get("subtitle_model") or "base")
    responses: list[UploadResponse] = []
    with TASK_LOCK:
        BATCH_LIMITS[root_batch_id] = threading.BoundedSemaphore(group_parallelism)

    for index, group in enumerate(groups, start=1):
        task_id = uuid.uuid4().hex[:12]
        group_output = output_root / str(group["name"])
        group_output.mkdir(parents=True, exist_ok=True)
        source_paths = [str(path) for path in group["videos"]]
        task = VariantTask(
            task_id=task_id,
            operation="drama_batch_reels",
            original_filename=f"{group['name']}（{len(source_paths)} 集）",
            input_path=source_paths[0],
            source_paths=source_paths,
            source_filenames=[path.name for path in group["videos"]],
            batch_id=root_batch_id,
            worker_count=safe_workers,
            options=options,
            output_count=max(1, len(source_paths) * safe_versions),
            message=f"等待处理目录 {index}/{len(groups)}：{group['name']}",
            tool_options={
                **_advanced_profile_from_payload(payload),
                "output_dir": str(group_output),
                "versions_per_episode": safe_versions,
                "min_seconds": safe_min_seconds,
                "max_seconds": safe_max_seconds,
                "intro_text": _folder_intro_text(group["path"], common_intro),
                "source_text_dir": str(group["path"]),
                "cloud_group_id": f"{root_batch_id}_{index:03d}",
                "cloud_folder_name": str(group["name"]),
                "effect_english_subtitles": _payload_bool(payload, "effect_english_subtitles", False),
                "subtitle_model": subtitle_model if subtitle_model in {"tiny", "base", "small", "medium"} else "base",
                "direct_output": True,
                "root_batch": True,
                "root_output_dir": str(output_root),
                "source_group_name": str(group["name"]),
                "drama_id": str(group["drama_id"]),
                "publish_batch_size": publish_batch_size,
                "root_group_position": index,
                "root_group_total": len(groups),
                "group_parallelism": group_parallelism,
            },
        )
        with TASK_LOCK:
            TASKS[task_id] = task
        _submit_task(task_id)
        responses.append(UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}"))
    return BatchUploadResponse(tasks=responses)


@app.post("/api/drama-reels/batch", response_model=UploadResponse)
async def drama_reels_batch(
    files: list[UploadFile] = File(...),
    output_dir: str = Form(""),
    cloud_group_id: str = Form(""),
    cloud_folder_name: str = Form(""),
    drama_id: str = Form(""),
    max_episodes: int = Form(20),
    versions_per_episode: int = Form(1),
    worker_count: int = Form(3),
    min_seconds: float = Form(28),
    max_seconds: float = Form(30),
    intensity: str = Form("balanced"),
    effect_background: bool = Form(True),
    effect_zoom: bool = Form(True),
    effect_color: bool = Form(True),
    effect_texture: bool = Form(True),
    effect_speed: bool = Form(True),
    effect_vignette: bool = Form(True),
    effect_center_scratch: bool = Form(True),
    effect_light_sweep: bool = Form(True),
    effect_film_grain: bool = Form(True),
    effect_hook_clip: bool = Form(False),
    effect_hook_caption: bool = Form(False),
    effect_frame_extract: bool = Form(True),
    effect_frame_interpolate: bool = Form(False),
    effect_md5: bool = Form(True),
    effect_border: bool = Form(True),
    effect_random_transition: bool = Form(True),
    effect_remove_progress: bool = Form(True),
    hook_clip_seconds: float = Form(3.0),
    hook_texts: str = Form(""),
    hook_duration: float = Form(3.0),
    intro_text: str = Form(""),
    effect_english_subtitles: bool = Form(False),
    subtitle_model: str = Form("base"),
) -> UploadResponse:
    valid_files = [file for file in files if file.filename]
    if not valid_files:
        raise HTTPException(status_code=400, detail="请至少选择一集视频。")

    safe_max_episodes = max(1, min(int(max_episodes or 20), 50))
    safe_versions = max(1, min(int(versions_per_episode or 1), 10))
    safe_workers = _sanitize_worker_count(worker_count)
    safe_min_seconds = max(5.0, min(float(min_seconds or 28), 90.0))
    safe_max_seconds = max(safe_min_seconds, min(float(max_seconds or 30), 120.0))
    task_id = uuid.uuid4().hex[:12]
    batch_dir = UPLOAD_DIR / f"{task_id}_drama_batch"
    batch_dir.mkdir(parents=True, exist_ok=True)
    source_paths: list[str] = []
    source_filenames: list[str] = []

    for index, file in enumerate(valid_files[:safe_max_episodes], start=1):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}:
            raise HTTPException(status_code=400, detail=f"{file.filename} 格式不支持。")
        input_path = batch_dir / f"{index:03d}_{safe_stem(file.filename)}{suffix}"
        with input_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        source_paths.append(str(input_path))
        source_filenames.append(file.filename or input_path.name)

    estimated_parts = 0
    midpoint = (safe_min_seconds + safe_max_seconds) / 2
    for path in source_paths:
        try:
            estimated_parts += max(1, int(round(get_video_info(path).duration / midpoint)))
        except Exception:
            estimated_parts += 1

    resolved_output_dir = _resolve_output_dir(output_dir)
    options = VariantOptions(
        intensity=intensity if intensity in {"light", "balanced", "strong"} else "balanced",
        effect_background=effect_background,
        effect_zoom=effect_zoom,
        effect_color=effect_color,
        effect_texture=effect_texture,
        effect_speed=effect_speed,
        effect_vignette=effect_vignette,
        effect_center_scratch=effect_center_scratch,
        effect_light_sweep=effect_light_sweep,
        effect_film_grain=effect_film_grain,
        effect_hook_clip=effect_hook_clip,
        effect_hook_caption=effect_hook_caption,
        effect_frame_extract=effect_frame_extract,
        effect_frame_interpolate=effect_frame_interpolate,
        effect_md5=effect_md5,
        effect_mirror=False,
        effect_border=effect_border,
        effect_random_transition=effect_random_transition,
        effect_remove_progress=effect_remove_progress,
        hook_clip_seconds=max(1.0, min(float(hook_clip_seconds or 3.0), 8.0)),
        hook_texts=_parse_hook_texts(hook_texts),
        hook_duration=max(1.0, min(float(hook_duration or 3.0), 8.0)),
    )
    saved_profile = dict(load_baidu_config(DATA_DIR).get("auto_task_config") or {})
    task = VariantTask(
        task_id=task_id,
        operation="drama_batch_reels",
        original_filename=f"整集批量 Reel（{len(source_paths)} 集）",
        input_path=source_paths[0],
        source_paths=source_paths,
        source_filenames=source_filenames,
        batch_id=uuid.uuid4().hex[:12],
        worker_count=safe_workers,
        options=options,
        output_count=max(1, estimated_parts * safe_versions),
        message="等待整集 Reel 批处理",
        tool_options={
            **_advanced_profile_from_payload(saved_profile),
            "output_dir": str(resolved_output_dir),
            "versions_per_episode": safe_versions,
            "min_seconds": safe_min_seconds,
            "max_seconds": safe_max_seconds,
            "intro_text": intro_text,
            "cloud_group_id": cloud_group_id.strip(),
            "cloud_folder_name": cloud_folder_name.strip(),
            "drama_id": _normalize_drama_id(
                drama_id,
                cloud_folder_name.strip() or Path(resolved_output_dir).name or "drama",
            ),
            "effect_english_subtitles": effect_english_subtitles,
            "subtitle_model": subtitle_model if subtitle_model in {"tiny", "base", "small", "medium"} else "base",
        },
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/drama-reels/{source_task_id}/generate", response_model=UploadResponse)
def generate_drama_reels(source_task_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> UploadResponse:
    with TASK_LOCK:
        source_task = TASKS.get(source_task_id)
    if not source_task:
        raise HTTPException(status_code=404, detail="分析任务不存在。")
    plan_path = str(source_task.effects.get("reel_plan_path") or "")
    if not plan_path or not Path(plan_path).exists():
        raise HTTPException(status_code=404, detail="还没有可生成的视频方案。")

    task_id = uuid.uuid4().hex[:12]
    selected_ids = payload.get("selected_ids") or []
    output_dir = str(payload.get("output_dir") or source_task.effects.get("reels_dir") or _task_output_dir(source_task))
    task = VariantTask(
        task_id=task_id,
        operation="drama_reel_generate",
        original_filename=f"Generate Reels from {source_task_id}",
        input_path=source_task.input_path,
        source_paths=source_task.source_paths,
        source_filenames=source_task.source_filenames,
        batch_id=uuid.uuid4().hex[:12],
        worker_count=1,
        output_count=max(1, len(selected_ids) if selected_ids else 20),
        message="等待生成 Reel 视频",
        tool_options={
            **source_task.tool_options,
            "output_dir": output_dir,
            "plan_path": plan_path,
            "selected_ids": selected_ids,
            "generate_videos": True,
        },
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/upload-batch", response_model=BatchUploadResponse)
async def upload_batch(
    files: list[UploadFile] = File(...),
    intensity: str = Form("balanced"),
    effect_background: bool = Form(True),
    effect_zoom: bool = Form(True),
    effect_color: bool = Form(True),
    effect_texture: bool = Form(True),
    effect_speed: bool = Form(True),
    effect_vignette: bool = Form(True),
    effect_center_scratch: bool = Form(True),
    effect_light_sweep: bool = Form(True),
    effect_film_grain: bool = Form(True),
    effect_hook_clip: bool = Form(True),
    effect_hook_caption: bool = Form(False),
    effect_frame_extract: bool = Form(True),
    effect_frame_interpolate: bool = Form(False),
    effect_md5: bool = Form(True),
    effect_mirror: bool = Form(False),
    effect_border: bool = Form(True),
    effect_random_transition: bool = Form(True),
    effect_remove_progress: bool = Form(True),
    hook_clip_seconds: float = Form(3.0),
    hook_texts: str = Form(""),
    hook_duration: float = Form(3.0),
    output_count: int = Form(1),
    worker_count: int = Form(DEFAULT_PARALLEL_JOBS),
    output_dir: str = Form(""),
    intro_text: str = Form(""),
    batch_id: str = Form(""),
    batch_total: int = Form(0),
    batch_start: int = Form(1),
    cloud_group_id: str = Form(""),
    cloud_folder_name: str = Form(""),
    effect_english_subtitles: bool = Form(False),
    subtitle_model: str = Form("base"),
    advanced_pipeline: bool = Form(False),
    advanced_crop_min: float = Form(0.02),
    advanced_crop_max: float = Form(0.05),
    advanced_speed_min: float = Form(1.015),
    advanced_speed_max: float = Form(1.045),
    advanced_head_min: float = Form(0.2),
    advanced_head_max: float = Form(0.5),
    advanced_tail_min: float = Form(0.3),
    advanced_tail_max: float = Form(0.6),
    advanced_color_min: float = Form(0.98),
    advanced_color_max: float = Form(1.03),
    advanced_fps: int = Form(0),
    advanced_resolution: str = Form("720p"),
    advanced_interpolate: bool = Form(False),
    advanced_blur_bottom: bool = Form(False),
    advanced_blur_sigma_min: float = Form(14.0),
    advanced_blur_sigma_max: float = Form(22.0),
    advanced_border: bool = Form(False),
    advanced_eq_bands: int = Form(0),
    advanced_reverb: bool = Form(False),
    advanced_watermark_path: str = Form(""),
    advanced_watermark_opacity: float = Form(0.22),
    advanced_watermark_width: float = Form(0.12),
    advanced_style_mode: str = Form("film"),
    advanced_style_opacity: float = Form(0.10),
    advanced_style_grain: float = Form(1.2),
    advanced_pip_path: str = Form(""),
    advanced_pip_enabled: bool = Form(False),
    advanced_ambient_path: str = Form(""),
    advanced_ambient_db: float = Form(-40.0),
    advanced_bgm_path: str = Form(""),
    advanced_bgm_db: float = Form(-24.0),
    advanced_project_name: str = Form("VideoVariantStudio"),
    advanced_project_version: str = Form(APP_VERSION),
) -> BatchUploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个视频文件。")

    responses: list[UploadResponse] = []
    workers = _sanitize_worker_count(worker_count)
    resolved_output_dir = _resolve_output_dir(output_dir)
    _write_intro_file(resolved_output_dir, intro_text)
    valid_files = [file for file in files if file.filename]
    total_count = max(len(valid_files), int(batch_total or 0))
    start_position = max(1, int(batch_start or 1))
    batch_id = (batch_id or "").strip() or uuid.uuid4().hex[:12]
    with TASK_LOCK:
        if batch_id not in BATCH_LIMITS:
            BATCH_LIMITS[batch_id] = threading.BoundedSemaphore(workers)
    for index, file in enumerate(valid_files, start=1):
        if not file.filename:
            continue
        suffix = Path(file.filename).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".webm"}:
            raise HTTPException(status_code=400, detail=f"{file.filename} 格式不支持。")
        task_id = uuid.uuid4().hex[:12]
        input_path = UPLOAD_DIR / f"{task_id}_{index:03d}_{safe_stem(file.filename)}{suffix}"
        with input_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        try:
            source_info = get_video_info(input_path)
        except Exception as exc:
            input_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"{file.filename} 无法读取视频信息：{exc}") from exc
        if source_info.width <= 0 or source_info.height <= 0 or source_info.fps < 1.0:
            input_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{file.filename} 的视频时间轴异常（检测帧率 {source_info.fps:g} FPS，"
                    f"时长 {source_info.duration:g} 秒），请重新切分或修复后再处理。"
                ),
            )
        options = VariantOptions(
            intensity=intensity,
            effect_background=effect_background,
            effect_zoom=effect_zoom,
            effect_color=effect_color,
            effect_texture=effect_texture,
            effect_speed=effect_speed,
            effect_vignette=effect_vignette,
            effect_center_scratch=effect_center_scratch,
            effect_light_sweep=effect_light_sweep,
            effect_film_grain=effect_film_grain,
            effect_hook_clip=effect_hook_clip,
            effect_hook_caption=effect_hook_caption,
            effect_frame_extract=effect_frame_extract,
            effect_frame_interpolate=effect_frame_interpolate,
            effect_md5=effect_md5,
            effect_mirror=False,
            effect_border=effect_border,
            effect_random_transition=effect_random_transition,
            effect_remove_progress=effect_remove_progress,
            hook_clip_seconds=max(1.0, min(float(hook_clip_seconds or 3.0), 8.0)),
            hook_texts=_parse_hook_texts(hook_texts),
            hook_duration=max(1.0, min(float(hook_duration or 3.0), 8.0)),
        )
        task = VariantTask(
            task_id=task_id,
            original_filename=file.filename,
            input_path=str(input_path),
            source_paths=[str(input_path)],
            source_filenames=[file.filename],
            batch_id=batch_id,
            worker_count=workers,
            options=options,
            output_count=max(1, min(int(output_count or 1), 20)),
            tool_options={
                "output_dir": str(resolved_output_dir),
                "batch_total": total_count,
                "batch_position": start_position + index - 1,
                "cloud_group_id": cloud_group_id.strip(),
                "cloud_folder_name": cloud_folder_name.strip(),
                "effect_english_subtitles": effect_english_subtitles,
                "subtitle_model": subtitle_model if subtitle_model in {"tiny", "base", "small", "medium"} else "base",
                "advanced_pipeline": advanced_pipeline,
                "advanced_crop_min": advanced_crop_min,
                "advanced_crop_max": advanced_crop_max,
                "advanced_speed_min": advanced_speed_min,
                "advanced_speed_max": advanced_speed_max,
                "advanced_head_min": advanced_head_min,
                "advanced_head_max": advanced_head_max,
                "advanced_tail_min": advanced_tail_min,
                "advanced_tail_max": advanced_tail_max,
                "advanced_color_min": advanced_color_min,
                "advanced_color_max": advanced_color_max,
                "advanced_fps": advanced_fps,
                "advanced_resolution": advanced_resolution,
                "advanced_interpolate": advanced_interpolate,
                "advanced_blur_bottom": advanced_blur_bottom,
                "advanced_blur_sigma_min": advanced_blur_sigma_min,
                "advanced_blur_sigma_max": advanced_blur_sigma_max,
                "advanced_border": advanced_border,
                "advanced_eq_bands": advanced_eq_bands,
                "advanced_reverb": advanced_reverb,
                "advanced_watermark_path": advanced_watermark_path.strip(),
                "advanced_watermark_opacity": advanced_watermark_opacity,
                "advanced_watermark_width": advanced_watermark_width,
                "advanced_style_mode": advanced_style_mode,
                "advanced_style_opacity": advanced_style_opacity,
                "advanced_style_grain": advanced_style_grain,
                "advanced_pip_path": advanced_pip_path.strip(),
                "advanced_pip_enabled": advanced_pip_enabled,
                "advanced_ambient_path": advanced_ambient_path.strip(),
                "advanced_ambient_db": advanced_ambient_db,
                "advanced_bgm_path": advanced_bgm_path.strip(),
                "advanced_bgm_db": advanced_bgm_db,
                "advanced_project_name": advanced_project_name.strip() or "VideoVariantStudio",
                "advanced_project_version": advanced_project_version.strip() or APP_VERSION,
            },
        )
        with TASK_LOCK:
            TASKS[task_id] = task
        responses.append(UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}"))
        _submit_task(task_id)

    if not responses:
        raise HTTPException(status_code=400, detail="没有读取到有效视频文件。")

    return BatchUploadResponse(tasks=responses)


@app.get("/api/tasks")
def list_tasks() -> dict[str, Any]:
    with TASK_LOCK:
        tasks = list(TASKS.values())
    return {
        "ok": True,
        "default_parallel_jobs": DEFAULT_PARALLEL_JOBS,
        "max_parallel_jobs": MAX_WORKER_CAP,
        "tasks": [_dump(task) for task in tasks],
    }


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    with TASK_LOCK:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return _dump(task)


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        future = TASK_FUTURES.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if task.status in {TaskState.completed, TaskState.failed, TaskState.cancelled}:
        return {"ok": True, "task": _dump(task)}

    task.cancel_requested = True
    request_cancel(task_id)
    if future is not None and future.cancel():
        _set(task, status=TaskState.cancelled, progress=task.progress, message="任务已停止")
    else:
        _set(task, progress=task.progress, message="正在停止任务...")
    return {"ok": True, "task": _dump(task)}


@app.get("/api/download/{task_id}")
def download(task_id: str) -> FileResponse:
    task = TASKS.get(task_id)
    if not task or task.operation not in {"merge", "download"}:
        raise HTTPException(status_code=404, detail="输出文件不存在。")
    output = task.output_path or (task.variant_paths[0] if task.variant_paths else None)
    if not output:
        raise HTTPException(status_code=404, detail="输出文件不存在。")
    path = Path(output)
    if not path.exists():
        raise HTTPException(status_code=404, detail="输出文件已不存在。")
    return FileResponse(path, filename=path.name, media_type=_media_type(path, "video/mp4"))


@app.get("/api/download/{task_id}/package")
def download_package(task_id: str) -> FileResponse:
    task = TASKS.get(task_id)
    if not task or task.operation != "split" or not task.package_path:
        raise HTTPException(status_code=404, detail="整包文件不存在。")
    path = Path(task.package_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="整包文件已不存在。")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/api/download/{task_id}/variants/{index}")
def download_variant(task_id: str, index: int) -> FileResponse:
    task = TASKS.get(task_id)
    if not task or task.operation not in {"split", "download"} or index < 1 or index > len(task.variant_paths):
        raise HTTPException(status_code=404, detail="输出文件不存在。")
    path = Path(task.variant_paths[index - 1])
    if not path.exists():
        raise HTTPException(status_code=404, detail="输出文件已不存在。")
    return FileResponse(path, media_type=_media_type(path, "video/mp4"), filename=path.name)


@app.get("/api/drama-reels/{task_id}/reports/{name}")
def download_drama_report(task_id: str, name: str) -> FileResponse:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")
    allowed = {
        "highlights": "highlights_path",
        "reel_plan": "reel_plan_path",
        "top20": "top20_path",
    }
    key = allowed.get(name)
    if not key:
        raise HTTPException(status_code=404, detail="报告不存在。")
    path = Path(str(task.effects.get(key) or ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件不存在。")
    return FileResponse(path, media_type="application/json", filename=path.name)

from __future__ import annotations

import subprocess
import shutil
import os
import threading
import traceback
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import BatchUploadResponse, TaskState, UploadResponse, VariantOptions, VariantTask
from .video_utils import app_root, asset_root, check_runtime, get_video_info, safe_stem
from .visual_variant import merge_videos, render_variant, split_video_by_random_range


APP_ROOT = app_root()
ASSET_ROOT = asset_root()
DATA_DIR = APP_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
STATIC_DIR = ASSET_ROOT / "static"
for directory in (UPLOAD_DIR, OUTPUT_DIR):
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

app = FastAPI(
    title="Video Variant Studio",
    description="Local visual variant studio for content A/B testing and batch video processing.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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

    if task.status == TaskState.failed:
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


def _submit_task(task_id: str) -> None:
    with TASK_LOCK:
        task = TASKS[task_id]
    _set(task, status=TaskState.queued, progress=0, message=f"等待调度，本批线程数 {task.worker_count} 个视频")
    future = EXECUTOR.submit(_process, task_id)
    with TASK_LOCK:
        TASK_FUTURES[task_id] = future


def _version_info() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "-C", str(APP_ROOT), "log", "-1", "--format=%h|%ci|%s"],
            capture_output=True,
            text=True,
            check=True,
        )
        commit, committed_at, subject = (result.stdout.strip().split("|", 2) + ["", "", ""])[:3]
        return {
            "ok": True,
            "version": commit or "dev",
            "committed_at": committed_at,
            "subject": subject,
        }
    except Exception:
        return {
            "ok": True,
            "version": "local",
            "committed_at": "",
            "subject": "Packaged local build",
        }


def _process(task_id: str) -> None:
    with TASK_LOCK:
        task = TASKS[task_id]
        limiter = BATCH_LIMITS.get(task.batch_id)

    renderer = _render_task if task.operation == "variant" else _render_tool_task
    if limiter is not None:
        _set(task, status=TaskState.queued, progress=0, message=f"等待本批空闲线程，本批线程数 {task.worker_count}")
        with limiter:
            renderer(task)
    else:
        renderer(task)


def _render_task(task: VariantTask) -> None:
    try:
        runtime = check_runtime()
        if not runtime.get("ok"):
            raise RuntimeError(str(runtime.get("error") or "FFmpeg runtime missing"))
        _set(task, status=TaskState.processing, progress=8, message="正在准备视频素材")
        input_video = task.input_path
        variant_paths: list[str] = []
        effects_by_version: dict[str, Any] = {}
        info = None
        total = max(1, min(task.output_count, 20))
        for index in range(1, total + 1):
            progress = 12 + int((index - 1) / total * 78)
            _set(task, progress=progress, message=f"正在生成第 {index}/{total} 个视觉版本")
            variant_task_id = f"{task.task_id}v{index:02d}"
            output_path, effects, info = render_variant(
                input_video=input_video,
                output_dir=OUTPUT_DIR,
                task_id=variant_task_id,
                options=task.options,
                output_stem=f"{safe_stem(task.original_filename)}_version_{index:02d}",
            )
            variant_paths.append(str(output_path))
            effects_by_version[f"version_{index:02d}"] = effects

        task.variant_paths = variant_paths
        task.variant_download_urls = [f"/api/download/{task.task_id}/variants/{index}" for index in range(1, len(variant_paths) + 1)]
        output_path = Path(variant_paths[0])
        task.video_info = info
        task.effects = effects_by_version
        task.output_path = str(output_path)
        task.download_url = task.variant_download_urls[0] if len(task.variant_download_urls) == 1 else None
        _set(task, status=TaskState.completed, progress=100, message=f"处理完成，已生成 {total} 个独立版本")
    except Exception as exc:
        task.error = str(exc)
        task.effects["traceback"] = traceback.format_exc(limit=6)
        _set(task, status=TaskState.failed, progress=100, message="处理失败")


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

        if task.operation == "merge":
            _set(task, status=TaskState.processing, progress=15, message="正在按选择顺序合并视频")
            output_path = merge_videos(input_paths=task.source_paths, work_dir=OUTPUT_DIR, task_id=task.task_id)
            task.output_path = str(output_path)
            task.download_url = f"/api/download/{task.task_id}"
            task.video_info = get_video_info(output_path)
            task.effects = {"operation": "merge", "source_count": len(task.source_paths)}
            _set(task, status=TaskState.completed, progress=100, message=f"合并完成，共 {len(task.source_paths)} 个视频")
            return

        if task.operation == "split":
            min_seconds = float(task.tool_options.get("min_seconds", 50))
            max_seconds = float(task.tool_options.get("max_seconds", 56))
            _set(task, status=TaskState.processing, progress=15, message=f"正在按 {min_seconds:g}-{max_seconds:g} 秒随机切分视频")
            parts = split_video_by_random_range(
                input_video=task.input_path,
                output_dir=OUTPUT_DIR,
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
            package_path = OUTPUT_DIR / f"{task.task_id}_split_parts.zip"
            _zip_outputs(package_path, paths)
            task.package_path = str(package_path)
            task.package_url = f"/api/download/{task.task_id}/package"
            _set(task, status=TaskState.completed, progress=100, message=f"切分完成，共 {len(paths)} 个片段")
            return

        raise RuntimeError(f"未知任务类型：{task.operation}")
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
    }


@app.get("/api/version")
def version() -> dict[str, Any]:
    return _version_info()


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
    output_count: int = Form(1),
    worker_count: int = Form(DEFAULT_PARALLEL_JOBS),
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
    )
    count = max(1, min(int(output_count or 1), 20))
    workers = _sanitize_worker_count(worker_count)
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
    )
    with TASK_LOCK:
        BATCH_LIMITS[batch_id] = threading.BoundedSemaphore(workers)
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/merge", response_model=UploadResponse)
async def merge_uploaded_videos(files: list[UploadFile] = File(...)) -> UploadResponse:
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
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _submit_task(task_id)
    return UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")


@app.post("/api/split", response_model=UploadResponse)
async def split_uploaded_video(
    file: UploadFile = File(...),
    segment_range: str = Form("50-56"),
) -> UploadResponse:
    suffix = _validate_video_upload(file)
    min_seconds, max_seconds = _parse_split_range(segment_range)

    task_id = uuid.uuid4().hex[:12]
    input_path = UPLOAD_DIR / f"{task_id}_split_{safe_stem(file.filename)}{suffix}"
    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

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
        tool_options={"min_seconds": min_seconds, "max_seconds": max_seconds},
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
    output_count: int = Form(1),
    worker_count: int = Form(DEFAULT_PARALLEL_JOBS),
) -> BatchUploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个视频文件。")

    responses: list[UploadResponse] = []
    workers = _sanitize_worker_count(worker_count)
    batch_id = uuid.uuid4().hex[:12]
    with TASK_LOCK:
        BATCH_LIMITS[batch_id] = threading.BoundedSemaphore(workers)
    for index, file in enumerate(files, start=1):
        if not file.filename:
            continue
        suffix = Path(file.filename).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".webm"}:
            raise HTTPException(status_code=400, detail=f"{file.filename} 格式不支持。")
        task_id = uuid.uuid4().hex[:12]
        input_path = UPLOAD_DIR / f"{task_id}_{index:03d}_{safe_stem(file.filename)}{suffix}"
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


@app.get("/api/download/{task_id}")
def download(task_id: str) -> FileResponse:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="输出文件不存在。")
    output = task.output_path or (task.variant_paths[0] if task.variant_paths else None)
    if not output:
        raise HTTPException(status_code=404, detail="输出文件不存在。")
    path = Path(output)
    if not path.exists():
        raise HTTPException(status_code=404, detail="输出文件已不存在。")
    return FileResponse(path, filename=path.name, media_type="video/mp4")


@app.get("/api/download/{task_id}/package")
def download_package(task_id: str) -> FileResponse:
    task = TASKS.get(task_id)
    if not task or not task.package_path:
        raise HTTPException(status_code=404, detail="整包文件不存在。")
    path = Path(task.package_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="整包文件已不存在。")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/api/download/{task_id}/variants/{index}")
def download_variant(task_id: str, index: int) -> FileResponse:
    task = TASKS.get(task_id)
    if not task or index < 1 or index > len(task.variant_paths):
        raise HTTPException(status_code=404, detail="输出文件不存在。")
    path = Path(task.variant_paths[index - 1])
    if not path.exists():
        raise HTTPException(status_code=404, detail="输出文件已不存在。")
    return FileResponse(path, media_type="video/mp4", filename=path.name)

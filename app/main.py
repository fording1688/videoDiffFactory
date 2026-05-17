from __future__ import annotations

import shutil
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import BatchUploadResponse, TaskState, UploadResponse, VariantOptions, VariantTask
from .video_utils import app_root, asset_root, check_runtime, safe_stem
from .visual_variant import merge_videos, render_variant


APP_ROOT = app_root()
ASSET_ROOT = asset_root()
DATA_DIR = APP_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
STATIC_DIR = ASSET_ROOT / "static"
for directory in (UPLOAD_DIR, OUTPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

TASKS: dict[str, VariantTask] = {}

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


def _set(task: VariantTask, *, status: TaskState | None = None, progress: int | None = None, message: str | None = None) -> None:
    if status is not None:
        task.status = status
    if progress is not None:
        task.progress = progress
    if message is not None:
        task.message = message
    TASKS[task.task_id] = task


def _process(task_id: str) -> None:
    task = TASKS[task_id]
    try:
        runtime = check_runtime()
        if not runtime.get("ok"):
            raise RuntimeError(str(runtime.get("error") or "FFmpeg runtime missing"))
        _set(task, status=TaskState.processing, progress=8, message="正在准备视频素材")
        input_video = task.input_path
        if task.source_paths and len(task.source_paths) > 1:
            _set(task, progress=22, message=f"正在按上传顺序合并 {len(task.source_paths)} 个视频")
            input_video = str(merge_videos(input_paths=task.source_paths, work_dir=OUTPUT_DIR, task_id=task.task_id))
            task.input_path = input_video
        _set(task, progress=42, message="正在生成随机视觉参数并处理合并后的视频")
        output_path, effects, info = render_variant(
            input_video=input_video,
            output_dir=OUTPUT_DIR,
            task_id=task.task_id,
            options=task.options,
            output_stem=safe_stem(task.original_filename),
        )
        task.video_info = info
        task.effects = effects
        task.output_path = str(output_path)
        task.download_url = f"/api/download/{task.task_id}"
        _set(task, status=TaskState.completed, progress=100, message="处理完成，可以下载新视频")
    except Exception as exc:
        task.error = str(exc)
        task.effects["traceback"] = traceback.format_exc(limit=6)
        _set(task, status=TaskState.failed, progress=100, message="处理失败")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "runtime": check_runtime(), "data_dir": str(DATA_DIR)}


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
    )
    task = VariantTask(task_id=task_id, original_filename=file.filename, input_path=str(input_path), options=options)
    TASKS[task_id] = task
    threading.Thread(target=_process, args=(task_id,), daemon=True).start()
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
) -> BatchUploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个视频文件。")

    task_id = uuid.uuid4().hex[:12]
    source_paths: list[str] = []
    source_filenames: list[str] = []
    for index, file in enumerate(files, start=1):
        if not file.filename:
            continue
        suffix = Path(file.filename).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".webm"}:
            raise HTTPException(status_code=400, detail=f"{file.filename} 格式不支持。")
        source_filenames.append(file.filename)
        input_path = UPLOAD_DIR / f"{task_id}_{index:03d}_{safe_stem(file.filename)}{suffix}"
        with input_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        source_paths.append(str(input_path))

    if not source_paths:
        raise HTTPException(status_code=400, detail="没有读取到有效视频文件。")

    options = VariantOptions(
        intensity=intensity,
        effect_background=effect_background,
        effect_zoom=effect_zoom,
        effect_color=effect_color,
        effect_texture=effect_texture,
        effect_speed=effect_speed,
        effect_vignette=effect_vignette,
    )
    if len(source_filenames) == 1:
        original_filename = source_filenames[0]
    else:
        first_name = safe_stem(source_filenames[0])
        original_filename = f"{first_name}_merged_{len(source_filenames)}_clips.mp4"
    task = VariantTask(
        task_id=task_id,
        original_filename=original_filename,
        input_path=source_paths[0],
        source_paths=source_paths,
        source_filenames=source_filenames,
        options=options,
    )
    TASKS[task_id] = task
    threading.Thread(target=_process, args=(task_id,), daemon=True).start()
    return BatchUploadResponse(tasks=[UploadResponse(task_id=task_id, status_url=f"/api/tasks/{task_id}")])


@app.get("/api/tasks")
def list_tasks() -> dict[str, Any]:
    return {"ok": True, "tasks": [_dump(task) for task in TASKS.values()]}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return _dump(task)


@app.get("/api/download/{task_id}")
def download(task_id: str) -> FileResponse:
    task = TASKS.get(task_id)
    if not task or not task.output_path:
        raise HTTPException(status_code=404, detail="输出文件不存在。")
    path = Path(task.output_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="输出文件已不存在。")
    return FileResponse(path, media_type="video/mp4", filename=path.name)

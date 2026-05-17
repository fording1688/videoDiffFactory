from __future__ import annotations

from enum import Enum
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskState(str, Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class VideoInfo(BaseModel):
    duration: float = 0
    width: int = 0
    height: int = 0
    fps: float = 0
    has_audio: bool = False
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None


class VariantOptions(BaseModel):
    intensity: str = "balanced"
    effect_background: bool = True
    effect_zoom: bool = True
    effect_color: bool = True
    effect_texture: bool = True
    effect_speed: bool = True
    effect_vignette: bool = True


class VariantTask(BaseModel):
    task_id: str
    status: TaskState = TaskState.queued
    progress: int = 0
    message: str = "等待处理"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    elapsed_seconds: float = 0
    estimated_total_seconds: Optional[float] = None
    remaining_seconds: Optional[float] = None
    original_filename: str = ""
    input_path: str = ""
    source_paths: list[str] = Field(default_factory=list)
    source_filenames: list[str] = Field(default_factory=list)
    batch_id: str = ""
    worker_count: int = 3
    output_count: int = 1
    variant_paths: list[str] = Field(default_factory=list)
    variant_download_urls: list[str] = Field(default_factory=list)
    output_path: Optional[str] = None
    download_url: Optional[str] = None
    options: VariantOptions = Field(default_factory=VariantOptions)
    video_info: Optional[VideoInfo] = None
    effects: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class UploadResponse(BaseModel):
    ok: bool = True
    task_id: str
    status_url: str


class BatchUploadResponse(BaseModel):
    ok: bool = True
    tasks: list[UploadResponse]

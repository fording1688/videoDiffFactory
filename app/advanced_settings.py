from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


CONFIG_FILENAME = "advanced_pipeline.json"
RESOURCE_KEYS = (
    "watermark_path",
    "pip_path",
    "ambient_path",
    "bgm_path",
)


def config_path(data_dir: Path) -> Path:
    """配置位于项目外的用户数据目录，源码升级不会覆盖。"""
    return data_dir / CONFIG_FILENAME


def load_advanced_settings(data_dir: Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "watermark_path": "",
        "pip_path": "",
        "ambient_path": "",
        "bgm_path": "",
        "project_name": "VideoVariantStudio",
        "project_version": "0.5.2",
    }
    path = config_path(data_dir)
    if not path.is_file():
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return defaults
    if not isinstance(payload, dict):
        return defaults
    for key in defaults:
        if key in payload:
            defaults[key] = str(payload[key] or "").strip()
    return defaults


def save_advanced_settings(data_dir: Path, values: Mapping[str, Any]) -> dict[str, Any]:
    current = load_advanced_settings(data_dir)
    for key in current:
        if key in values:
            current[key] = str(values[key] or "").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = config_path(data_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return current

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


CHUNK_SIZE = 4 * 1024 * 1024
OAUTH_AUTHORIZE_URL = "https://openapi.baidu.com/oauth/2.0/authorize"
OAUTH_TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
FILE_API_URL = "https://pan.baidu.com/rest/2.0/xpan/file"
UPLOAD_API_URL = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
_REMOTE_GROUP_LOCK = threading.Lock()
_REMOTE_GROUP_DIRS: dict[str, str] = {}


class BaiduPanError(RuntimeError):
    pass


def config_path(data_dir: Path) -> Path:
    return data_dir / "baidu_pan.json"


def load_config(data_dir: Path) -> dict[str, Any]:
    path = config_path(data_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data_dir: Path, values: dict[str, Any]) -> dict[str, Any]:
    current = load_config(data_dir)
    current.update(values)
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return current


def public_status(config: dict[str, Any]) -> dict[str, Any]:
    expires_at = float(config.get("expires_at") or 0)
    return {
        "configured": bool(config.get("app_key") and config.get("secret_key")),
        "authorized": bool(config.get("refresh_token") or (config.get("access_token") and expires_at > time.time())),
        "enabled": bool(config.get("enabled")),
        "app_key": str(config.get("app_key") or ""),
        "redirect_uri": str(config.get("redirect_uri") or "oob"),
        "remote_dir": str(config.get("remote_dir") or ""),
        "expires_at": expires_at,
    }


def authorization_url(config: dict[str, Any]) -> str:
    app_key = str(config.get("app_key") or "").strip()
    if not app_key:
        raise BaiduPanError("请先保存百度网盘 App Key。")
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": app_key,
            "redirect_uri": str(config.get("redirect_uri") or "oob"),
            "scope": "basic,netdisk",
            "display": "popup",
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{query}"


def _json_request(url: str, data: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BaiduPanError(f"百度网盘接口请求失败：HTTP {exc.code} {detail[-500:]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BaiduPanError(f"无法连接百度网盘：{exc}") from exc
    if payload.get("error"):
        raise BaiduPanError(str(payload.get("error_description") or payload.get("error")))
    errno = payload.get("errno")
    if errno not in (None, 0):
        raise BaiduPanError(f"百度网盘返回错误 errno={errno}：{payload}")
    return payload


def exchange_code(data_dir: Path, code: str) -> dict[str, Any]:
    config = load_config(data_dir)
    payload = _json_request(
        OAUTH_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": str(config.get("app_key") or ""),
            "client_secret": str(config.get("secret_key") or ""),
            "redirect_uri": str(config.get("redirect_uri") or "oob"),
        },
    )
    return save_config(
        data_dir,
        {
            "access_token": payload.get("access_token", ""),
            "refresh_token": payload.get("refresh_token", ""),
            "expires_at": time.time() + int(payload.get("expires_in") or 2_592_000) - 300,
        },
    )


def access_token(data_dir: Path) -> str:
    config = load_config(data_dir)
    token = str(config.get("access_token") or "")
    if token and float(config.get("expires_at") or 0) > time.time():
        return token
    refresh_token = str(config.get("refresh_token") or "")
    if not refresh_token:
        raise BaiduPanError("百度网盘尚未授权，请先完成 OAuth 授权。")
    payload = _json_request(
        OAUTH_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": str(config.get("app_key") or ""),
            "client_secret": str(config.get("secret_key") or ""),
        },
    )
    save_config(
        data_dir,
        {
            "access_token": payload.get("access_token", ""),
            "refresh_token": payload.get("refresh_token") or refresh_token,
            "expires_at": time.time() + int(payload.get("expires_in") or 2_592_000) - 300,
        },
    )
    return str(payload.get("access_token") or "")


def normalize_remote_dir(value: str) -> str:
    path = "/" + "/".join(part for part in value.replace("\\", "/").split("/") if part)
    if not path.startswith("/apps/") or len(path.split("/")) < 3:
        raise BaiduPanError("个人百度网盘目标目录必须位于 /apps/应用名称/ 下。")
    return path.rstrip("/")


def _file_api(token: str, method: str, data: dict[str, Any]) -> dict[str, Any]:
    return _json_request(f"{FILE_API_URL}?method={method}&access_token={urllib.parse.quote(token)}", data, timeout=120)


def ensure_remote_dir(token: str, remote_dir: str) -> None:
    parts = [part for part in normalize_remote_dir(remote_dir).split("/") if part]
    # Baidu creates /apps/<application name> for the authorized application.
    # Only create descendants; attempting to create /apps itself is rejected.
    current = "/" + "/".join(parts[:2])
    for part in parts[2:]:
        current += "/" + part
        try:
            _file_api(token, "create", {"path": current, "isdir": 1, "rtype": 0})
        except BaiduPanError as exc:
            # errno -8 means the directory already exists.
            if "errno=-8" not in str(exc):
                raise


def reserve_remote_subdir(data_dir: Path, base_dir: str, folder_name: str, group_key: str) -> str:
    """Atomically reserve one remote child directory for a local output group."""
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", folder_name.strip()).strip(" .") or "output"
    cache_key = group_key.strip() or safe_name
    with _REMOTE_GROUP_LOCK:
        existing = _REMOTE_GROUP_DIRS.get(cache_key)
        if existing:
            return existing
        token = access_token(data_dir)
        normalized_base = normalize_remote_dir(base_dir)
        ensure_remote_dir(token, normalized_base)
        for number in range(1, 10_000):
            suffix = "" if number == 1 else f"_{number}"
            candidate = f"{normalized_base}/{safe_name}{suffix}"
            try:
                _file_api(token, "create", {"path": candidate, "isdir": 1, "rtype": 0})
                _REMOTE_GROUP_DIRS[cache_key] = candidate
                return candidate
            except BaiduPanError as exc:
                if "errno=-8" not in str(exc):
                    raise
        raise BaiduPanError(f"无法为输出组创建不重复的百度网盘目录：{safe_name}")


def _block_md5s(path: Path) -> list[str]:
    blocks: list[str] = []
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            blocks.append(hashlib.md5(chunk).hexdigest())
    return blocks or [hashlib.md5(b"").hexdigest()]


def upload_file(
    data_dir: Path,
    local_path: Path,
    remote_dir: str,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    if not local_path.is_file():
        raise BaiduPanError(f"待上传文件不存在：{local_path}")
    token = access_token(data_dir)
    remote_dir = normalize_remote_dir(remote_dir)
    ensure_remote_dir(token, remote_dir)
    remote_path = f"{remote_dir}/{local_path.name}"
    size = local_path.stat().st_size
    blocks = _block_md5s(local_path)
    if len(blocks) > 1000:
        raise BaiduPanError(f"文件超过当前 4 MiB × 1000 分片限制：{local_path.name}")
    block_json = json.dumps(blocks, separators=(",", ":"))
    pre = _file_api(
        token,
        "precreate",
        {"path": remote_path, "size": size, "isdir": 0, "autoinit": 1, "rtype": 3, "block_list": block_json},
    )
    upload_id = str(pre.get("uploadid") or "")
    return_type = int(pre.get("return_type") or 0)
    if return_type != 2:
        with local_path.open("rb") as handle:
            for index, _ in enumerate(blocks):
                chunk = handle.read(CHUNK_SIZE)
                query = urllib.parse.urlencode(
                    {"method": "upload", "type": "tmpfile", "access_token": token, "path": remote_path, "uploadid": upload_id, "partseq": index}
                )
                request = urllib.request.Request(f"{UPLOAD_API_URL}?{query}", data=chunk, method="POST")
                request.add_header("Content-Type", "application/octet-stream")
                try:
                    with urllib.request.urlopen(request, timeout=300) as response:
                        part = json.loads(response.read().decode("utf-8", errors="replace"))
                except Exception as exc:
                    raise BaiduPanError(f"上传分片 {index + 1}/{len(blocks)} 失败：{exc}") from exc
                if part.get("md5") and str(part["md5"]).lower() != blocks[index]:
                    raise BaiduPanError(f"上传分片校验失败：{local_path.name} 第 {index + 1} 片")
                if progress:
                    progress(min(size, (index + 1) * CHUNK_SIZE), size)
    created = _file_api(
        token,
        "create",
        {"path": remote_path, "size": size, "isdir": 0, "rtype": 3, "uploadid": upload_id, "block_list": block_json},
    )
    if progress:
        progress(size, size)
    return {"local_path": str(local_path), "remote_path": remote_path, "fs_id": created.get("fs_id"), "size": size}

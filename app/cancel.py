from __future__ import annotations

import subprocess
import threading


class CancelledTask(RuntimeError):
    pass


_LOCK = threading.RLock()
_CANCEL_EVENTS: dict[str, threading.Event] = {}


def cancel_event(task_id: str) -> threading.Event:
    with _LOCK:
        event = _CANCEL_EVENTS.get(task_id)
        if event is None:
            event = threading.Event()
            _CANCEL_EVENTS[task_id] = event
        return event


def request_cancel(task_id: str) -> None:
    cancel_event(task_id).set()


def clear_cancel(task_id: str) -> None:
    with _LOCK:
        _CANCEL_EVENTS.pop(task_id, None)


def is_cancel_requested(task_id: str | None) -> bool:
    if not task_id:
        return False
    with _LOCK:
        event = _CANCEL_EVENTS.get(task_id)
    return bool(event and event.is_set())


def run_cancellable(command: list[str], *, task_id: str | None = None) -> subprocess.CompletedProcess[str]:
    if not task_id:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    if is_cancel_requested(task_id):
        raise CancelledTask("任务已取消。")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    while True:
        if is_cancel_requested(task_id):
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            raise CancelledTask((stderr or stdout or "任务已取消。").strip() or "任务已取消。")
        try:
            # communicate() drains stdout/stderr while FFmpeg is running.
            # Polling without reading can fill the Windows pipe buffer and
            # deadlock long encodes, leaving the destination at 0 KB.
            stdout, stderr = process.communicate(timeout=0.2)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue

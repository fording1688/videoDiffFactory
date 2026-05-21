from __future__ import annotations

import subprocess
import threading
import time


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
        return subprocess.run(command, capture_output=True, text=True, check=False)
    if is_cancel_requested(task_id):
        raise CancelledTask("任务已取消。")

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while True:
        if is_cancel_requested(task_id):
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            raise CancelledTask((stderr or stdout or "任务已取消。").strip() or "任务已取消。")
        code = process.poll()
        if code is not None:
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(command, code, stdout, stderr)
        time.sleep(0.2)

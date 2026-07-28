from __future__ import annotations

import os
import socket
import threading
import time
import webbrowser

import uvicorn

from app.main import app


def _available_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def _exit_when_parent_closes(parent_pid: int) -> None:
    """Do not leave a detached worker behind after its terminal is closed."""
    while True:
        time.sleep(1.0)
        if os.getppid() != parent_pid:
            os._exit(0)


def main() -> None:
    parent_pid = os.getppid()
    if parent_pid > 1:
        threading.Thread(
            target=_exit_when_parent_closes,
            args=(parent_pid,),
            name="terminal-parent-watchdog",
            daemon=True,
        ).start()
    preferred_port = int(os.getenv("VIDEO_VARIANT_PORT", "8120"))
    port = _available_port(preferred_port)
    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Video Variant Studio running at {url}")
    # The browser polls task state while large batches are running. Per-request
    # access logs can produce millions of lines and eventually block the local
    # server when stdout is not actively consumed.
    uvicorn.run(app, host="127.0.0.1", port=port, reload=False, access_log=False)


if __name__ == "__main__":
    main()

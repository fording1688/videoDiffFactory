from __future__ import annotations

import os
import socket
import threading
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


def main() -> None:
    preferred_port = int(os.getenv("VIDEO_VARIANT_PORT", "8120"))
    port = _available_port(preferred_port)
    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Video Variant Studio running at {url}")
    uvicorn.run(app, host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()

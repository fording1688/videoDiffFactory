from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn


def main() -> None:
    port = int(os.getenv("VIDEO_VARIANT_PORT", "8120"))
    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Video Variant Studio running at {url}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()

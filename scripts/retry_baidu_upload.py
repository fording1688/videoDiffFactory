from __future__ import annotations

import argparse
import time
from pathlib import Path

from app.baidu_pan import list_directory, upload_file
from app.main import DATA_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume a Video Variant Studio Baidu upload.")
    parser.add_argument("local_dir", type=Path)
    parser.add_argument("remote_dir")
    args = parser.parse_args()

    local_dir = args.local_dir.expanduser().resolve()
    if not local_dir.is_dir():
        raise SystemExit(f"Local directory does not exist: {local_dir}")

    remote_entries = {
        str(item.get("server_filename") or ""): int(item.get("size") or 0)
        for item in list_directory(DATA_DIR, args.remote_dir)
        if not int(item.get("isdir") or 0)
    }
    files = sorted(
        path for path in local_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".txt"}
    )
    if not files:
        raise SystemExit(f"No output files found in: {local_dir}")

    for index, path in enumerate(files, start=1):
        if remote_entries.get(path.name) == path.stat().st_size:
            print(f"[{index}/{len(files)}] skip existing: {path.name}", flush=True)
            continue
        for attempt in range(1, 6):
            try:
                print(f"[{index}/{len(files)}] upload: {path.name} (attempt {attempt}/5)", flush=True)
                upload_file(DATA_DIR, path, args.remote_dir)
                break
            except Exception as exc:
                if attempt >= 5:
                    raise
                delay = min(30, 2 ** attempt)
                print(f"  failed: {exc}; retry in {delay}s", flush=True)
                time.sleep(delay)
    print("Baidu upload completed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

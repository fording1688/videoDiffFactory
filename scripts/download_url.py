from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.downloader import download_video_url
from app.video_utils import app_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download a video from a shared URL with yt-dlp.")
    parser.add_argument("url", help="YouTube/Facebook/Instagram/TikTok/etc. shared video URL")
    parser.add_argument("--output-dir", default=None, help="Target directory. Defaults to data/uploads.")
    parser.add_argument("--cookies-browser", default=None, help="Browser name for logged-in sites, for example chrome.")
    parser.add_argument("--proxy", default=None, help="Optional proxy URL passed to yt-dlp.")
    parser.add_argument("--playlist", action="store_true", help="Allow profile, channel, playlist, or page URLs.")
    parser.add_argument("--max-downloads", type=int, default=30, help="Maximum videos for playlist/page URLs.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else app_root() / "data" / "uploads"
    task_id = uuid.uuid4().hex[:12]
    output_paths, info = download_video_url(
        url=args.url,
        output_dir=output_dir,
        task_id=task_id,
        cookies_browser=args.cookies_browser,
        proxy=args.proxy,
        allow_playlist=args.playlist,
        max_downloads=args.max_downloads,
    )
    print(json.dumps({
        "ok": True,
        "task_id": task_id,
        "filepaths": [str(path) for path in output_paths],
        "filenames": [path.name for path in output_paths],
        "title": info.get("title") or "",
        "duration": info.get("duration"),
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "webpage_url": info.get("webpage_url") or args.url,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

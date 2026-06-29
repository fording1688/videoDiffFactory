from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.cookies import extract_cookies_from_browser

from .video_utils import safe_stem


class DownloadError(RuntimeError):
    pass


def _facebook_collection_candidates(url: str) -> list[str]:
    parsed = urllib.parse.urlparse(url)
    if "facebook.com" not in parsed.netloc:
        return [url]
    query = urllib.parse.parse_qs(parsed.query)
    profile_id = (query.get("id") or [""])[0]
    candidates = [url]
    if profile_id:
        candidates.extend([
            f"https://www.facebook.com/profile.php?id={profile_id}&sk=reels_tab",
            f"https://m.facebook.com/profile.php?id={profile_id}&sk=reels_tab",
            f"https://mbasic.facebook.com/profile.php?id={profile_id}&sk=reels_tab",
            f"https://www.facebook.com/profile.php?id={profile_id}&sk=videos",
            f"https://m.facebook.com/profile.php?id={profile_id}&v=videos",
            f"https://mbasic.facebook.com/profile.php?id={profile_id}&v=videos",
        ])
    if parsed.path.strip("/"):
        handle = parsed.path.strip("/").split("/")[0]
        if handle not in {"profile.php", "watch", "reel", "videos"}:
            candidates.extend([
                f"https://www.facebook.com/{handle}/reels",
                f"https://www.facebook.com/{handle}/videos",
                f"https://m.facebook.com/{handle}/videos",
                f"https://mbasic.facebook.com/{handle}/videos",
            ])
    return list(dict.fromkeys(candidates))


def _is_facebook_collection_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if "facebook.com" not in parsed.netloc:
        return False
    query = urllib.parse.parse_qs(parsed.query)
    path = parsed.path.strip("/")
    sk = (query.get("sk") or [""])[0]
    if path == "profile.php":
        return True
    if sk in {"reels_tab", "videos"}:
        return True
    return path.endswith("/reels") or path.endswith("/videos") or path in {"reels", "videos"}


def _download_facebook_collection(
    *,
    url: str,
    output_dir: Path,
    task_id: str,
    cookies_browser: str | None,
    proxy: str | None,
    max_downloads: int | None,
) -> tuple[list[Path], dict[str, Any]]:
    limit = max(1, min(int(max_downloads or 30), 200))
    video_urls = _extract_facebook_video_urls(url, proxy, cookies_browser, limit)
    if not video_urls:
        raise DownloadError(
            "没有从这个 Facebook 主页里解析到公开视频链接。请确认页面公开视频可见，"
            "或选择浏览器 cookies 后重试；如果仍失败，请先复制具体 Reel/视频链接。"
        )

    all_paths: list[Path] = []
    entries: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, video_url in enumerate(video_urls, start=1):
        try:
            paths, entry_info = download_video_url(
                url=video_url,
                output_dir=output_dir,
                task_id=f"{task_id}_{index:03d}",
                cookies_browser=cookies_browser,
                proxy=proxy,
                allow_playlist=False,
            )
            all_paths.extend(paths)
            entries.append(entry_info)
        except DownloadError as exc:
            failures.append(f"{video_url}: {exc}")

    if not all_paths:
        detail = failures[-1] if failures else "没有可下载的视频。"
        raise DownloadError(f"已解析到 {len(video_urls)} 个 Facebook 视频链接，但下载失败：{detail}")

    return all_paths, {
        "title": f"Facebook batch: {url}",
        "extractor_key": "Facebook",
        "webpage_url": url,
        "entries": entries,
        "failures": failures,
        "local_filenames": [path.name for path in all_paths],
    }


def _cookie_header(cookies_browser: str | None, url: str) -> str:
    if not cookies_browser:
        return ""
    try:
        cookie_jar = extract_cookies_from_browser(cookies_browser.strip())
    except Exception:
        return ""
    request = urllib.request.Request(url)
    cookie_jar.add_cookie_header(request)
    return request.get_header("Cookie") or ""


def _open_url(url: str, proxy: str | None, cookies_browser: str | None) -> str:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    cookie_header = _cookie_header(cookies_browser, url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="ignore")


def _extract_facebook_video_urls(url: str, proxy: str | None, cookies_browser: str | None, limit: int) -> list[str]:
    found: list[str] = []
    patterns = [
        re.compile(r"/reel/([0-9]{8,})"),
        re.compile(r"watch/\?v=([0-9]{8,})"),
        re.compile(r"/[^\"'<>\\s]+/videos/([0-9]{8,})"),
        re.compile(r"story_fbid=([0-9]{8,})"),
        re.compile(r'"videoID":"([0-9]{8,})"'),
        re.compile(r'"video_id":"([0-9]{8,})"'),
    ]
    errors: list[str] = []
    for candidate in _facebook_collection_candidates(url):
        try:
            body = html.unescape(_open_url(candidate, proxy, cookies_browser)).replace("\\/", "/")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        for pattern in patterns:
            for match in pattern.finditer(body):
                video_id = match.group(1)
                video_url = f"https://www.facebook.com/reel/{video_id}"
                if video_url not in found:
                    found.append(video_url)
                    if len(found) >= limit:
                        return found
    if not found and errors:
        raise DownloadError("主页解析失败，无法打开 Facebook 页面：" + errors[-1])
    return found


def _downloaded_filepaths(info: dict[str, Any], collected: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for path in collected:
        if path.exists() and path not in paths:
            paths.append(path)

    requested = info.get("requested_downloads") or []
    for item in requested:
        filepath = item.get("filepath") or item.get("_filename")
        if filepath and Path(filepath).exists():
            path = Path(filepath).resolve()
            if path not in paths:
                paths.append(path)

    filepath = info.get("filepath") or info.get("_filename")
    if filepath and Path(filepath).exists():
        path = Path(filepath).resolve()
        if path not in paths:
            paths.append(path)

    for entry in info.get("entries") or []:
        if isinstance(entry, dict):
            paths.extend(_downloaded_filepaths(entry, []))

    if paths:
        return paths

    raise DownloadError("下载完成，但没有拿到输出文件路径。")


def download_video_url(
    *,
    url: str,
    output_dir: Path,
    task_id: str,
    cookies_browser: str | None = None,
    proxy: str | None = None,
    allow_playlist: bool = False,
    max_downloads: int | None = None,
) -> tuple[list[Path], dict[str, Any]]:
    cleaned_url = (url or "").strip()
    if not cleaned_url:
        raise DownloadError("请输入视频分享链接。")

    output_dir.mkdir(parents=True, exist_ok=True)
    if allow_playlist and _is_facebook_collection_url(cleaned_url):
        return _download_facebook_collection(
            url=cleaned_url,
            output_dir=output_dir,
            task_id=task_id,
            cookies_browser=cookies_browser,
            proxy=proxy,
            max_downloads=max_downloads,
        )

    outtmpl = str(output_dir / f"{task_id}_%(extractor_key)s_%(id)s_%(title).80B.%(ext)s")
    collected_paths: list[Path] = []

    def collect_finished(download: dict[str, Any]) -> None:
        if download.get("status") != "finished":
            return
        filename = download.get("filename")
        if filename:
            collected_paths.append(Path(filename).resolve())

    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "noplaylist": not allow_playlist,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [collect_finished],
    }
    if allow_playlist and max_downloads:
        ydl_opts["playlistend"] = max(1, min(int(max_downloads), 200))
    if cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_browser.strip(),)
    if proxy:
        ydl_opts["proxy"] = proxy.strip()

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(cleaned_url, download=True)
    except Exception as exc:
        if allow_playlist and "facebook.com" in cleaned_url and "Unsupported URL" in str(exc):
            return _download_facebook_collection(
                url=cleaned_url,
                output_dir=output_dir,
                task_id=task_id,
                cookies_browser=cookies_browser,
                proxy=proxy,
                max_downloads=max_downloads,
            )
        raise DownloadError(str(exc)) from exc

    if not isinstance(info, dict):
        raise DownloadError("下载失败：yt-dlp 没有返回视频信息。")

    output_paths = _downloaded_filepaths(info, collected_paths)
    missing = [path for path in output_paths if not path.exists()]
    if missing:
        raise DownloadError(f"下载失败：找不到输出文件 {missing[0]}")

    title = str(info.get("title") or output_paths[0].stem)
    info["local_filename"] = output_paths[0].name
    info["local_filenames"] = [path.name for path in output_paths]
    info["safe_title"] = safe_stem(title)
    return output_paths, info

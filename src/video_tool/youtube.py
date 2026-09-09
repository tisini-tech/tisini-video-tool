"""Shared YouTube/yt-dlp operations.

All runnable Video Tool versions use this module so download behaviour stays
consistent. UI code is intentionally absent; callers receive ordinary
Python values/exceptions and decide how to present them.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from .core import extract_video_id, project_path

ProgressHook = Callable[[dict], None]


@dataclass
class DownloadOptions:
    """Options shared by the Basic, Pro, and compiler download paths."""

    output_dir: str
    format_type: str = "MP4"
    quality: str = "Best"
    cookie_browser: str | None = None
    cookie_file: str | None = None
    bypass_no_auth: bool = False
    progress_hook: ProgressHook | None = None
    remote_components: list[str] | None = None


def quality_filter(quality: str) -> str:
    """Return the yt-dlp height selector used by the original versions."""
    return {
        "1080p": "[height<=1080]",
        "720p": "[height<=720]",
        "480p": "[height<=480]",
        "360p": "[height<=360]",
    }.get(quality, "")


def build_ydl_options(options: DownloadOptions) -> dict:
    """Build yt-dlp options without adding version-specific behaviour."""
    output_dir = Path(options.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    height_filter = quality_filter(options.quality)

    ydl_options = {
        "outtmpl": str(output_dir / "%(id)s_%(title)s.%(ext)s"),
        "retries": 10,
        "fragment_retries": 10,
        "ignoreerrors": False,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
    }

    if options.progress_hook:
        ydl_options["progress_hooks"] = [options.progress_hook]

    if options.remote_components is not None:
        ydl_options["remote_components"] = options.remote_components

    has_auth = False

    # A local Netscape cookies.txt file is the preferred non-browser
    # authentication mechanism. This lets Video Tool work without a live
    # browser session or browser-cookie extraction.
    cookie_file = options.cookie_file
    if not cookie_file:
        # Normal Video Tool authentication: place a Netscape-format
        # youtube_cookies.txt in the project root. This works without a
        # browser session and is preferred over browser-cookie extraction.
        candidate = project_path("youtube_cookies.txt")
        if os.path.isfile(candidate):
            cookie_file = candidate

    if cookie_file:
        cookie_path = Path(cookie_file).expanduser().resolve()
        if not cookie_path.is_file():
            raise FileNotFoundError(f"YouTube cookie file not found: {cookie_path}")
        if cookie_path.stat().st_size == 0:
            raise ValueError(f"YouTube cookie file is empty: {cookie_path}")

        # yt-dlp expects a Netscape-format cookies.txt file here. Do a small
        # sanity check before starting a network request so an invalid export
        # fails clearly instead of looking like a YouTube authentication failure.
        with cookie_path.open("r", encoding="utf-8", errors="replace") as handle:
            first_nonempty = next((line.strip() for line in handle if line.strip()), "")
        if not (first_nonempty.startswith("# Netscape HTTP Cookie File")
                or first_nonempty.startswith("# HTTP Cookie File")):
            raise ValueError(
                "youtube_cookies.txt is not in Netscape cookies.txt format. "
                "Export the cookies as a Netscape-format cookies.txt file."
            )

        ydl_options["cookiefile"] = str(cookie_path)
        has_auth = True
    elif options.cookie_browser and options.cookie_browser.lower() != "none":
        ydl_options["cookiesfrombrowser"] = (options.cookie_browser.lower(),)
        has_auth = True

    if options.bypass_no_auth and not has_auth:
        ydl_options["extractor_args"] = {
            "youtube": {
                "player_client": "android",
                "player_skip": "webpage,configs,js",
            }
        }

    if options.format_type.upper() == "MP3":
        ydl_options["format"] = "bestaudio/best"
        ydl_options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif options.quality == "Best":
        ydl_options["format"] = f"bv*+ba/best{height_filter}"
        ydl_options["merge_output_format"] = "mp4"
    else:
        ydl_options["format"] = (
            f"bestvideo{height_filter}+bestaudio{height_filter}/best{height_filter}"
        )
        ydl_options["merge_output_format"] = "mp4"

    return ydl_options


def find_downloaded_file(folder: str, format_type: str) -> str | None:
    """Return the newest likely output file in a download directory."""
    try:
        files = [
            (str(path), path.stat().st_mtime)
            for path in Path(folder).iterdir()
            if path.is_file()
        ]
    except OSError:
        return None

    if not files:
        return None

    files.sort(key=lambda item: item[1], reverse=True)
    expected_extensions = (
        {".mp3"} if format_type.upper() == "MP3"
        else {".mp4", ".mkv", ".webm"}
    )

    for path, _ in files:
        if Path(path).suffix.lower() in expected_extensions:
            if "_trim_" not in Path(path).name:
                return path

    return files[0][0]


def video_info(url: str, cookie_browser: str | None = None) -> dict:
    """Fetch metadata without downloading a video."""
    options = {"quiet": True, "no_warnings": True}
    if cookie_browser and cookie_browser.lower() != "none":
        options["cookiesfrombrowser"] = (cookie_browser.lower(),)

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def download_url(
    url: str,
    options: DownloadOptions,
    *,
    download: bool = True,
) -> str | None:
    """Download one URL and return the resulting file path."""
    ydl_options = build_ydl_options(options)

    # Be tolerant of CSV/input data that already contains a full URL.
    # Never turn https://... into https://https://...
    if url.startswith("https://https://"):
        url = url[len("https://"):]
    elif url.startswith("http://http://"):
        url = url[len("http://"):]

    with yt_dlp.YoutubeDL(ydl_options) as ydl:
        info = ydl.extract_info(url, download=download)

        if not download:
            return None

        filename = ydl.prepare_filename(info)

    # yt-dlp may change the extension during post-processing.
    if options.format_type.upper() == "MP3":
        mp3_path = str(Path(filename).with_suffix(".mp3"))
        if os.path.exists(mp3_path):
            return mp3_path

    if os.path.exists(filename):
        return filename

    return find_downloaded_file(options.output_dir, options.format_type)


def download_many(
    urls: list[str],
    options: DownloadOptions,
    *,
    progress_hook: ProgressHook | None = None,
) -> list[str]:
    """Download multiple URLs sequentially, preserving version behaviour."""
    results: list[str] = []

    if progress_hook:
        options.progress_hook = progress_hook

    for url in urls:
        path = download_url(url, options)
        if path:
            results.append(path)

    return results


def video_id(url: str) -> str | None:
    """Shared alias used by callers that need the YouTube cache key."""
    return extract_video_id(url)
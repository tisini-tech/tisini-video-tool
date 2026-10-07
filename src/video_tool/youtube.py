
"""Shared YouTube/yt-dlp operations.

All runnable Video Tool versions use this module so download behaviour stays
consistent. UI code is intentionally absent; callers receive ordinary
Python values/exceptions and decide how to present them.

Range downloads
---------------

Set ``section_start`` and/or ``section_end`` on ``DownloadOptions`` to fetch
only that part of a video. yt-dlp hands the job to one ffmpeg process, which
reads only those seconds from YouTube's servers and re-encodes them, so the
rest of the video never reaches the disk.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from .core import (
    MissingToolError,
    VideoToolError,
    extract_video_id,
    find_ffmpeg,
    project_path,
    trim_label,
)
from .errors import explain_download_error

logger = logging.getLogger(__name__)

ProgressHook = Callable[[dict], None]


# ---------------------------------------------------------------------------
# Range-download settings
# ---------------------------------------------------------------------------

# Placed before each -i. If a connection to YouTube drops part-way,
# ffmpeg asks again from the byte where it stopped instead of ending the
# file early.
RECONNECT_ARGS = [
    "-reconnect",
    "1",
    "-reconnect_streamed",
    "1",
    "-reconnect_delay_max",
    "5",
]

# Prefer up to 1080p, then higher frame rate, then h264 video and AAC audio.
# h264 decodes far faster than AV1, which yt-dlp otherwise may prefer.
RANGE_FORMAT_SORT = [
    "res:1080",
    "fps",
    "vcodec:h264",
    "acodec:aac",
]


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
    ffmpeg_location: str | None = None

    # Range download. Leave both as None to fetch the whole video.
    section_start: float | None = None
    section_end: float | None = None

    # Encoder settings, used for range downloads only.
    range_preset: str = "veryfast"
    range_crf: int = 20
    range_fps: int | None = None


def has_section(options: DownloadOptions) -> bool:
    """Return True when the options ask for a time range."""

    return (
        options.section_start is not None
        or options.section_end is not None
    )


def quality_filter(quality: str) -> str:
    """Return the yt-dlp video height selector."""

    return {
        "1080p": "[height<=1080]",
        "720p": "[height<=720]",
        "480p": "[height<=480]",
        "360p": "[height<=360]",
    }.get(quality, "")


def _ffmpeg_available(options: DownloadOptions) -> bool:
    """Return True when yt-dlp will be able to find ffmpeg."""

    if options.ffmpeg_location:
        return Path(options.ffmpeg_location).expanduser().exists()

    return find_ffmpeg() is not None


def _section_bounds(
    options: DownloadOptions,
) -> tuple[float, float]:
    """Return checked start/end seconds.

    An omitted end time is represented internally as infinity.
    """

    start = options.section_start or 0.0
    end = options.section_end

    if start < 0:
        raise ValueError("Start time cannot be negative")

    if end is not None and end <= start:
        raise ValueError(
            "End time must be greater than start time"
        )

    return start, float("inf") if end is None else end


def _apply_section_options(
    ydl_options: dict,
    options: DownloadOptions,
    output_dir: Path,
) -> None:
    """Configure yt-dlp to download only a requested time range."""

    if not _ffmpeg_available(options):
        raise MissingToolError(
            "ffmpeg is needed to download a time range, but it was not found. "
            "Install ffmpeg and make sure it is on your PATH."
        )

    start, end = _section_bounds(options)

    ydl_options["download_ranges"] = download_range_func(
        None,
        [(start, end)],
    )

    # Re-encode so the cut lands on the requested time rather than the
    # nearest keyframe.
    ydl_options["force_keyframes_at_cuts"] = True

    # yt-dlp forwards these to ffmpeg:
    # ffmpeg_i -> before every input
    # ffmpeg_o -> after inputs, before the output file
    ffmpeg_args: dict[str, list[str]] = {
        "ffmpeg_i": list(RECONNECT_ARGS),
    }

    if options.format_type.upper() != "MP3":
        ydl_options["format_sort"] = list(RANGE_FORMAT_SORT)

        output_args = [
            "-c:v",
            "libx264",
            "-preset",
            options.range_preset,
            "-crf",
            str(options.range_crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]

        if options.range_fps:
            output_args += [
                "-vf",
                f"fps={options.range_fps}",
            ]

        ffmpeg_args["ffmpeg_o"] = output_args

    ydl_options["external_downloader_args"] = ffmpeg_args

    # "_trim_" in the name from the start means later scans of the folder
    # cannot mistake this short clip for a full downloaded source.
    label = trim_label(
        options.section_start,
        options.section_end,
    )

    # The YouTube ID is deliberately NOT included in the user-visible
    # filename. Cache identity is handled separately.
    ydl_options["outtmpl"] = str(
        output_dir / f"%(title)s_trim_{label}.%(ext)s"
    )


def _cookie_file(options: DownloadOptions) -> str | None:
    """Resolve the cookie file configured for YouTube access."""

    cookie_file = options.cookie_file

    if not cookie_file:
        candidate = project_path("youtube_cookies.txt")

        if os.path.isfile(candidate):
            cookie_file = candidate

    if not cookie_file:
        return None

    cookie_path = Path(cookie_file).expanduser().resolve()

    if not cookie_path.is_file():
        raise FileNotFoundError(
            f"YouTube cookie file not found: {cookie_path}"
        )

    if cookie_path.stat().st_size == 0:
        raise ValueError(
            f"YouTube cookie file is empty: {cookie_path}"
        )

    # yt-dlp expects a Netscape-format cookies.txt file.
    with cookie_path.open(
        "r",
        encoding="utf-8",
        errors="replace",
    ) as handle:
        first_nonempty = next(
            (
                line.strip()
                for line in handle
                if line.strip()
            ),
            "",
        )

    if not (
        first_nonempty.startswith("# Netscape HTTP Cookie File")
        or first_nonempty.startswith("# HTTP Cookie File")
    ):
        raise ValueError(
            "youtube_cookies.txt is not in Netscape cookies.txt format. "
            "Export the cookies as a Netscape-format cookies.txt file."
        )

    return str(cookie_path)


def _apply_auth_options(
    ydl_options: dict,
    options: DownloadOptions,
) -> None:
    """Apply YouTube authentication settings to yt-dlp options."""

    cookie_file = _cookie_file(options)

    if cookie_file:
        ydl_options["cookiefile"] = cookie_file
        has_auth = True

    elif (
        options.cookie_browser
        and options.cookie_browser.lower() != "none"
    ):
        ydl_options["cookiesfrombrowser"] = (
            options.cookie_browser.lower(),
        )
        has_auth = True

    else:
        has_auth = False

    if options.bypass_no_auth and not has_auth:
        ydl_options["extractor_args"] = {
            "youtube": {
                "player_client": "android",
                "player_skip": "webpage,configs,js",
            }
        }


def build_ydl_options(
    options: DownloadOptions,
) -> dict:
    """Build yt-dlp options without adding version-specific behaviour."""

    output_dir = Path(options.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    height_filter = quality_filter(options.quality)

    # Keep filenames user-friendly.
    #
    # IMPORTANT:
    # The YouTube ID is intentionally not part of the filename.
    # Cache identity is handled separately by core.py using:
    #
    #     {video_id}_{format_type}
    #
    # Users therefore see the actual YouTube title while the application
    # retains stable cache behaviour.
    ydl_options = {
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "retries": 10,
        "fragment_retries": 10,
        "ignoreerrors": False,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
    }

    if options.progress_hook:
        ydl_options["progress_hooks"] = [
            options.progress_hook
        ]

    if options.remote_components is not None:
        ydl_options["remote_components"] = options.remote_components

    # Only set when explicitly supplied, for example by the GUI's bundled
    # binary. Otherwise yt-dlp continues using ffmpeg from PATH.
    if options.ffmpeg_location:
        ydl_options["ffmpeg_location"] = (
            options.ffmpeg_location
        )

    _apply_auth_options(
        ydl_options,
        options,
    )

    if options.format_type.upper() == "MP3":
        ydl_options["format"] = "bestaudio/best"
        ydl_options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    elif options.quality == "Best":
        ydl_options["format"] = "bv*+ba/best"
        ydl_options["merge_output_format"] = "mp4"

    else:
        # Apply the resolution restriction to the video stream only.
        # Audio does not have a meaningful video height.
        ydl_options["format"] = (
            f"bestvideo{height_filter}+"
            f"bestaudio/"
            f"best{height_filter}"
        )
        ydl_options["merge_output_format"] = "mp4"

    # Range settings come last so their filename and format behaviour
    # override the normal full-download configuration.
    if has_section(options):
        _apply_section_options(
            ydl_options,
            options,
            output_dir,
        )

    return ydl_options


def find_downloaded_file(
    folder: str,
    format_type: str,
) -> str | None:
    """Return the newest likely media output in a download directory."""

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

    files.sort(
        key=lambda item: item[1],
        reverse=True,
    )

    expected_extensions = (
        {".mp3"}
        if format_type.upper() == "MP3"
        else {".mp4", ".mkv", ".webm"}
    )

    for path, _ in files:
        filename = Path(path).name

        if (
            Path(path).suffix.lower() in expected_extensions
            and "_trim_" not in filename
        ):
            return path

    # Do not return an arbitrary file such as .part, .json or .txt.
    return None


def video_info(
    url: str,
    cookie_browser: str | None = None,
    cookie_file: str | None = None,
) -> dict:
    """Fetch YouTube metadata without downloading the video."""

    options = {
        "quiet": True,
        "no_warnings": True,
    }

    temp_options = DownloadOptions(
        output_dir=".",
        cookie_browser=cookie_browser,
        cookie_file=cookie_file,
    )

    resolved_cookie_file = _cookie_file(temp_options)

    if resolved_cookie_file:
        options["cookiefile"] = resolved_cookie_file

    elif (
        cookie_browser
        and cookie_browser.lower() != "none"
    ):
        options["cookiesfrombrowser"] = (
            cookie_browser.lower(),
        )

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(
            _normalise_url(url),
            download=False,
        )


def _normalise_url(url: str) -> str:
    """Be tolerant of input containing a duplicated URL scheme.

    For example:

        https://https://youtube.com/watch?v=...

    becomes:

        https://youtube.com/watch?v=...
    """

    if url.startswith("https://https://"):
        return url[len("https://"):]

    if url.startswith("http://http://"):
        return url[len("http://"):]

    return url


def _final_path(
    ydl: yt_dlp.YoutubeDL,
    info: dict,
) -> str:
    """Return the path yt-dlp wrote after merging/post-processing."""

    finished = info.get("requested_downloads") or []

    if finished and finished[0].get("filepath"):
        return finished[0]["filepath"]

    return ydl.prepare_filename(info)


def download_url(
    url: str,
    options: DownloadOptions,
    *,
    download: bool = True,
) -> str | None:
    """Download one URL and return the resulting file path.

    For a range download the returned path always exists, or an error is
    raised.

    yt-dlp ``DownloadError`` exceptions are not wrapped. Callers can use
    ``describe_download_error()`` to turn one into user-facing advice.
    """

    url = _normalise_url(url)

    ydl_options = build_ydl_options(options)

    with yt_dlp.YoutubeDL(ydl_options) as ydl:
        info = ydl.extract_info(
            url,
            download=download,
        )

        if not download:
            return None

        if info is None:
            raise VideoToolError(
                "YouTube returned no information for this link."
            )

        filename = _final_path(
            ydl,
            info,
        )

    if has_section(options):
        # Never guess here: a wrong guess could be an unrelated video.
        if os.path.exists(filename):
            return filename

        raise VideoToolError(
            "The range download finished but the file is missing: "
            f"{filename}"
        )

    # yt-dlp may change the extension during post-processing.
    if options.format_type.upper() == "MP3":
        mp3_path = str(
            Path(filename).with_suffix(".mp3")
        )

        if os.path.exists(mp3_path):
            return mp3_path

    if os.path.exists(filename):
        return filename

    return find_downloaded_file(
        options.output_dir,
        options.format_type,
    )


def download_many(
    urls: list[str],
    options: DownloadOptions,
    *,
    progress_hook: ProgressHook | None = None,
) -> list[str]:
    """Download multiple URLs sequentially."""

    results: list[str] = []

    # Avoid mutating the caller's DownloadOptions object.
    if progress_hook is not None:
        options = replace(
            options,
            progress_hook=progress_hook,
        )

    for url in urls:
        path = download_url(
            url,
            options,
        )

        if path:
            results.append(path)

    return results


def video_id(url: str) -> str | None:
    """Return the YouTube ID used by the cache system."""

    return extract_video_id(url)


# ---------------------------------------------------------------------------
# Error messages for users
# ---------------------------------------------------------------------------


def describe_download_error(
    exc: BaseException,
) -> str:
    """Return a short user-facing message from a yt-dlp error.

    The wording comes from :func:`video_tool.errors.explain_download_error`,
    so every front end says the same thing. Errors it does not know keep
    yt-dlp's own text, cleaned of colour codes.
    """

    info = explain_download_error(exc)

    if info.code == "download_failed":
        return info.detail or "The download failed."

    return " ".join(
        part
        for part in (info.message, info.hint, info.cli_hint)
        if part
    )

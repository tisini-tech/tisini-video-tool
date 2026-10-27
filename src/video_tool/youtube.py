
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
import re
import subprocess
import sys
import threading
import time
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
from .procutil import child_process_kwargs

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
    js_runtime_path: str | None = None
    # Runtime-managed standalone executable used by the GUI.
    yt_dlp_executable: str | None = None

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

    # An explicitly selected browser should be used instead of silently
    # preferring a project-local cookie file. Explicit cookie_file still wins.
    if not cookie_file and not options.cookie_browser:
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

    # Use the app-managed Deno executable when provided.
    if options.js_runtime_path:
        ydl_options["js_runtimes"] = {
            "deno": {
                "path": options.js_runtime_path,
            }
        }

    # Same default as the standalone path below: fetch the EJS solver from
    # npm unless the caller chose its own list.
    if options.remote_components is None:
        ydl_options["remote_components"] = ["ejs:npm"]

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


class ExecutableLaunchError(MissingToolError):
    """The standalone yt-dlp program could not run at all.

    This points at the program or the computer (for example antivirus
    blocking it), not at the video. ``download_url`` answers it by using the
    yt-dlp that is built into the app instead.
    """


# Lines the standalone yt-dlp prints for us to read. The prefixes keep them
# apart from the file path it prints when the download ends.
_PROGRESS_PREFIX = "VTPROG|"
_STREAMS_PREFIX = "VTSTREAMS|"
_PROGRESS_TEMPLATE = (
    "download:"
    + _PROGRESS_PREFIX
    + "%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.fragment_index)s|"
    "%(progress.fragment_count)s"
)
# Prints the format ids about to download, such as "137, 140" for a video
# stream plus an audio stream. Empty when there is only one stream.
_STREAMS_PRINT = (
    "before_dl:" + _STREAMS_PREFIX + "%(requested_formats.:.format_id|)l"
)

# Text the PyInstaller start-up code prints when yt-dlp.exe cannot unpack
# itself, for example "[PYI-21604:ERROR] Failed to extract python310.dll".
_LAUNCH_FAILURE_MARKERS = (
    "[PYI-",
    "Failed to extract",
    "Failed to load Python",
)

_ANSI_CODES = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_LAST_LOG_PATH = Path.home() / ".video-tool-gui" / "last_download.log"


def _number(value: object) -> float | None:
    """Return a non-negative number, or None for "NA", blanks and junk."""

    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

    # NaN is the only value that is not equal to itself.
    if number != number or number < 0:
        return None

    return number


class ProgressTracker:
    """Turn yt-dlp progress reports into one 0 to 1 value per download.

    A video and its audio usually download as two streams, one after the
    other, and each report only covers the stream in progress. The tracker
    spreads the bar across all streams, so it moves forward once.

    It takes the dictionaries yt-dlp gives a progress hook. The standalone
    path in this module sends dictionaries of the same shape.
    """

    def __init__(self) -> None:
        self._stage = 0
        self._last_done = 0.0
        self._shown = 0.0

    def fraction(self, report: dict) -> float | None:
        """Return the overall progress, or None if the report has none."""

        if report.get("status") != "downloading":
            return None

        done = _number(report.get("downloaded_bytes"))

        if done is None:
            return None

        # The byte count falls back to zero when the next stream starts.
        if done < self._last_done:
            self._stage += 1

        self._last_done = done

        total = _number(report.get("total_bytes")) or _number(
            report.get("total_bytes_estimate")
        )

        if total:
            part = done / total
        else:
            index = _number(report.get("fragment_index"))
            count = _number(report.get("fragment_count"))

            if index is None or not count:
                return None

            part = index / count

        part = min(max(part, 0.0), 1.0)

        info = report.get("info_dict") or {}
        streams = max(1, len(info.get("requested_formats") or []))
        # Seeing a later stage proves there are at least that many streams.
        streams = max(streams, self._stage + 1)
        stage = min(self._stage, streams - 1)

        overall = (stage + part) / streams

        # Never move backwards, whatever the reports say.
        self._shown = max(self._shown, overall)

        return self._shown


def _child_output_encoding() -> str:
    """Return the text encoding yt-dlp uses when its output goes to a pipe.

    On Windows that is the system code page (cp1252 on most English
    systems), not UTF-8. Reading it as UTF-8 turns an apostrophe into a
    broken character and garbles file names that have accents.
    """

    if sys.platform == "win32":
        try:
            import ctypes

            return f"cp{ctypes.windll.kernel32.GetACP()}"  # type: ignore[attr-defined]
        except Exception:
            return "utf-8"

    return "utf-8"


def _clean_ytdlp_output(text: str, returncode: int) -> str:
    """Boil yt-dlp's output down to the lines that explain the failure.

    yt-dlp prints many warnings before the real error. Showing all of them
    buries the cause, so keep the ERROR lines, or the last few lines when
    there are none.
    """

    text = _ANSI_CODES.sub("", text or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    errors = [line for line in lines if line.startswith("ERROR:")]

    if errors:
        chosen = errors
    else:
        chosen = [
            line for line in lines if not line.startswith("WARNING:")
        ][-5:]

    if not chosen:
        return (
            f"yt-dlp stopped with exit code {returncode} and gave no "
            "details. An antivirus program may be blocking it."
        )

    return "\n".join(chosen)[-2000:]


def _is_launch_failure(
    stderr_text: str,
    stdout_lines: list[str],
    returncode: int,
) -> bool:
    """Return True when yt-dlp itself failed to start or was cut off."""

    if returncode == 0:
        return False

    if any(marker in stderr_text for marker in _LAUNCH_FAILURE_MARKERS):
        return True

    # Failed with nothing at all to say: killed, not refused.
    return not stderr_text.strip() and not stdout_lines


def _write_last_log(
    args: list[str],
    returncode: int,
    stdout_lines: list[str],
    stderr_text: str,
) -> None:
    """Keep yt-dlp's full output of the last failure for support."""

    try:
        _LAST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LAST_LOG_PATH.write_text(
            "command: " + " ".join(args) + "\n"
            f"exit code: {returncode}\n\n"
            "--- stdout ---\n" + "\n".join(stdout_lines) + "\n\n"
            "--- stderr ---\n" + stderr_text,
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Could not write %s", _LAST_LOG_PATH, exc_info=True)


def _forward_progress(
    line: str,
    streams: int,
    hook: ProgressHook | None,
) -> None:
    """Pass one progress line to the caller's hook, shaped like yt-dlp's."""

    if hook is None:
        return

    parts = line[len(_PROGRESS_PREFIX):].split("|")
    parts += ["NA"] * (5 - len(parts))
    done, total, estimate, index, count = parts[:5]

    try:
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": _number(done),
                "total_bytes": _number(total),
                "total_bytes_estimate": _number(estimate),
                "fragment_index": _number(index),
                "fragment_count": _number(count),
                "info_dict": {"requested_formats": [{}] * streams},
            }
        )
    except Exception:
        # A faulty hook must never stop the download.
        logger.debug("Progress hook failed", exc_info=True)


def _run_ytdlp(
    args: list[str],
    options: DownloadOptions,
) -> tuple[int, list[str], str]:
    """Run the standalone yt-dlp once.

    Return the exit code, the lines it printed to standard output (progress
    lines removed), and everything it printed to standard error. Progress
    reaches ``options.progress_hook`` while the program runs.
    """

    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=_child_output_encoding(),
            errors="replace",
            **child_process_kwargs(),
        )
    except OSError as exc:
        raise ExecutableLaunchError(
            f"Could not start yt-dlp at {args[0]}: {exc}"
        ) from exc

    # Read standard error on its own thread. If nobody empties it, a long
    # error message can fill the pipe and freeze yt-dlp.
    stderr_parts: list[str] = []

    def drain_stderr() -> None:
        if process.stderr is not None:
            for text_line in process.stderr:
                stderr_parts.append(text_line)

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()

    stdout_lines: list[str] = []
    streams = 1

    if process.stdout is not None:
        for raw_line in process.stdout:
            line = raw_line.strip()

            if not line:
                continue

            if line.startswith(_STREAMS_PREFIX):
                ids = line[len(_STREAMS_PREFIX):].split(",")
                streams = max(1, len([item for item in ids if item.strip()]))
            elif line.startswith(_PROGRESS_PREFIX):
                _forward_progress(line, streams, options.progress_hook)
            else:
                stdout_lines.append(line)

    returncode = process.wait()
    reader.join(timeout=5)

    return returncode, stdout_lines, "".join(stderr_parts)


def _download_url_with_executable(
    url: str,
    options: DownloadOptions,
) -> str:
    """Run the GUI-managed standalone yt-dlp for full-video downloads.

    This lets the GUI use its runtime-updated yt-dlp executable rather than
    only updating a standalone binary while downloads use the bundled module.

    Progress goes to ``options.progress_hook`` as it happens. If the program
    cannot run at all, ``ExecutableLaunchError`` is raised after a few tries.
    """
    executable = Path(options.yt_dlp_executable or "").expanduser()
    if not executable.is_file():
        raise MissingToolError(
            f"The configured yt-dlp executable was not found: {executable}"
        )

    output_dir = Path(options.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "%(title)s.%(ext)s")

    args = [
        str(executable),
        "--no-playlist",
        "--retries", "10",
        "--fragment-retries", "10",
        "--output", output_template,
        "--print", "after_move:filepath",
        "--print", _STREAMS_PRINT,
        # One progress line per update, in a form this module can read.
        "--newline",
        "--progress",
        "--progress-template", _PROGRESS_TEMPLATE,
    ]

    cookie_file = _cookie_file(options)
    if cookie_file:
        args.extend(["--cookies", cookie_file])
    elif options.cookie_browser and options.cookie_browser.lower() != "none":
        args.extend(["--cookies-from-browser", options.cookie_browser.lower()])

    # The standalone official yt-dlp executable bundles EJS solver scripts;
    # allow remote npm EJS components as an automatically refreshed fallback.
    components = options.remote_components or ["ejs:npm"]
    for component in components:
        args.extend(["--remote-components", component])

    if options.ffmpeg_location:
        args.extend(["--ffmpeg-location", options.ffmpeg_location])
    if options.js_runtime_path:
        args.extend([
            "--js-runtimes",
            f"deno:{Path(options.js_runtime_path).expanduser()}",
        ])

    if options.format_type.upper() == "MP3":
        args.extend([
            "--format", "bestaudio/best",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
        ])
    elif options.quality == "Best":
        args.extend([
            "--format", "bv*+ba/best",
            "--merge-output-format", "mp4",
        ])
    else:
        height_filter = quality_filter(options.quality)
        args.extend([
            "--format",
            f"bestvideo{height_filter}+bestaudio/best{height_filter}",
            "--merge-output-format", "mp4",
        ])

    args.append(url)

    # yt-dlp.exe unpacks itself on every start. That sometimes fails for a
    # moment (antivirus scanning, a busy disk), so try again before giving up.
    attempts = 3
    for attempt in range(1, attempts + 1):
        returncode, stdout_lines, stderr_text = _run_ytdlp(args, options)

        if returncode == 0:
            break

        launch_failure = _is_launch_failure(
            stderr_text, stdout_lines, returncode
        )

        if launch_failure and attempt < attempts:
            logger.warning(
                "yt-dlp did not start (attempt %s of %s); trying again",
                attempt,
                attempts,
            )
            time.sleep(2 * attempt)
            continue

        _write_last_log(args, returncode, stdout_lines, stderr_text)

        details = _clean_ytdlp_output(
            stderr_text or "\n".join(stdout_lines), returncode
        )

        if launch_failure:
            raise ExecutableLaunchError(
                f"yt-dlp could not start.\n{details}"
            )

        raise VideoToolError(
            f"yt-dlp could not download this video.\n{details}"
        )

    # --print after_move:filepath reports the final post-processed file path.
    for line in reversed(stdout_lines):
        candidate_text = line.strip()
        if not candidate_text:
            continue
        candidate = Path(candidate_text).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())

    fallback = find_downloaded_file(str(output_dir), options.format_type)
    if fallback and Path(fallback).is_file():
        return str(Path(fallback).resolve())

    raise VideoToolError(
        "yt-dlp finished without reporting a saved media file. "
        "Check the output folder and try again."
    )


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

    # Use the runtime-updated executable for GUI full downloads. Range-download
    # and other callers retain the established Python yt-dlp API pipeline.
    if download and options.yt_dlp_executable and not has_section(options):
        try:
            return _download_url_with_executable(url, options)
        except ExecutableLaunchError as exc:
            # The program would not even start. Try the yt-dlp built into
            # this app rather than failing for a reason the user cannot fix.
            logger.warning(
                "The standalone yt-dlp would not run (%s). "
                "Using the built-in yt-dlp instead.",
                exc,
            )

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

"""
🛠 Video Tool Core Utilities — HIGH QUALITY VERSION

Shared utilities for Video Tool apps.

Quality improvements:

• trim_clip: tries lossless copy first, falls back to high-quality re-encode
• trim_remote: uses the same shared trim engine for remote streams
• download_remote: downloads remote streams with automatic re-encode fallback
• merge_clips: uses concat demuxer with -c copy for lossless merging
• Proper CRF/preset settings when re-encode is unavoidable
"""
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .errors import (
    MissingToolError,
    VideoToolError,
    ffmpeg_failure,
)

logger = logging.getLogger(__name__)

# === RUNTIME PATHS ===

# Keep generated state outside the source tree. This avoids polluting the
# project with cache/config files while giving every Video Tool version one
# consistent location for shared state.

VIDEO_TOOL_CONFIG_DIR = Path.home() / ".config" / "video-tool"
VIDEO_TOOL_CACHE_DIR = Path.home() / ".cache" / "video-tool"
CACHE_FILE = str(VIDEO_TOOL_CACHE_DIR / "cache.json")
CONFIG_FILE = str(VIDEO_TOOL_CONFIG_DIR / "config.json")
DOWNLOAD_COUNT_FILE = str(VIDEO_TOOL_CACHE_DIR / "download_count.json")


def _ensure_runtime_dirs() -> None:
    """Create Video Tool runtime directories when state needs to be written."""
    VIDEO_TOOL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_TOOL_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# === PATH UTILS ===

def project_path(*parts):
    """Return an absolute path relative to the repository root."""
    # core.py lives at: <project>/src/video_tool/core.py
    project_root = Path(__file__).resolve().parents[2]
    return str(project_root.joinpath(*parts))


def src_path(*parts):
    """Return an absolute path relative to the src/ directory."""
    src_root = Path(__file__).resolve().parents[1]
    return str(src_root.joinpath(*parts))


def root_path(*parts):
    """Alias for project_path, kept for compatibility."""
    return project_path(*parts)


def ensure_dir(path):
    """Ensure a directory exists and return it."""
    os.makedirs(path, exist_ok=True)
    return path


def safe_filename(name):
    """Make a string safe for use as a filename."""
    return re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_")


# === FFMPEG ===

def find_ffmpeg():
    """Find ffmpeg executable."""
    for name in ["ffmpeg", "ffmpeg.exe"]:
        path = shutil.which(name)
        if path:
            return path

    # Common locations
    for loc in [
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]:
        if os.path.exists(loc):
            return loc

    return None


def find_ffprobe():
    """Find ffprobe executable."""
    for name in ["ffprobe", "ffprobe.exe"]:
        path = shutil.which(name)
        if path:
            return path

    for loc in [
        "/usr/bin/ffprobe",
        "/usr/local/bin/ffprobe",
        "/opt/homebrew/bin/ffprobe",
        r"C:\ffmpeg\bin\ffprobe.exe",
    ]:
        if os.path.exists(loc):
            return loc

    return None


def get_available_encoders(ffmpeg_path):
    """Get list of available video encoders."""
    if not ffmpeg_path:
        return []

    try:
        result = subprocess.run(
            [ffmpeg_path, "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        encoders = []

        for line in result.stdout.split("\n"):
            if "libx264" in line:
                encoders.append("libx264")
            elif "libx265" in line:
                encoders.append("libx265")
            elif "h264_nvenc" in line:
                encoders.append("h264_nvenc")
            elif "h264_amf" in line:
                encoders.append("h264_amf")
            elif "h264_videotoolbox" in line:
                encoders.append("h264_videotoolbox")

        return encoders

    except Exception:
        return []


def get_best_video_encoder(ffmpeg_path):
    encoders = get_available_encoders(ffmpeg_path)
    priority = ["libx264", "libx265"]  # Software encoders support CRF

    for enc in priority:
        if enc in encoders:
            return enc

    # Fallback to hardware only if no software encoder found
    fallback = [
        "h264_nvenc",
        "h264_amf",
        "h264_videotoolbox",
    ]

    for enc in fallback:
        if enc in encoders:
            return enc

    return "libx264"


# === TIME PARSING ===

def parse_time_to_seconds(time_str):
    """Convert time string to seconds. Handles HH:MM:SS, MM:SS, SS, etc."""
    if not time_str or not time_str.strip():
        return None

    time_str = time_str.strip()

    # Try pure seconds
    try:
        return float(time_str)
    except ValueError:
        pass

    # Try HH:MM:SS or MM:SS
    parts = time_str.split(":")

    try:
        if len(parts) == 3:
            return (
                int(parts[0]) * 3600
                + int(parts[1]) * 60
                + float(parts[2])
            )
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 1:
            return float(parts[0])
    except ValueError:
        pass

    return None


def seconds_to_timestamp(seconds):
    """Convert seconds to HH:MM:SS timestamp."""
    if seconds is None:
        return "00:00:00"

    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"{m:02d}:{s:02d}"


def _label_part(seconds):
    """Format seconds as 8h32m40s or 1m30s."""
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)

    return (
        f"{h}h{m:02d}m{s:02d}s"
        if h
        else f"{m}m{s:02d}s"
    )


def trim_label(start, end):
    """Return a file-name label for a trim range.

    Examples:
        8h32m40s-8h53m20s
        1m30s-2m05s

    Colons are avoided because Windows does not allow them in filenames.
    """
    first = _label_part(start) if start else "start"
    last = _label_part(end) if end else "end"

    return f"{first}-{last}"


# === CACHE ===

def load_cache():
    """Load video cache."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return {}


def save_cache(cache):
    """Save video cache."""
    try:
        _ensure_runtime_dirs()

        # Write to a temp file first, so a failed write cannot leave a
        # half-written cache.json behind.
        temp_file = f"{CACHE_FILE}.tmp"

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)

        os.replace(temp_file, CACHE_FILE)

    except Exception:
        pass


def get_cached_path(cache, video_id, format_type="MP4"):
    """Get cached video path if it exists."""
    key = f"{video_id}_{format_type}"
    path = cache.get(key)

    if path and os.path.exists(path):
        return path

    return None


def add_to_cache(cache, video_id, format_type, path):
    """Add video to cache."""
    key = f"{video_id}_{format_type}"
    cache[key] = str(path)
    save_cache(cache)


def default_download_directories() -> list[str]:
    """Return the conventional Video Tool download locations.

    Linux filesystems are case-sensitive, so both historical spellings are
    checked. The user's explicit output directory is added separately by
    callers.
    """
    downloads = Path.home() / "Downloads"

    candidates = [
        downloads / "video-tool",
        downloads / "Video-Tool",
    ]

    existing: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        key = str(candidate.resolve(strict=False))

        if key not in seen:
            seen.add(key)
            existing.append(str(candidate))

    return existing


def find_local_cached_file(video_id, format_type="MP4", directories=None):
    """Find a previously downloaded file without contacting YouTube.

    The cache registry is checked first by ``resolve_cached_file``. This
    directory scan is a recovery path for files whose cache entry is missing.

    Only filenames containing the exact YouTube ID are accepted; unrelated
    videos are never guessed to be a match.
    """
    if not video_id:
        return None

    extensions = (
        {".mp3"}
        if format_type.upper() == "MP3"
        else {".mp4", ".mkv", ".webm"}
    )

    search_dirs = list(directories or [])

    for directory in default_download_directories():
        if directory not in search_dirs:
            search_dirs.append(directory)

    for directory in search_dirs:
        path = Path(directory).expanduser()

        if not path.is_dir():
            continue

        try:
            candidates = [
                p
                for p in path.iterdir()
                if (
                    p.is_file()
                    and p.suffix.lower() in extensions
                    and video_id in p.name
                    and "_trim_" not in p.name
                )
            ]
        except OSError:
            continue

        if candidates:
            return str(
                max(
                    candidates,
                    key=lambda p: p.stat().st_mtime,
                )
            )

    return None


def resolve_cached_file(
    url,
    format_type="MP4",
    *,
    cache=None,
    directories=None,
):
    """Resolve a local/cached download before any network operation."""
    video_id = (
        extract_video_id(url)
        if is_valid_youtube_url(url)
        else url
    )

    if not video_id:
        return None

    cache = cache if cache is not None else load_cache()

    cached = get_cached_path(
        cache,
        video_id,
        format_type,
    )

    if cached:
        return cached

    return find_local_cached_file(
        video_id,
        format_type,
        directories,
    )


# === CONFIG ===

def load_config():
    """Load app config."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return {}


def save_config(config):
    """Save app config."""
    try:
        _ensure_runtime_dirs()

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    except Exception:
        pass


# === DOWNLOAD COUNT ===

def load_download_count():
    """Load download counter."""
    if os.path.exists(DOWNLOAD_COUNT_FILE):
        try:
            with open(DOWNLOAD_COUNT_FILE, encoding="utf-8") as f:
                data = json.load(f)

                return data.get("count", 0)

        except Exception:
            pass

    return 0


def save_download_count(count):
    """Save download counter."""
    try:
        _ensure_runtime_dirs()

        with open(DOWNLOAD_COUNT_FILE, "w", encoding="utf-8") as f:
            json.dump({"count": count}, f)

    except Exception:
        pass


# === URL HELPERS ===

def is_valid_youtube_url(url):
    """Check if URL is a valid YouTube URL."""
    if not url:
        return False

    patterns = [
        r"(?:https?://)?(?:www\.)?youtube\.com/watch\?v=[\w-]+",
        r"(?:https?://)?(?:www\.)?youtu\.be/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/shorts/[\w-]+",

        # A stream that is live now, or that has ended and become a normal
        # video: YouTube keeps the /live/ link working for both.
        r"(?:https?://)?(?:www\.)?youtube\.com/live/[\w-]+",
    ]

    return any(re.search(p, url) for p in patterns)


def extract_video_id(url):
    """Extract YouTube video ID from URL."""
    if not url:
        return None

    # youtu.be/ID
    m = re.search(r"youtu\.be/([\w-]+)", url)

    if m:
        return m.group(1)

    # youtube.com/watch?v=ID
    m = re.search(r"[?&]v=([\w-]+)", url)

    if m:
        return m.group(1)

    # youtube.com/shorts/ID
    m = re.search(r"/shorts/([\w-]+)", url)

    if m:
        return m.group(1)

    # youtube.com/live/ID
    m = re.search(r"/live/([\w-]+)", url)

    if m:
        return m.group(1)

    return None


def _is_remote_input(input_path):
    """Return True when the input is an HTTP/HTTPS media URL."""
    if not input_path:
        return False

    try:
        scheme = urlparse(str(input_path)).scheme.lower()
    except Exception:
        return False

    return scheme in {"http", "https"}


# === VIDEO INFO ===

def get_video_info(ffmpeg_path, video_path):
    """Get video metadata using ffprobe."""
    ffprobe = find_ffprobe()

    if not ffprobe or not os.path.exists(video_path):
        return {}

    try:
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,codec_name,pix_fmt,duration",
            "-show_entries",
            "format=duration,bit_rate",
            "-of",
            "json",
            video_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )

        data = json.loads(result.stdout)

        # Normalize structure
        if "streams" not in data:
            data["streams"] = []

        if "format" not in data:
            data["format"] = {}

        return data

    except Exception:
        return {
            "streams": [],
            "format": {},
        }


def _to_seconds(value):
    """Turn an ffprobe value into a float, or None when it is missing."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe_durations(path):
    """Return the length in seconds of a file's video, audio and whole.

    Values are None when ffprobe is missing, fails, or cannot tell.
    """
    lengths = {
        "file": None,
        "video": None,
        "audio": None,
    }

    ffprobe = find_ffprobe()

    if not ffprobe:
        logger.warning(
            "ffprobe not found; cannot check clip length"
        )
        return lengths

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,duration:format=duration",
        "-of",
        "json",
        path,
    ]

    try:
        run = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        data = json.loads(run.stdout or "{}")

    except (
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        logger.warning(
            "ffprobe failed on %s: %s",
            path,
            exc,
        )
        return lengths

    lengths["file"] = _to_seconds(
        data.get("format", {}).get("duration")
    )

    for stream in data.get("streams", []):
        kind = stream.get("codec_type")

        if (
            kind in ("video", "audio")
            and lengths[kind] is None
        ):
            lengths[kind] = _to_seconds(
                stream.get("duration")
            )

    return lengths


def check_clip_length(
    path,
    wanted,
    tolerance=None,
    kinds=("video", "audio"),
):
    """Compare a clip's stream lengths with the requested length.

    Returns a list of plain-language problems. An empty list means the clip
    is long enough.
    """
    if tolerance is None:
        tolerance = max(
            2.0,
            0.005 * wanted,
        )

    lengths = probe_durations(path)
    problems = []

    for kind in kinds:
        seconds = (
            lengths[kind]
            if lengths[kind] is not None
            else lengths["file"]
        )

        if seconds is None:
            problems.append(
                f"could not read the {kind} length"
            )

        elif seconds < wanted - tolerance:
            problems.append(
                f"{kind} is {seconds:.1f}s long, "
                f"wanted {wanted:.1f}s"
            )

    return problems


# === HIGH QUALITY TRIM ===

def _encode_timeout(duration):
    """Seconds to let a re-encode run: 10 times clip length, at least 300."""
    return max(
        300.0,
        (duration or 0.0) * 10.0,
    )


def _run_encode(cmd, output_path, timeout):
    """Run an ffmpeg command; turn a timeout into a clear error."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    except subprocess.TimeoutExpired as exc:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)

        except OSError:
            logger.warning(
                "Could not remove partial file %s",
                output_path,
            )

        raise VideoToolError(
            f"The video took more than {timeout / 60:.0f} minutes to "
            "process, so Video Tool stopped it. Try a shorter clip."
        ) from exc


def trim_clip(
    ffmpeg_path,
    input_path,
    output_path,
    start_sec,
    end_sec,
    resolution="1080p",
    bitrate_kbps=8000,
    fps=30,
    crf=18,
    preset="slow",
    encoder=None,
    caption_filter=None,
    force_reencode=False,
    include_audio=True,
):
    """
    Trim a video clip with maximum quality preservation.

    ``input_path`` may be either:

        • a local filesystem path
        • an HTTP/HTTPS media URL

    Strategy:

    1. Try lossless copy first when allowed.
    2. If copy fails, use frame-accurate re-encode.
    3. If that fails, use the simpler high-quality re-encode path.

    ``force_reencode=True`` skips the lossless copy strategy.

    ``include_audio=False`` creates a video-only output.

    caption_filter:
        An optional drawtext filter string. Since a burned-in caption changes
        pixels, the lossless copy path is skipped when a caption is present.
    """

    if not ffmpeg_path:
        raise MissingToolError(
            "FFmpeg was not found."
        )

    is_remote = _is_remote_input(input_path)

    if not is_remote and not os.path.exists(input_path):
        raise FileNotFoundError(
            f"Input not found: {input_path}"
        )

    os.makedirs(
        os.path.dirname(output_path) or ".",
        exist_ok=True,
    )

    # Determine duration
    duration = None

    if start_sec is not None and end_sec is not None:
        duration = end_sec - start_sec

    elif end_sec is not None:
        duration = end_sec

    encode_timeout = _encode_timeout(duration)

    # ------------------------------------------------------------------
    # STRATEGY 1: LOSSLESS COPY
    # ------------------------------------------------------------------
    #
    # Skip when:
    #
    # • accurate mode explicitly requested
    # • a caption needs to be burned in
    #
    # Remote streams are allowed here. If the remote server does not support
    # the required seeking/range behavior, ffmpeg will fail and the normal
    # re-encode path below will take over.
    #
    if (
        duration
        and duration > 0
        and not caption_filter
        and not force_reencode
    ):
        copy_cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            str(start_sec),
            "-i",
            input_path,
            "-t",
            str(duration),
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-fflags",
            "+genpts",
        ]

        if not include_audio:
            copy_cmd.extend(["-an"])

        copy_cmd.append(output_path)

        try:
            result = subprocess.run(
                copy_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if (
                result.returncode == 0
                and os.path.exists(output_path)
                and os.path.getsize(output_path) > 1000
            ):
                # Verify the output is valid.
                info = get_video_info(
                    ffmpeg_path,
                    output_path,
                )

                out_duration = 0

                if info and "format" in info:
                    try:
                        out_duration = float(
                            info["format"].get(
                                "duration",
                                0,
                            )
                        )
                    except (
                        TypeError,
                        ValueError,
                    ):
                        out_duration = 0

                if (
                    out_duration > 0
                    or os.path.getsize(output_path) > 100000
                ):
                    print(
                        f"  ✅ Lossless trim: "
                        f"{os.path.basename(output_path)}"
                    )
                    return output_path

            if os.path.exists(output_path):
                os.remove(output_path)

        except Exception:
            if os.path.exists(output_path):
                os.remove(output_path)

    # ------------------------------------------------------------------
    # STRATEGY 2: FRAME-ACCURATE RE-ENCODE
    # ------------------------------------------------------------------

    # Build resolution filter
    res_filter = None

    if resolution == "1080p":
        res_filter = "scale=1920:-2:flags=lanczos"

    elif resolution == "720p":
        res_filter = "scale=1280:-2:flags=lanczos"

    elif resolution == "480p":
        res_filter = "scale=854:-2:flags=lanczos"

    elif resolution == "360p":
        res_filter = "scale=640:-2:flags=lanczos"

    # Build filter chain
    filters = []

    if res_filter:
        filters.append(res_filter)

    # Add fps filter only if explicitly supplied
    if fps:
        filters.append(f"fps={fps}")

    filters.append("format=yuv420p")

    if caption_filter:
        filters.append(caption_filter)

    vf = (
        ",".join(filters)
        if filters
        else "format=yuv420p"
    )

    enc = encoder or get_best_video_encoder(
        ffmpeg_path
    )

    # Two-stage seek:
    #
    # 1. Fast input-side seek lands near the target.
    # 2. Output-side seek handles the final few seconds precisely.
    #
    SEEK_BUFFER = 5.0

    cmd = [
        ffmpeg_path,
        "-y",
    ]

    if start_sec and start_sec > 0:
        coarse = max(
            0.0,
            start_sec - SEEK_BUFFER,
        )

        remainder = start_sec - coarse

        cmd.extend([
            "-ss",
            str(coarse),
        ])

        cmd.extend([
            "-i",
            input_path,
        ])

        if remainder > 0:
            cmd.extend([
                "-ss",
                str(remainder),
            ])

    else:
        cmd.extend([
            "-i",
            input_path,
        ])

    # Duration
    if duration and duration > 0:
        cmd.extend([
            "-t",
            str(duration),
        ])

    # Video encoding
    cmd.extend([
        "-c:v",
        enc,
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ])

    if enc == "libx264":
        cmd.extend([
            "-tune",
            "film",
            "-profile:v",
            "high",
            "-level",
            "4.2",
        ])

    # Audio
    if include_audio:
        cmd.extend([
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-ar",
            "48000",
            "-ac",
            "2",
        ])
    else:
        cmd.append("-an")

    # Video filter
    cmd.extend([
        "-vf",
        vf,
    ])

    cmd.append(output_path)

    print(
        f"  🎬 Frame-accurate trim "
        f"(CRF {crf}, preset {preset})..."
    )

    result = _run_encode(
        cmd,
        output_path,
        encode_timeout,
    )

    if (
        result.returncode == 0
        and os.path.exists(output_path)
        and os.path.getsize(output_path) > 1000
    ):
        return output_path

    if os.path.exists(output_path):
        os.remove(output_path)

    # ------------------------------------------------------------------
    # STRATEGY 3: HIGH-QUALITY RE-ENCODE FALLBACK
    # ------------------------------------------------------------------

    res_filter = None

    if resolution == "1080p":
        res_filter = "scale=1920:-2:flags=lanczos"

    elif resolution == "720p":
        res_filter = "scale=1280:-2:flags=lanczos"

    elif resolution == "480p":
        res_filter = "scale=854:-2:flags=lanczos"

    elif resolution == "360p":
        res_filter = "scale=640:-2:flags=lanczos"

    filters = []

    if res_filter:
        filters.append(res_filter)

    filters.append("format=yuv420p")

    if caption_filter:
        filters.append(caption_filter)

    vf = ",".join(filters) if filters else None

    enc = encoder or get_best_video_encoder(
        ffmpeg_path
    )

    cmd = [
        ffmpeg_path,
        "-y",
    ]

    # Input seek for faster processing
    if start_sec and start_sec > 0:
        cmd.extend([
            "-ss",
            str(start_sec),
        ])

    cmd.extend([
        "-i",
        input_path,
    ])

    # Output duration
    if duration and duration > 0:
        cmd.extend([
            "-t",
            str(duration),
        ])

    # Video encoding
    cmd.extend([
        "-c:v",
        enc,
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ])

    # Encoder-specific tuning
    if enc == "libx264":
        cmd.extend([
            "-tune",
            "film",
            "-profile:v",
            "high",
            "-level",
            "4.2",
        ])

    # Audio
    if include_audio:
        cmd.extend([
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-ar",
            "48000",
            "-ac",
            "2",
        ])
    else:
        cmd.append("-an")

    # Video filter
    if vf:
        cmd.extend([
            "-vf",
            vf,
        ])

    # Frame rate
    if fps:
        cmd.extend([
            "-r",
            str(fps),
        ])

    cmd.append(output_path)

    print(
        f"  🎬 Re-encoding trim "
        f"(CRF {crf}, preset {preset})..."
    )

    result = _run_encode(
        cmd,
        output_path,
        encode_timeout,
    )

    if result.returncode != 0:
        raise ffmpeg_failure(
            "cut the clip",
            result.stderr,
        )

    if (
        not os.path.exists(output_path)
        or os.path.getsize(output_path) < 1000
    ):
        raise VideoToolError(
            "The clip came out empty. Check that the start and end "
            "times fall inside the video."
        )

    return output_path


def trim_remote(
    ffmpeg_path,
    stream_url,
    output_path,
    start_sec,
    end_sec,
    resolution="1080p",
    bitrate_kbps=8000,
    fps=30,
    crf=18,
    preset="slow",
    encoder=None,
    caption_filter=None,
    include_audio=True,
    re_encode=False,
):
    """Trim a remote HTTP/HTTPS media stream.

    This is intentionally a thin wrapper around ``trim_clip``.

    The actual trimming engine remains centralized in ``trim_clip`` so local
    and remote processing use the same quality, seeking, encoder and fallback
    behavior.

    ``re_encode=True`` skips the stream-copy path and goes directly to the
    frame-accurate re-encode path.
    """
    if not stream_url:
        raise ValueError(
            "A remote stream URL is required."
        )

    if not _is_remote_input(stream_url):
        raise ValueError(
            "trim_remote requires an HTTP/HTTPS stream URL."
        )

    return trim_clip(
        ffmpeg_path,
        stream_url,
        output_path,
        start_sec,
        end_sec,
        resolution=resolution,
        bitrate_kbps=bitrate_kbps,
        fps=fps,
        crf=crf,
        preset=preset,
        encoder=encoder,
        caption_filter=caption_filter,
        force_reencode=re_encode,
        include_audio=include_audio,
    )


def download_remote(
    ffmpeg_path,
    stream_url,
    output_path,
    re_encode=False,
    crf=18,
    preset="slow",
    encoder=None,
    include_audio=True,
):
    """Download a complete remote HTTP/HTTPS media stream.

    Strategy:

    1. Try stream-copy first when ``re_encode`` is False.
    2. If stream-copy fails, automatically re-encode.
    3. If ``re_encode`` is True, skip copy and encode directly.

    The caller does not need to know whether the fallback was required.
    """

    if not ffmpeg_path:
        raise MissingToolError(
            "FFmpeg was not found."
        )

    if not stream_url:
        raise ValueError(
            "A remote stream URL is required."
        )

    if not _is_remote_input(stream_url):
        raise ValueError(
            "download_remote requires an HTTP/HTTPS stream URL."
        )

    os.makedirs(
        os.path.dirname(output_path) or ".",
        exist_ok=True,
    )

    # ------------------------------------------------------------------
    # STRATEGY 1: STREAM COPY
    # ------------------------------------------------------------------

    if not re_encode:
        copy_cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            stream_url,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
        ]

        if not include_audio:
            copy_cmd.append("-an")

        copy_cmd.append(output_path)

        try:
            result = subprocess.run(
                copy_cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )

            if (
                result.returncode == 0
                and os.path.exists(output_path)
                and os.path.getsize(output_path) > 1000
            ):
                print(
                    f"  ✅ Remote download: "
                    f"{os.path.basename(output_path)}"
                )
                return output_path

        except Exception:
            pass

        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # STRATEGY 2: HIGH-QUALITY RE-ENCODE
    # ------------------------------------------------------------------

    enc = encoder or get_best_video_encoder(
        ffmpeg_path
    )

    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        stream_url,
        "-map",
        "0:v:0",
    ]

    if include_audio:
        cmd.extend([
            "-map",
            "0:a?",
        ])
    else:
        cmd.append("-an")

    cmd.extend([
        "-c:v",
        enc,
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ])

    if enc == "libx264":
        cmd.extend([
            "-tune",
            "film",
            "-profile:v",
            "high",
            "-level",
            "4.2",
        ])

    if include_audio:
        cmd.extend([
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-ar",
            "48000",
            "-ac",
            "2",
        ])

    cmd.append(output_path)

    print(
        f"  🎬 Downloading remote stream "
        f"(CRF {crf}, preset {preset})..."
    )

    result = _run_encode(
        cmd,
        output_path,
        600,
    )

    if result.returncode != 0:
        raise ffmpeg_failure(
            "download the video section",
            result.stderr,
        )

    if (
        not os.path.exists(output_path)
        or os.path.getsize(output_path) < 1000
    ):
        raise VideoToolError(
            "The downloaded section came out empty. Check that the "
            "start and end times fall inside the video."
        )

    print(
        f"  ✅ Remote download complete: "
        f"{os.path.basename(output_path)}"
    )

    return output_path


# === HIGH QUALITY MERGE ===

def _probe_streams(path):
    """Return the settings of a file's first video and audio streams.

    The result is ``{"video": {...}, "audio": {...} or None}``. It is
    ``None`` when ffprobe is missing or cannot read the file.
    """
    ffprobe = find_ffprobe()

    if not ffprobe:
        return None

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,profile,width,height,pix_fmt,"
        "r_frame_rate,sample_aspect_ratio,sample_rate,channels",
        "-of",
        "json",
        str(path),
    ]

    try:
        run = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(run.stdout or "{}")
    except Exception:
        return None

    video = None
    audio = None

    for stream in data.get("streams", []):
        kind = stream.get("codec_type")

        if kind == "video" and video is None:
            video = {
                key: stream.get(key)
                for key in (
                    "codec_name",
                    "profile",
                    "width",
                    "height",
                    "pix_fmt",
                    "r_frame_rate",
                    "sample_aspect_ratio",
                )
            }

        elif kind == "audio" and audio is None:
            audio = {
                key: stream.get(key)
                for key in (
                    "codec_name",
                    "sample_rate",
                    "channels",
                )
            }

    if video is None:
        return None

    return {"video": video, "audio": audio}


def _can_copy_merge(streams):
    """True when every clip has the same stream settings.

    The concat demuxer with ``-c copy`` joins the files without looking
    at them. It only works when codec, size, frame rate and audio match.
    When ffprobe could not read a clip, the answer is False.
    """
    if not streams or any(item is None for item in streams):
        return False

    return all(item == streams[0] for item in streams[1:])


def _fps_from_ratio(text, default=30.0):
    """Turn an ffprobe frame rate such as ``30000/1001`` into a float."""
    try:
        top, _, bottom = str(text).partition("/")
        value = float(top) / float(bottom or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return default

    return value if value > 0 else default


def _merge_length_ok(clip_paths, output_path):
    """Check that a merged file is about as long as its parts added up.

    Returns True when the lengths cannot be read, so a missing ffprobe
    never blocks a merge.
    """
    expected = 0.0

    for clip in clip_paths:
        length = probe_durations(clip)["file"]

        if length is None:
            return True

        expected += length

    actual = probe_durations(output_path)["file"]

    if actual is None:
        return True

    return abs(actual - expected) <= max(1.0, expected * 0.03)


def merge_clips(
    ffmpeg_path,
    clip_paths,
    output_path,
    re_encode=False,
    crf=18,
    preset="slow",
    encoder=None,
):
    """
    Merge multiple clips into one video with zero quality loss when possible.

    Strategy:

    1. If every clip has the same codec, size, frame rate and audio,
       join them with the concat demuxer and ``-c copy``. Afterwards,
       check that the result is as long as the parts added up.
    2. Otherwise, or if that check fails, or if ``re_encode`` is True,
       decode every clip, bring them to one size, frame rate and audio
       format, and re-encode them into a single file.
    """
    if not ffmpeg_path:
        raise MissingToolError(
            "FFmpeg was not found. Install it and make sure it is on "
            "your PATH."
        )

    if not clip_paths:
        raise ValueError("No clips to merge")

    if len(clip_paths) == 1:
        shutil.copy2(
            clip_paths[0],
            output_path,
        )
        return output_path

    os.makedirs(
        os.path.dirname(output_path) or ".",
        exist_ok=True,
    )

    streams = [_probe_streams(clip) for clip in clip_paths]

    # --- STRATEGY 1: Lossless concat demuxer ---
    #
    # ffmpeg's concat demuxer does not compare the clips. With different
    # sizes, frame rates or sample rates it still exits with code 0 and
    # writes a file whose later parts are blank or broken. So the clips
    # are compared here first.

    if not re_encode and _can_copy_merge(streams):
        list_file = output_path + ".concat_list.txt"

        try:
            with open(
                list_file,
                "w",
                encoding="utf-8",
            ) as f:
                for clip in clip_paths:
                    abs_path = os.path.abspath(clip)

                    # Escape single quotes in path for concat file
                    abs_path = abs_path.replace(
                        "'",
                        "'\\''",
                    )

                    f.write(
                        f"file '{abs_path}'\n"
                    )

            cmd = [
                ffmpeg_path,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file,
                "-c",
                "copy",
                "-fflags",
                "+genpts",
                "-movflags",
                "+faststart",
                output_path,
            ]

            print(
                f"  🔗 Lossless merge "
                f"({len(clip_paths)} clips)..."
            )

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if (
                result.returncode == 0
                and os.path.exists(output_path)
                and os.path.getsize(output_path) > 1000
            ):
                if _merge_length_ok(clip_paths, output_path):
                    print(
                        "  ✅ Lossless merge complete"
                    )
                    return output_path

                print(
                    "  ⚠️ The joined file is not as long as "
                    "its parts, re-encoding instead"
                )

            else:
                print(
                    "  ⚠️ Concat copy failed, "
                    "falling back to re-encode"
                )

            if os.path.exists(output_path):
                os.remove(output_path)

        finally:
            if os.path.exists(list_file):
                os.remove(list_file)

    elif not re_encode:
        print(
            "  ⚠️ Clips differ in codec, size, frame rate or audio; "
            "a lossless join would break them"
        )

    # --- STRATEGY 2: High-quality re-encode merge ---

    print(
        f"  🎬 Re-encoding merge "
        f"({len(clip_paths)} clips)..."
    )

    enc = encoder or get_best_video_encoder(
        ffmpeg_path
    )

    # Every clip is scaled to the first clip's size and frame rate. The
    # concat filter refuses inputs that differ in size, and it needs the
    # same audio format on every input.

    reference = streams[0]["video"] if streams[0] else None

    width = height = fps = None

    if reference:
        try:
            width = int(reference["width"]) // 2 * 2
            height = int(reference["height"]) // 2 * 2
            fps = _fps_from_ratio(reference["r_frame_rate"])
        except (TypeError, ValueError):
            width = height = fps = None

    # When ffprobe could not read a clip, assume it has audio.
    has_audio = [
        item is None or item["audio"] is not None
        for item in streams
    ]
    any_audio = any(has_audio)

    inputs = []

    for clip in clip_paths:
        inputs.extend([
            "-i",
            clip,
        ])

    filter_parts = []
    joined = ""

    for i, clip in enumerate(clip_paths):
        if width and height and fps:
            filter_parts.append(
                f"[{i}:v:0]"
                f"scale={width}:{height}"
                ":force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1,fps={fps:.3f},format=yuv420p"
                f"[v{i}]"
            )
        else:
            filter_parts.append(
                f"[{i}:v:0]setsar=1[v{i}]"
            )

        joined += f"[v{i}]"

        if not any_audio:
            continue

        if has_audio[i]:
            filter_parts.append(
                f"[{i}:a:0]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo"
                f"[a{i}]"
            )
        else:
            # This clip has no sound; fill its length with silence.
            length = probe_durations(clip)
            seconds = length["video"] or length["file"] or 0.0

            filter_parts.append(
                "anullsrc=r=48000:cl=stereo,"
                f"atrim=duration={seconds:.3f},"
                f"asetpts=N/SR/TB[a{i}]"
            )

        joined += f"[a{i}]"

    filter_complex = (
        ";".join(filter_parts)
        + ";"
        + joined
        + f"concat=n={len(clip_paths)}:v=1:a={1 if any_audio else 0}"
        + ("[outv][outa]" if any_audio else "[outv]")
    )

    cmd = [
        ffmpeg_path,
        "-y",
    ] + inputs + [
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
    ]

    if any_audio:
        cmd.extend([
            "-map",
            "[outa]",
        ])

    cmd.extend([
        "-c:v",
        enc,
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ])

    if any_audio:
        cmd.extend([
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-ar",
            "48000",
            "-ac",
            "2",
        ])

    if enc == "libx264":
        cmd.extend([
            "-tune",
            "film",
            "-profile:v",
            "high",
            "-level",
            "4.2",
        ])

    cmd.append(output_path)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.returncode != 0:
        raise ffmpeg_failure(
            "join the clips",
            result.stderr,
        )

    if (
        not os.path.exists(output_path)
        or os.path.getsize(output_path) < 1000
    ):
        raise VideoToolError(
            "The joined video came out empty."
        )

    return output_path


def apply_watermark(
    ffmpeg_path,
    input_path,
    output_path,
    logo_path,
    position="bottomright",
    margin=20,
    width=None,
    opacity=1.0,
    crf=18,
    preset="slow",
    encoder=None,
):
    """
    Burn a logo onto a video.

    This always re-encodes because a watermark changes pixels.

    The watermark encode uses the input video's duration to calculate a
    suitable timeout. This avoids the old fixed 600-second limit on longer
    compilations.

    If the encode times out, _run_encode() removes any partial output and
    raises a VideoToolError with a user-friendly message.
    """
    if not ffmpeg_path:
        raise MissingToolError(
            "FFmpeg was not found. Install it and make sure it is on "
            "your PATH."
        )

    if not os.path.exists(input_path):
        raise FileNotFoundError(
            f"Input not found: {input_path}"
        )

    if not os.path.exists(logo_path):
        raise FileNotFoundError(
            f"Logo not found: {logo_path}"
        )

    os.makedirs(
        os.path.dirname(output_path) or ".",
        exist_ok=True,
    )

    positions = {
        "topleft": f"{margin}:{margin}",
        "topright": (
            f"main_w-overlay_w-{margin}:{margin}"
        ),
        "bottomleft": (
            f"{margin}:main_h-overlay_h-{margin}"
        ),
        "bottomright": (
            f"main_w-overlay_w-{margin}:"
            f"main_h-overlay_h-{margin}"
        ),
        "center": (
            "(main_w-overlay_w)/2:"
            "(main_h-overlay_h)/2"
        ),
    }

    overlay_xy = positions.get(
        position,
        positions["bottomright"],
    )

    logo_filters = []

    if width:
        logo_filters.append(
            f"scale={int(width)}:-1"
        )

    if opacity < 1.0:
        logo_filters.append(
            f"format=rgba,"
            f"colorchannelmixer=aa={opacity}"
        )

    logo_chain = (
        ",".join(logo_filters)
        if logo_filters
        else "null"
    )

    filter_complex = (
        f"[1:v]{logo_chain}[logo];"
        f"[0:v][logo]overlay={overlay_xy}[outv]"
    )

    enc = encoder or get_best_video_encoder(
        ffmpeg_path
    )

    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        input_path,
        "-i",
        logo_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
        "-map",
        "0:a?",
        "-c:v",
        enc,
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-c:a",
        "copy",
        output_path,
    ]

    # --------------------------------------------------------------
    # Duration-aware timeout
    # --------------------------------------------------------------
    #
    # Watermarking requires a complete video re-encode. Use the same
    # duration-based timeout model as trim_clip() rather than the old
    # fixed 600-second limit.
    #
    # If ffprobe cannot determine the duration, keep a conservative
    # 600-second fallback.
    #
    durations = probe_durations(input_path)
    input_duration = durations.get("file")

    if input_duration:
        encode_timeout = _encode_timeout(
            input_duration
        )
    else:
        encode_timeout = 600.0

    print(
        f"  🏷️ Watermarking ({position})..."
    )

    result = _run_encode(
        cmd,
        output_path,
        encode_timeout,
    )

    if result.returncode != 0:
        raise ffmpeg_failure(
            "add the watermark",
            result.stderr,
        )

    if (
        not os.path.exists(output_path)
        or os.path.getsize(output_path) < 1000
    ):
        raise VideoToolError(
            "The video with the watermark came out empty."
        )

    print("  ✅ Watermark applied")

    return output_path


def find_caption_font():
    """Look for a usable font file for burned-in captions."""
    candidates = [
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",

        # macOS
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",

        # Windows
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]

    for path in candidates:
        if os.path.exists(path):
            return path

    return None


def build_caption_filter(
    text,
    tmp_dir,
    fontfile=None,
    fontsize=36,
    color="white",
    box=True,
    box_color="0x1E343D@0.92",
    position="bottom",
    margin=40,
    uppercase=True,
):
    """Build a drawtext filter that burns text onto a clip.

    Returns:
        (filter_string, text_file_path)
    """
    if uppercase:
        text = text.upper()

    os.makedirs(
        tmp_dir,
        exist_ok=True,
    )

    fd, txt_path = tempfile.mkstemp(
        suffix=".txt",
        prefix="caption_",
        dir=tmp_dir,
    )

    with os.fdopen(
        fd,
        "w",
        encoding="utf-8",
    ) as f:
        f.write(text)

    positions = {
        "bottom": (
            "x=(w-text_w)/2:"
            f"y=h-text_h-{margin}"
        ),
        "top": (
            "x=(w-text_w)/2:"
            f"y={margin}"
        ),
    }

    xy = positions.get(
        position,
        positions["bottom"],
    )

    parts = (
        [f"fontfile='{fontfile}'"]
        if fontfile
        else ["font=Sans"]
    )

    parts += [
        f"textfile='{txt_path}'",
        "expansion=none",
        f"fontsize={fontsize}",
        f"fontcolor={color}",
        xy,
    ]

    if box:
        parts += [
            "box=1",
            f"boxcolor={box_color}",
            "boxborderw=16",
        ]

    return (
        "drawtext=" + ":".join(parts),
        txt_path,
    )


# === CSV READING ===

def read_csv_entries(csv_path):
    """Read CSV entries for auto-compiler.

    Expected format:
        player,match_id,action,start_time,end_time

    Skips comment lines starting with // or # and blank lines.
    """
    entries = []

    raw_lines = []

    with open(
        csv_path,
        encoding="utf-8",
    ) as f:
        for line in f:
            stripped = line.strip()

            if (
                not stripped
                or stripped.startswith("//")
                or stripped.startswith("#")
            ):
                continue

            raw_lines.append(line)

    if not raw_lines:
        return []

    reader = csv.DictReader(raw_lines)
    fieldnames = reader.fieldnames or []

    col_map = {}

    for col in fieldnames:
        col_lower = col.lower().strip()

        if col_lower in (
            "player",
            "name",
            "player_name",
        ):
            col_map["player"] = col

        elif col_lower in (
            "match_id",
            "match",
            "video",
            "url",
            "video_id",
            "matchid",
        ):
            col_map["match_id"] = col

        elif col_lower in (
            "action",
            "event",
            "clip_name",
            "description",
        ):
            col_map["action"] = col

        elif col_lower in (
            "start_time",
            "start",
            "starttime",
            "begin",
        ):
            col_map["start_time"] = col

        elif col_lower in (
            "end_time",
            "end",
            "endtime",
            "finish",
        ):
            col_map["end_time"] = col

    # Default mapping if no recognized columns
    if not col_map and len(fieldnames) >= 5:
        col_map = {
            "player": fieldnames[0],
            "match_id": fieldnames[1],
            "action": fieldnames[2],
            "start_time": fieldnames[3],
            "end_time": fieldnames[4],
        }

    for row in reader:
        try:
            player = row.get(
                col_map.get(
                    "player",
                    "player",
                ),
                "",
            ).strip()

            match_id = row.get(
                col_map.get(
                    "match_id",
                    "match_id",
                ),
                "",
            ).strip()

            action = row.get(
                col_map.get(
                    "action",
                    "action",
                ),
                "",
            ).strip()

            start_str = row.get(
                col_map.get(
                    "start_time",
                    "start_time",
                ),
                "",
            ).strip()

            end_str = row.get(
                col_map.get(
                    "end_time",
                    "end_time",
                ),
                "",
            ).strip()

            if not player or not match_id:
                continue

            start = (
                parse_time_to_seconds(start_str)
                or 0
            )

            end = parse_time_to_seconds(
                end_str
            )

            if end is not None and end <= start:
                continue

            entries.append({
                "player": player,
                "match_id": match_id,
                "action": action or "clip",
                "start": start,
                "end": end,
                "start_str": start_str or "0:00",
                "end_str": end_str or "",
            })

        except Exception:
            continue

    return entries


def format_size(size_bytes):
    """Format byte size to human readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"

    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"

    elif size_bytes < 1024 * 1024 * 1024:
        return (
            f"{size_bytes / (1024 * 1024):.1f} MB"
        )

    else:
        return (
            f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        )


def format_duration(seconds):
    """Format seconds to MM:SS or HH:MM:SS."""
    if seconds is None:
        return "00:00"

    seconds = int(seconds)

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"

    return f"{m:02d}:{s:02d}"
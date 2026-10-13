
"""Manage private runtime dependencies for the Video Tool GUI."""

from __future__ import annotations

import json
import logging
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import time
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None] | None

BUNDLE_DIR = Path.home() / ".video-tool-gui" / "bin"
STATE_PATH = Path.home() / ".video-tool-gui" / "check_state.json"
VERSION_CHECK_INTERVAL_HOURS = 24

YTDLP_RELEASE_BASE = (
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download"
)
DENO_RELEASE_BASE = (
    "https://github.com/denoland/deno/releases/latest/download"
)
FFMPEG_RELEASE_BASE = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download"
)
YTDLP_API_URL = (
    "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
)

SYSTEM = platform.system()
MACHINE = platform.machine().lower()

YTDLP_ASSETS = {
    "Windows": "yt-dlp.exe",
    "Linux": "yt-dlp_linux",
    "Darwin": "yt-dlp_macos",
}

DENO_ASSETS = {
    ("Windows", "x86_64"): "deno-x86_64-pc-windows-msvc.zip",
    ("Windows", "amd64"): "deno-x86_64-pc-windows-msvc.zip",
    ("Linux", "x86_64"): "deno-x86_64-unknown-linux-gnu.zip",
    ("Linux", "amd64"): "deno-x86_64-unknown-linux-gnu.zip",
    ("Linux", "aarch64"): "deno-aarch64-unknown-linux-gnu.zip",
    ("Linux", "arm64"): "deno-aarch64-unknown-linux-gnu.zip",
    ("Darwin", "x86_64"): "deno-x86_64-apple-darwin.zip",
    ("Darwin", "amd64"): "deno-x86_64-apple-darwin.zip",
    ("Darwin", "aarch64"): "deno-aarch64-apple-darwin.zip",
    ("Darwin", "arm64"): "deno-aarch64-apple-darwin.zip",
}

FFMPEG_ASSETS = {
    "Windows": "ffmpeg-master-latest-win64-gpl.zip",
    "Linux": "ffmpeg-master-latest-linux64-gpl.tar.xz",
}


def _report(cb: ProgressCallback, message: str) -> None:
    if cb:
        cb(message)
    else:
        print(f"[bundled_deps] {message}")


def _make_executable(path: Path) -> None:
    if SYSTEM != "Windows":
        path.chmod(
            path.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )


def _binary_name(tool: str) -> str:
    suffix = ".exe" if SYSTEM == "Windows" else ""
    return f"{tool}{suffix}"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _due_for_check(
    state: dict,
    key: str,
    interval_hours: int = VERSION_CHECK_INTERVAL_HOURS,
) -> bool:
    last = state.get(key)
    if not last:
        return True

    try:
        checked_at = datetime.fromisoformat(last)
        return datetime.now() - checked_at >= timedelta(
            hours=interval_hours
        )
    except (TypeError, ValueError):
        return True


def _stamp_check(key: str) -> None:
    state = _load_state()
    state[key] = datetime.now().isoformat()
    try:
        _save_state(state)
    except OSError:
        logger.warning("Could not save dependency check state", exc_info=True)



def _download(
    url: str,
    destination: Path,
    progress_callback: ProgressCallback = None,
) -> bool:
    """Download a file, resuming a partial transfer after network failures.

    The partial file is deliberately retained after a failed attempt and
    between application launches. If the server supports HTTP Range requests,
    the next attempt continues from the last byte already written. If it does
    not support ranges, a normal HTTP 200 response safely restarts the file.
    """
    temporary = destination.with_name(destination.name + ".download")
    max_attempts = 8
    destination.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_attempts + 1):
        existing_size = temporary.stat().st_size if temporary.exists() else 0
        headers = {
            "User-Agent": "Video-Tool-GUI",
            "Accept-Encoding": "identity",
        }
        if existing_size:
            headers["Range"] = f"bytes={existing_size}-"
            _report(
                progress_callback,
                f"Resuming {destination.name} from {existing_size / (1024 * 1024):.1f} MiB...",
            )

        try:
            expected_total: int | None = None
            with requests.get(
                url,
                stream=True,
                timeout=(30, 600),
                headers=headers,
            ) as response:
                # A stale partial may be larger than the asset currently served.
                # Clear it and retry once from byte zero.
                if response.status_code == 416 and existing_size:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(
                        "The server rejected the resume offset; restarting the download"
                    )

                if response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.match(
                        r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$",
                        content_range.strip(),
                        flags=re.IGNORECASE,
                    )
                    if not match or int(match.group(1)) != existing_size:
                        # Never append bytes unless the server confirms the exact
                        # starting offset. The next attempt starts cleanly.
                        temporary.unlink(missing_ok=True)
                        raise RuntimeError(
                            "The server returned an invalid Content-Range header"
                        )

                    mode = "ab" if existing_size else "wb"
                    total_text = match.group(3)
                    if total_text != "*":
                        expected_total = int(total_text)
                    else:
                        response_length = response.headers.get("Content-Length")
                        if response_length and response_length.isdigit():
                            expected_total = existing_size + int(response_length)
                else:
                    response.raise_for_status()
                    # Some servers ignore Range and return the entire asset (200).
                    # Opening with wb truncates the partial instead of duplicating it.
                    mode = "wb"
                    response_length = response.headers.get("Content-Length")
                    if response_length and response_length.isdigit():
                        expected_total = int(response_length)

                with temporary.open(mode) as output:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            output.write(chunk)

            actual_size = temporary.stat().st_size if temporary.exists() else 0
            if actual_size == 0:
                raise RuntimeError("The downloaded file is empty")
            if expected_total is not None and actual_size != expected_total:
                raise RuntimeError(
                    f"Incomplete download: received {actual_size} of {expected_total} bytes"
                )

            temporary.replace(destination)
            return True

        except Exception:
            logger.warning(
                "Download attempt %s/%s failed: %s",
                attempt,
                max_attempts,
                url,
                exc_info=True,
            )
            # Do not delete the .download file: it is the resume checkpoint.
            if attempt < max_attempts:
                delay = min(2 ** (attempt - 1), 30)
                _report(
                    progress_callback,
                    "Download interrupted; preserving received data and "
                    f"retrying in {delay}s ({attempt + 1}/{max_attempts})...",
                )
                time.sleep(delay)

    _report(
        progress_callback,
        f"Could not finish downloading {destination.name}. The partial file "
        "has been kept so a later attempt can resume it.",
    )
    return False

def _works(path: Path, argument: str = "--version") -> bool:
    """Check that a downloaded executable actually runs."""
    try:
        result = subprocess.run(
            [str(path), argument],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return result.returncode == 0 and bool(
            (result.stdout or result.stderr).strip()
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _install_binary(
    url: str,
    target: Path,
    check_argument: str = "--version",
) -> bool:
    """Download and validate a binary before replacing the working copy."""
    staged = target.with_name(target.name + ".new")
    staged.unlink(missing_ok=True)

    if not _download(url, staged):
        return False

    try:
        _make_executable(staged)

        if not _works(staged, check_argument):
            raise RuntimeError("Downloaded executable failed its version check")

        staged.replace(target)
        _make_executable(target)
        return True

    except Exception:
        logger.warning("Could not install %s", target.name, exc_info=True)
        staged.unlink(missing_ok=True)
        return False


def _update_binary(
    tool: str,
    asset_url: str,
    check_key: str,
    progress: ProgressCallback,
) -> Path | None:
    """Ensure a binary exists and refresh it at most once per check interval."""
    target = BUNDLE_DIR / _binary_name(tool)
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

    if not target.exists() or not _works(target):
        _report(progress, f"Downloading {tool}...")
        if _install_binary(asset_url, target):
            _stamp_check(check_key)
            _report(progress, f"{tool} ready.")
            return target

        # A broken file should not be treated as a usable dependency.
        if target.exists() and not _works(target):
            target.unlink(missing_ok=True)
        return target if target.exists() else None

    state = _load_state()
    if not _due_for_check(state, check_key):
        return target

    # yt-dlp has a release API; Deno and FFmpeg use rolling latest links.
    if tool == "yt-dlp":
        try:
            response = requests.get(
                YTDLP_API_URL,
                timeout=15,
                headers={"User-Agent": "Video-Tool-GUI"},
            )
            response.raise_for_status()
            latest_version = response.json()["tag_name"].lstrip("v")

            installed_result = subprocess.run(
                [str(target), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            installed_version = installed_result.stdout.strip().lstrip("v")

            if installed_version == latest_version:
                _stamp_check(check_key)
                return target

            _report(
                progress,
                f"Updating yt-dlp ({installed_version} → {latest_version})...",
            )
        except Exception:
            logger.warning("Could not check yt-dlp version", exc_info=True)
            _stamp_check(check_key)
            return target

    else:
        _report(progress, f"Refreshing {tool}...")

    if _install_binary(asset_url, target):
        _stamp_check(check_key)
        _report(progress, f"{tool} updated.")
    else:
        _report(progress, f"Could not update {tool}; keeping the existing copy.")

    return target


def ensure_ytdlp(progress_callback: ProgressCallback = None) -> Path | None:
    """Ensure the standalone yt-dlp executable exists and is current."""
    asset = YTDLP_ASSETS.get(SYSTEM)
    if not asset:
        _report(progress_callback, f"Automatic yt-dlp setup is unsupported on {SYSTEM}.")
        return None

    return _update_binary(
        "yt-dlp",
        f"{YTDLP_RELEASE_BASE}/{asset}",
        "ytdlp_checked_at",
        progress_callback,
    )



def ensure_deno(progress_callback: ProgressCallback = None) -> Path | None:
    """Install or refresh Deno from its official release archive."""
    asset = DENO_ASSETS.get((SYSTEM, MACHINE))
    if not asset:
        _report(
            progress_callback,
            f"Automatic Deno setup is unsupported on {SYSTEM} ({MACHINE}).",
        )
        return None

    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    target = BUNDLE_DIR / _binary_name("deno")
    state = _load_state()

    if target.exists() and not _works(target):
        target.unlink(missing_ok=True)

    if target.exists() and not _due_for_check(state, "deno_checked_at"):
        return target

    _report(
        progress_callback,
        "Downloading Deno..." if not target.exists() else "Checking for a Deno update...",
    )

    archive = BUNDLE_DIR / asset
    url = f"{DENO_RELEASE_BASE}/{asset}"

    if not _download(url, archive):
        _report(progress_callback, "Could not download Deno; keeping the existing copy if available.")
        return target if target.exists() and _works(target) else None

    try:
        if _extract_archive_binary(archive, target, _binary_name("deno")):
            _stamp_check("deno_checked_at")
            _report(progress_callback, "Deno ready.")
            return target

        _report(progress_callback, "Deno update failed; keeping the existing copy if available.")
        return target if target.exists() and _works(target) else None

    finally:
        archive.unlink(missing_ok=True)



def _extract_archive_binary(
    archive: Path,
    destination: Path,
    binary_name: str,
) -> bool:
    """Extract the requested binary and replace it only after validation."""
    extract_dir = BUNDLE_DIR / f"_{binary_name}_extract"
    staged = destination.with_name(destination.name + ".new")

    shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    staged.unlink(missing_ok=True)

    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                member = next(
                    (
                        item for item in zf.infolist()
                        if Path(item.filename).name == binary_name
                    ),
                    None,
                )
                if member is None:
                    raise RuntimeError(f"Archive has no {binary_name}")
                with zf.open(member) as source, staged.open("wb") as output:
                    shutil.copyfileobj(source, output)

        else:
            with tarfile.open(archive, "r:*") as tf:
                member = next(
                    (
                        item for item in tf.getmembers()
                        if item.isfile() and Path(item.name).name == binary_name
                    ),
                    None,
                )
                if member is None:
                    raise RuntimeError(f"Archive has no {binary_name}")
                source = tf.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Could not read {binary_name}")
                with source, staged.open("wb") as output:
                    shutil.copyfileobj(source, output)

        _make_executable(staged)

        if not _works(staged):
            raise RuntimeError(f"Extracted {binary_name} failed its version check")

        staged.replace(destination)
        _make_executable(destination)
        return True

    except Exception:
        logger.warning("Archive extraction failed: %s", archive, exc_info=True)
        staged.unlink(missing_ok=True)
        return False

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)



def ensure_ffmpeg(progress_callback: ProgressCallback = None) -> Path | None:
    """Ensure FFmpeg exists, preferring the bundled copy and falling back to system FFmpeg."""
    system_ffmpeg = shutil.which("ffmpeg")

    def fallback() -> Path | None:
        if system_ffmpeg:
            _report(
                progress_callback,
                f"Using system FFmpeg: {system_ffmpeg}",
            )
            return Path(system_ffmpeg)
        return None

    if SYSTEM == "Darwin":
        if system_ffmpeg:
            return Path(system_ffmpeg)
        _report(
            progress_callback,
            "FFmpeg was not found. Install it with 'brew install ffmpeg'.",
        )
        return None

    asset = FFMPEG_ASSETS.get(SYSTEM)
    if not asset:
        _report(
            progress_callback,
            f"Automatic FFmpeg setup is unsupported on {SYSTEM}.",
        )
        return fallback()

    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    target = BUNDLE_DIR / _binary_name("ffmpeg")
    state = _load_state()
    due = _due_for_check(state, "ffmpeg_checked_at", interval_hours=168)

    if target.exists() and not _works(target):
        target.unlink(missing_ok=True)

    if target.exists() and not due:
        return target

    _report(
        progress_callback,
        "Downloading FFmpeg..."
        if not target.exists()
        else "Checking for an FFmpeg update...",
    )

    archive = BUNDLE_DIR / asset
    url = f"{FFMPEG_RELEASE_BASE}/{asset}"

    if not _download(url, archive, progress_callback):
        archive.unlink(missing_ok=True)
        _report(
            progress_callback,
            "Could not download FFmpeg; keeping the existing copy if available.",
        )
        if target.exists() and _works(target):
            return target
        return fallback()

    try:
        if _extract_archive_binary(archive, target, _binary_name("ffmpeg")):
            _stamp_check("ffmpeg_checked_at")
            _report(progress_callback, "FFmpeg ready.")
            return target

        _report(
            progress_callback,
            "FFmpeg update failed; keeping the existing copy if available.",
        )
        if target.exists() and _works(target):
            return target
        return fallback()

    finally:
        archive.unlink(missing_ok=True)



def ensure_all(progress_callback: ProgressCallback = None) -> dict:
    """Set up all tools independently; return every successfully available path."""
    return {
        "ytdlp": ensure_ytdlp(progress_callback),
        "deno": ensure_deno(progress_callback),
        "ffmpeg": ensure_ffmpeg(progress_callback),
    }

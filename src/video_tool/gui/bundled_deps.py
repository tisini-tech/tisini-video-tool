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

from ..procutil import child_process_kwargs

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None] | None

BUNDLE_DIR = Path.home() / ".video-tool-gui" / "bin"
STATE_PATH = Path.home() / ".video-tool-gui" / "check_state.json"
VERSION_CHECK_INTERVAL_HOURS = 24
# The FFmpeg archive is about 150 MB and the build is a rolling one, so a
# fresh copy rarely matters. Check monthly.
FFMPEG_CHECK_INTERVAL_HOURS = 24 * 30

DENO_LATEST_URL = "https://github.com/denoland/deno/releases/latest"

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


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Replace a file, trying again if Windows says it is busy.

    Antivirus programs often hold a new file for a moment, which makes the
    first attempt fail with PermissionError.
    """
    for attempt in range(5):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.3 * (attempt + 1))


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _replace_with_retry(temporary, STATE_PATH)


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
        return datetime.now() - checked_at >= timedelta(hours=interval_hours)
    except (TypeError, ValueError):
        return True


def _stamp_check(key: str) -> None:
    state = _load_state()
    state[key] = datetime.now().isoformat()
    try:
        _save_state(state)
    except OSError:
        logger.warning("Could not save dependency check state", exc_info=True)


def _fingerprint(path: Path) -> dict | None:
    """Describe a file by its size and change time."""
    try:
        info = path.stat()
    except OSError:
        return None
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _is_verified(path: Path) -> bool:
    """Return True if this exact file already passed its check here.

    Running yt-dlp.exe takes seconds, because it unpacks itself first, and
    it can fail while antivirus scans it. A file that has not changed since
    it last passed does not need that test again.
    """
    fingerprint = _fingerprint(path)
    if fingerprint is None:
        return False
    verified = _load_state().get("verified")
    return isinstance(verified, dict) and verified.get(path.name) == fingerprint


def _mark_verified(path: Path) -> None:
    """Remember that this file passed its check."""
    fingerprint = _fingerprint(path)
    if fingerprint is None:
        return
    state = _load_state()
    verified = state.get("verified")
    if not isinstance(verified, dict):
        verified = {}
    verified[path.name] = fingerprint
    state["verified"] = verified
    try:
        _save_state(state)
    except OSError:
        logger.warning("Could not save dependency check state", exc_info=True)


def _download(
    url: str,
    destination: Path,
    progress_callback: ProgressCallback = None,
) -> bool:
    """Download a file and resume partial transfers when HTTP ranges work.

    A partial file is kept after a failed attempt and between application
    launches. If a server ignores the Range header and returns HTTP 200, the
    partial file is overwritten instead of appending duplicate bytes.
    """
    temporary = destination.with_name(destination.name + ".download")
    max_attempts = 8
    last_error = "Unknown download error"
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
                f"Resuming {destination.name} from "
                f"{existing_size / (1024 * 1024):.1f} MiB...",
            )

        try:
            expected_total: int | None = None
            with requests.get(
                url,
                stream=True,
                timeout=(30, 600),
                headers=headers,
            ) as response:
                # A 416 usually means the saved partial no longer matches the
                # asset. Remove it and retry from the beginning.
                if response.status_code == 416 and existing_size:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(
                        "The server rejected the resume offset; restarting "
                        "the download"
                    )

                if response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.match(
                        r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$",
                        content_range.strip(),
                        flags=re.IGNORECASE,
                    )
                    if not match or int(match.group(1)) != existing_size:
                        # Never append bytes unless the server confirms the
                        # exact offset. Start cleanly on the next attempt.
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
                    # Servers that ignore Range respond with 200: restart the
                    # temporary file rather than duplicating its contents.
                    mode = "wb"
                    response_length = response.headers.get("Content-Length")
                    if response_length and response_length.isdigit():
                        expected_total = int(response_length)

                last_progress_bucket = -1
                with temporary.open(mode) as output:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue

                        output.write(chunk)
                        received = output.tell()
                        progress_bucket = received // (32 * 1024 * 1024)

                        if progress_bucket > last_progress_bucket:
                            last_progress_bucket = progress_bucket
                            received_mb = received / (1024 * 1024)

                            if expected_total:
                                total_mb = expected_total / (1024 * 1024)
                                message = (
                                    f"Downloading {destination.name}: "
                                    f"{received_mb:.0f} / {total_mb:.0f} MiB"
                                )
                            else:
                                message = (
                                    f"Downloading {destination.name}: "
                                    f"{received_mb:.0f} MiB received"
                                )

                            _report(progress_callback, message)

            actual_size = temporary.stat().st_size if temporary.exists() else 0
            if actual_size == 0:
                raise RuntimeError("The downloaded file is empty")
            if expected_total is not None and actual_size != expected_total:
                raise RuntimeError(
                    f"Incomplete download: received {actual_size} of "
                    f"{expected_total} bytes"
                )

            temporary.replace(destination)
            return True

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Download attempt %s/%s failed: %s",
                attempt,
                max_attempts,
                url,
                exc_info=True,
            )

            # Keep the partial .download file so the next attempt can resume.
            if attempt < max_attempts:
                delay = min(2 ** (attempt - 1), 30)
                _report(
                    progress_callback,
                    f"Download attempt {attempt}/{max_attempts} failed "
                    f"({last_error}). Retrying in {delay}s; received data "
                    "will be preserved when possible...",
                )
                time.sleep(delay)

    _report(
        progress_callback,
        f"Could not finish downloading {destination.name} after "
        f"{max_attempts} attempts. Last error: {last_error}. "
        f"Partial download, if present: {temporary}",
    )
    return False


def _version_arg(name: str, argument: str = "--version") -> str:
    """Return the flag that makes the tool called ``name`` print its version.

    FFmpeg tools take a single dash (-version). Every other tool we manage
    (yt-dlp, Deno) takes two (--version) and rejects a single dash. The
    staged copy is named like "ffmpeg.exe.new", so match on the start.
    """
    if argument == "--version" and name.startswith(("ffmpeg", "ffprobe")):
        return "-version"
    return argument


def _works(path: Path, argument: str = "--version") -> bool:
    """Check that a downloaded executable actually runs."""
    argument = _version_arg(path.name, argument)
    try:
        result = subprocess.run(
            [str(path), argument],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            **child_process_kwargs(),
        )
        return result.returncode == 0 and bool(
            (result.stdout or result.stderr).strip()
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _usable(path: Path, attempts: int = 2) -> bool:
    """Return True if ``path`` is a program that works.

    A file that already passed and has not changed is trusted without
    running it. Otherwise it is run, and a failure is tried again before
    the file is called broken: one failure can come from antivirus
    scanning, not from a bad file.
    """
    if not path.exists():
        return False

    if _is_verified(path):
        return True

    for attempt in range(attempts):
        if _works(path):
            _mark_verified(path)
            return True
        if attempt + 1 < attempts:
            time.sleep(2)

    return False


def _run_check(path: Path, argument: str) -> tuple[bool, str]:
    """Run an executable and say why it failed, if it did."""
    try:
        result = subprocess.run(
            [str(path), argument],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            **child_process_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return False, "timed out after 60 s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"

    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        last = output.splitlines()[-1] if output else "no output"
        return False, f"exit code {result.returncode}: {last}"
    if not output:
        return False, "no output"
    return True, output.splitlines()[0]


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

        _replace_with_retry(staged, target)
        _make_executable(target)
        _mark_verified(target)
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

    if not _usable(target):
        _report(progress, f"Downloading {tool}...")
        if _install_binary(asset_url, target):
            _stamp_check(check_key)
            _report(progress, f"{tool} ready.")
            return target

        # The copy on disk did not pass. Do not use it, but do not delete it
        # either: it may only fail to start right now, and the next launch
        # will try again. Callers fall back to the built-in yt-dlp.
        return None

    state = _load_state()
    if not _due_for_check(state, check_key):
        return target

    # yt-dlp has a release API; Deno uses a rolling latest link.
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
                timeout=60,
                check=False,
                **child_process_kwargs(),
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
        _report(
            progress_callback,
            f"Automatic yt-dlp setup is unsupported on {SYSTEM}.",
        )
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

    usable = _usable(target)

    if usable and not _due_for_check(state, "deno_checked_at"):
        return target

    if usable:
        # Download the 40 MB archive only when a newer Deno exists.
        latest = _latest_deno_version()
        installed = _installed_deno_version(target)
        if latest is None or installed == latest:
            _stamp_check("deno_checked_at")
            return target
        _report(
            progress_callback,
            f"Updating Deno ({installed or 'unknown'} → {latest})...",
        )
    else:
        _report(progress_callback, "Downloading Deno...")

    archive = BUNDLE_DIR / asset
    url = f"{DENO_RELEASE_BASE}/{asset}"

    if not _download(url, archive, progress_callback):
        _report(
            progress_callback,
            "Could not download Deno; keeping the existing copy if available.",
        )
        return target if usable else None

    try:
        if _extract_archive_binary(archive, target, _binary_name("deno")):
            _stamp_check("deno_checked_at")
            _report(progress_callback, "Deno ready.")
            return target

        _report(
            progress_callback,
            "Deno update failed; keeping the existing copy if available.",
        )
        return target if usable else None

    finally:
        archive.unlink(missing_ok=True)


def _latest_deno_version() -> str | None:
    """Return the newest Deno version, or None if it cannot be found.

    GitHub redirects /releases/latest to /releases/tag/vX.Y.Z. Reading that
    redirect avoids the API, which limits how often one address may ask.
    """
    try:
        response = requests.head(
            DENO_LATEST_URL,
            allow_redirects=False,
            timeout=15,
            headers={"User-Agent": "Video-Tool-GUI"},
        )
    except requests.RequestException:
        logger.warning("Could not look up the latest Deno version", exc_info=True)
        return None

    match = re.search(
        r"/tag/v?(\d+\.\d+\.\d+\S*)$",
        response.headers.get("Location", ""),
    )
    return match.group(1) if match else None


def _installed_deno_version(path: Path) -> str | None:
    """Return the version of the Deno at ``path``, or None."""
    ok, detail = _run_check(path, "--version")
    if not ok:
        return None
    match = re.match(r"deno\s+(\S+)", detail)
    return match.group(1) if match else None



def _extract_archive_binaries(
    archive: Path,
    binaries: dict[str, Path],
) -> bool:
    """Extract and validate all requested binaries before replacing copies."""
    staged: dict[str, Path] = {}
    backups: dict[Path, Path] = {}
    installed: list[Path] = []

    try:
        for binary_name, destination in binaries.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged_path = destination.with_name(destination.name + ".new")
            staged_path.unlink(missing_ok=True)
            staged[binary_name] = staged_path

        # Extract and validate every requested executable first.
        for binary_name, staged_path in staged.items():
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as zf:
                    member = next(
                        (
                            item for item in zf.infolist()
                            if not item.is_dir()
                            and Path(item.filename).name == binary_name
                        ),
                        None,
                    )
                    if member is None:
                        raise RuntimeError(f"Archive has no {binary_name}")
                    with zf.open(member) as source_file:
                        with staged_path.open("wb") as output:
                            shutil.copyfileobj(source_file, output)
            else:
                with tarfile.open(archive, "r:*") as tf:
                    member = next(
                        (
                            item for item in tf.getmembers()
                            if item.isfile()
                            and Path(item.name).name == binary_name
                        ),
                        None,
                    )
                    if member is None:
                        raise RuntimeError(f"Archive has no {binary_name}")
                    source_file = tf.extractfile(member)
                    if source_file is None:
                        raise RuntimeError(f"Could not read {binary_name}")
                    with source_file, staged_path.open("wb") as output:
                        shutil.copyfileobj(source_file, output)

            _make_executable(staged_path)
            # FFmpeg tools take -version; Deno takes --version.
            ok, detail = _run_check(staged_path, _version_arg(binary_name))
            if not ok:
                raise RuntimeError(
                    f"Extracted {binary_name} failed its version check: "
                    f"{detail}"
                )

        # Preserve the old copies until all replacements succeed.
        for destination in binaries.values():
            if destination.exists():
                backup = destination.with_name(destination.name + ".backup")
                backup.unlink(missing_ok=True)
                _replace_with_retry(destination, backup)
                backups[destination] = backup

        for binary_name, destination in binaries.items():
            _replace_with_retry(staged[binary_name], destination)
            installed.append(destination)
            _make_executable(destination)

        # Each file just passed its check, so remember that.
        for destination in installed:
            _mark_verified(destination)

        return True

    except Exception:
        logger.warning(
            "Archive extraction failed: %s", archive, exc_info=True
        )

        # Remove new files and restore every old copy still backed up.
        for destination in installed:
            destination.unlink(missing_ok=True)

        for destination, backup in backups.items():
            if backup.exists():
                backup.replace(destination)

        return False

    finally:
        for staged_path in staged.values():
            staged_path.unlink(missing_ok=True)

        # Backups are deleted only after a successful installation.
        if len(installed) == len(binaries):
            for backup in backups.values():
                backup.unlink(missing_ok=True)


def _extract_archive_binary(
    archive: Path,
    destination: Path,
    binary_name: str,
) -> bool:
    """Extract and validate one binary without replacing it prematurely."""
    return _extract_archive_binaries(archive, {binary_name: destination})


def ensure_ffmpeg(progress_callback: ProgressCallback = None) -> Path | None:
    """Ensure bundled FFmpeg and ffprobe exist, with system fallback."""
    system_ffmpeg = shutil.which("ffmpeg")
    system_ffprobe = shutil.which("ffprobe")

    def fallback() -> Path | None:
        if system_ffmpeg and system_ffprobe:
            _report(
                progress_callback,
                f"Using system FFmpeg: {system_ffmpeg}",
            )
            return Path(system_ffmpeg)
        return None

    if SYSTEM == "Darwin":
        return fallback()

    asset = FFMPEG_ASSETS.get(SYSTEM)
    if not asset:
        _report(
            progress_callback,
            f"Automatic FFmpeg setup is unsupported on {SYSTEM}.",
        )
        return fallback()

    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    target = BUNDLE_DIR / _binary_name("ffmpeg")
    ffprobe_target = BUNDLE_DIR / _binary_name("ffprobe")

    ffmpeg_ok = _usable(target)
    ffprobe_ok = _usable(ffprobe_target)
    both_ok = ffmpeg_ok and ffprobe_ok

    state = _load_state()
    due = _due_for_check(
        state,
        "ffmpeg_checked_at",
        interval_hours=FFMPEG_CHECK_INTERVAL_HOURS,
    )

    if both_ok and not due:
        return target

    _report(
        progress_callback,
        "Checking for an FFmpeg update..."
        if both_ok
        else "Downloading FFmpeg tools...",
    )

    archive = BUNDLE_DIR / asset
    url = f"{FFMPEG_RELEASE_BASE}/{asset}"

    if not _download(url, archive, progress_callback):
        _report(
            progress_callback,
            "Could not download FFmpeg; keeping working copies if available.",
        )
        if both_ok:
            return target
        return fallback()

    try:
        binaries = {
            _binary_name("ffmpeg"): target,
            _binary_name("ffprobe"): ffprobe_target,
        }

        if _extract_archive_binaries(archive, binaries):
            _stamp_check("ffmpeg_checked_at")
            _report(progress_callback, "FFmpeg and ffprobe ready.")
            return target

        _report(
            progress_callback,
            "FFmpeg update failed; keeping working copies if available.",
        )

        if both_ok:
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

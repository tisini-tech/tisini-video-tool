"""Manages a private, app-owned copy of yt-dlp and ffmpeg for the GUI app.

This is deliberately separate from version_check.py, which checks the
SYSTEM yt-dlp/ffmpeg the CLI suite relies on, and only ever warns about
ffmpeg since it's outside pip's control. Everything in this module lives
in the GUI app's own private folder and never touches a system-wide
install, so it's safe to download and replace automatically.

A frozen executable can't `pip install --upgrade` itself -- there's no
live Python environment inside a compiled bundle to upgrade. So instead of
pip-upgrading yt-dlp the way the CLI does, this downloads yt-dlp's own
prebuilt standalone executable and calls it as a subprocess -- the same
way ffmpeg has always been treated in this project.

Downloads always go through GitHub's stable "latest release" redirect
(github.com/OWNER/REPO/releases/latest/download/ASSET), never the
api.github.com REST endpoint. That distinction matters: the API is capped
at 60 unauthenticated requests/hour *per IP*, easy to exhaust with
repeated testing or a handful of friends sharing a home network, while the
plain download redirect has no such limit. The REST API is only used
here, at most once a day, to check whether an *already-downloaded* binary
is stale -- never on the path that gets someone unblocked the first time.
"""
from __future__ import annotations

import json
import platform
import shutil
import stat
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import requests

ProgressCallback = Callable[[str], None] | None

BUNDLE_DIR = Path.home() / ".video-tool-gui" / "bin"
STATE_PATH = Path.home() / ".video-tool-gui" / "check_state.json"
VERSION_CHECK_INTERVAL_HOURS = 24

# Stable direct-download links -- never rate-limited, unlike api.github.com.
_YTDLP_ASSET_BY_OS = {
    "Windows": "yt-dlp.exe",
    "Linux": "yt-dlp_linux",
    "Darwin": "yt-dlp_macos",
}
_FFMPEG_ASSET_BY_OS = {
    # BtbN doesn't publish macOS builds; macOS falls back to a manual
    # instruction rather than a guessed URL. See ensure_ffmpeg().
    "Windows": "ffmpeg-master-latest-win64-gpl.zip",
    "Linux": "ffmpeg-master-latest-linux64-gpl.tar.xz",
}
_YTDLP_RELEASE_BASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download"
_FFMPEG_RELEASE_BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download"

# Only used for the optional, interval-gated "is this stale" check on an
# existing yt-dlp binary -- never on the download path itself.
YTDLP_API_URL = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"


def _report(cb: ProgressCallback, message: str) -> None:
    if cb:
        cb(message)
    else:
        print(f"[bundled_deps] {message}")


def _make_executable(path: Path) -> None:
    if platform.system() != "Windows":
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _ytdlp_binary_name() -> str:
    return "yt-dlp.exe" if platform.system() == "Windows" else "yt-dlp"


def _ffmpeg_binary_name() -> str:
    return "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _due_for_check(state: dict, key: str) -> bool:
    last = state.get(key)
    if not last:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last) > timedelta(
            hours=VERSION_CHECK_INTERVAL_HOURS)
    except ValueError:
        return True


def _download_direct(url: str, target: Path,
                      progress_callback: ProgressCallback) -> bool:
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp_path = target.with_suffix(".download")
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        tmp_path.replace(target)
        _make_executable(target)
        return True
    except Exception as e:
        _report(progress_callback, f"Download failed ({e}).")
        return False


def ensure_ytdlp(progress_callback: ProgressCallback = None) -> Path | None:
    """Return a path to a working yt-dlp executable, downloading it into
    BUNDLE_DIR if missing. Returns None (never raises) only if it's both
    missing and undownloadable -- callers should handle that by disabling
    downloads, not by crashing."""
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    target = BUNDLE_DIR / _ytdlp_binary_name()
    os_name = platform.system()
    asset_name = _YTDLP_ASSET_BY_OS.get(os_name)

    if not asset_name:
        _report(progress_callback, f"Unsupported OS for yt-dlp bundling: {os_name}")
        return target if target.exists() else None

    if not target.exists():
        # Nothing to compare against yet -- skip the API check entirely
        # and go straight to the direct download link.
        _report(progress_callback, "Downloading yt-dlp...")
        download_url = f"{_YTDLP_RELEASE_BASE}/{asset_name}"
        if _download_direct(download_url, target, progress_callback):
            # Stamp "checked" now too -- otherwise the very next launch
            # would immediately retry the freshness check below instead
            # of waiting out the normal daily interval.
            state = _load_state()
            state["ytdlp_checked_at"] = datetime.now().isoformat()
            _save_state(state)
            _report(progress_callback, "yt-dlp ready.")
            return target
        return None

    # A binary already exists -- only worth checking freshness once a day,
    # and a failed check (offline, rate-limited) should never block use of
    # what's already there.
    state = _load_state()
    if not _due_for_check(state, "ytdlp_checked_at"):
        return target

    state["ytdlp_checked_at"] = datetime.now().isoformat()
    _save_state(state)

    try:
        resp = requests.get(YTDLP_API_URL, timeout=10,
                             headers={"User-Agent": "video-tool-gui"})
        resp.raise_for_status()
        latest_tag = resp.json()["tag_name"]
        result = subprocess.run([str(target), "--version"],
                                 capture_output=True, text=True, timeout=10)
        installed = result.stdout.strip()
        if installed == latest_tag:
            return target  # already current
        _report(progress_callback, f"yt-dlp {installed} -> {latest_tag}, updating...")
    except Exception as e:
        _report(progress_callback,
                f"Skipping yt-dlp freshness check ({e}). Using existing copy.")
        return target

    download_url = f"{_YTDLP_RELEASE_BASE}/{asset_name}"
    if _download_direct(download_url, target, progress_callback):
        _report(progress_callback, "yt-dlp updated.")
    return target  # keep the old one if the update download itself failed


def ensure_ffmpeg(progress_callback: ProgressCallback = None) -> Path | None:
    """Same idea as ensure_ytdlp, for ffmpeg. BtbN's builds are rolling
    'latest master' without clean version tags, so this only checks
    presence, never freshness -- and never touches api.github.com at all,
    since the direct download link doesn't need it."""
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    target = BUNDLE_DIR / _ffmpeg_binary_name()
    if target.exists():
        return target

    os_name = platform.system()
    if os_name == "Darwin":
        _report(progress_callback,
                "No automatic ffmpeg download for macOS yet -- install it with "
                "'brew install ffmpeg' and it'll be found on PATH as a fallback.")
        return None

    asset_name = _FFMPEG_ASSET_BY_OS.get(os_name)
    if not asset_name:
        _report(progress_callback, f"Unsupported OS for ffmpeg bundling: {os_name}")
        return None

    _report(progress_callback, "Downloading ffmpeg (first run only, larger file)...")
    download_url = f"{_FFMPEG_RELEASE_BASE}/{asset_name}"
    archive_path = BUNDLE_DIR / asset_name

    if not _download_direct(download_url, archive_path, progress_callback):
        archive_path.unlink(missing_ok=True)
        return None

    try:
        extract_dir = BUNDLE_DIR / "_ffmpeg_extract"
        extract_dir.mkdir(exist_ok=True)
        if asset_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as z:
                z.extractall(extract_dir)
        else:
            with tarfile.open(archive_path) as t:
                t.extractall(extract_dir)

        found = next(extract_dir.rglob(_ffmpeg_binary_name()), None)
        if not found:
            raise RuntimeError("Downloaded archive didn't contain an ffmpeg binary")
        shutil.copy2(found, target)
        _make_executable(target)

        archive_path.unlink(missing_ok=True)
        shutil.rmtree(extract_dir, ignore_errors=True)
        _report(progress_callback, "ffmpeg ready.")
        return target
    except Exception as e:
        _report(progress_callback, f"ffmpeg setup failed ({e}).")
        archive_path.unlink(missing_ok=True)
        return None


def ensure_all(progress_callback: ProgressCallback = None) -> dict:
    """Call once at GUI startup. Never raises -- returns whatever paths it
    managed to secure, possibly None for one or both."""
    return {
        "ytdlp": ensure_ytdlp(progress_callback),
        "ffmpeg": ensure_ffmpeg(progress_callback),
    }

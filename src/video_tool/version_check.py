
"""Keep yt-dlp and ffmpeg from silently going stale.

yt-dlp can break against YouTube fairly often and gets patched quickly, so
it is worth checking for updates and automatically upgrading it. yt-dlp is
a normal pip package inside the project's own environment, making the
upgrade a standard and reversible action.

ffmpeg is different: it is a system binary, not something pip manages.
Replacing it silently could affect other applications on the machine, so
ffmpeg is checked and reported only. The user decides if and how to update
it.

Both checks are limited to at most once per day. Their state is stored in
VIDEO_TOOL_CACHE_DIR, so normal runs do not pay the network-check cost.

Any network or version-check failure is swallowed and reported softly. A
version check should never be the reason a download or compile fails.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

from .core import VIDEO_TOOL_CACHE_DIR, find_ffmpeg

CHECK_INTERVAL_HOURS = 24

STATE_PATH = VIDEO_TOOL_CACHE_DIR / "version_check.json"

YT_DLP_RELEASES_URL = (
    "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
)

FFMPEG_TAGS_URL = (
    "https://api.github.com/repos/FFmpeg/FFmpeg/tags"
)


def _load_state() -> dict:
    """Load version-check timestamps from disk."""

    if not STATE_PATH.exists():
        return {}

    try:
        with open(STATE_PATH, encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    """Save version-check timestamps to disk."""

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(STATE_PATH, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


def _due_for_check(
    state: dict,
    key: str,
    interval_hours: float,
) -> bool:
    """Return True when a dependency check is due."""

    last = state.get(key)

    if not last:
        return True

    try:
        last_time = datetime.fromisoformat(last)
    except ValueError:
        return True

    return (
        datetime.now() - last_time
        > timedelta(hours=interval_hours)
    )


def _check_yt_dlp(auto_upgrade: bool = True) -> None:
    """Check yt-dlp and optionally upgrade it."""

    import yt_dlp

    installed = yt_dlp.version.__version__

    try:
        response = requests.get(
            YT_DLP_RELEASES_URL,
            timeout=10,
            headers={
                "User-Agent": "video_tool-version-check",
            },
        )
        response.raise_for_status()

        latest = response.json()["tag_name"].lstrip("v")

    except Exception:
        logger.debug("yt-dlp version check failed", exc_info=True)
        print(
            "Could not check for a newer yt-dlp (no connection to "
            f"GitHub). Using the version you have, {installed}."
        )
        return

    if installed == latest:
        return

    print(
        f"[version_check] yt-dlp {installed} is behind "
        f"latest ({latest})."
    )

    if not auto_upgrade:
        print(
            "[version_check] Run: "
            "pip install --upgrade yt-dlp"
        )
        return

    print("[version_check] Upgrading yt-dlp...")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "yt-dlp",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print(
            f"[version_check] yt-dlp upgraded: "
            f"{installed} -> {latest}"
        )
    else:
        print(
            "[version_check] yt-dlp upgrade failed, "
            f"continuing with {installed}:\n"
            f"{result.stderr.strip()}"
        )


def _parse_ffmpeg_version(
    ffmpeg_path: str,
) -> str | None:
    """Extract the version string reported by ffmpeg."""

    try:
        result = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        match = re.search(
            r"ffmpeg version (\S+)",
            result.stdout,
        )

        return match.group(1) if match else None

    except Exception:
        return None


def _check_ffmpeg() -> None:
    """Check the installed ffmpeg version without modifying it."""

    ffmpeg_path = find_ffmpeg()

    if not ffmpeg_path:
        print(
            "[version_check] No ffmpeg found on this system. "
            "AutoCompiler needs it to trim and merge clips."
        )
        return

    installed = _parse_ffmpeg_version(ffmpeg_path)

    if not installed:
        return

    try:
        response = requests.get(
            FFMPEG_TAGS_URL,
            timeout=10,
            headers={
                "User-Agent": "video_tool-version-check",
            },
        )
        response.raise_for_status()

        tags = response.json()
        latest = (
            tags[0]["name"].lstrip("n")
            if tags
            else None
        )

    except Exception:
        logger.debug("ffmpeg version check failed", exc_info=True)
        print(
            "Could not check for a newer ffmpeg (no connection to "
            f"GitHub). Using the version you have, {installed}."
        )
        return

    if (
        not latest
        or installed.startswith(latest)
        or latest.startswith(installed)
    ):
        return

    print(
        f"[version_check] ffmpeg {installed} may be behind "
        f"latest ({latest}). ffmpeg is a system binary, not "
        "auto-upgraded here — see "
        "https://ffmpeg.org/download.html if you want to update it."
    )


def check_dependencies(
    auto_upgrade_ytdlp: bool = True,
    interval_hours: float = CHECK_INTERVAL_HOURS,
) -> None:
    """Check dependencies when their daily check is due."""

    state = _load_state()
    ran_any = False

    if _due_for_check(
        state,
        "yt_dlp_checked_at",
        interval_hours,
    ):
        _check_yt_dlp(auto_upgrade=auto_upgrade_ytdlp)
        state["yt_dlp_checked_at"] = datetime.now().isoformat()
        ran_any = True

    if _due_for_check(
        state,
        "ffmpeg_checked_at",
        interval_hours,
    ):
        _check_ffmpeg()
        state["ffmpeg_checked_at"] = datetime.now().isoformat()
        ran_any = True

    if ran_any:
        _save_state(state)

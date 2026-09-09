"""Keeps yt-dlp and ffmpeg from silently going stale.

yt-dlp breaks against YouTube fairly often and gets patched fast, so it's
worth auto-upgrading — it's a normal pip package inside the project's own
venv, so upgrading it is a standard, reversible action (`pip install
--upgrade` any time undoes it).

ffmpeg is different: it's a system binary, not something pip manages, and
replacing it silently could affect other things on the machine outside
this project's control. So ffmpeg gets checked and reported, never
auto-replaced — the person decides if and how to update it.

Both checks are gated to run at most once a day (state kept in
VIDEO_TOOL_CACHE_DIR), so normal runs pay no extra startup cost. Any network
failure here is swallowed and logged softly — a version check should never
be the reason a compile fails.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta

import requests

from .core import VIDEO_TOOL_CACHE_DIR, find_ffmpeg

CHECK_INTERVAL_HOURS = 24
STATE_PATH = VIDEO_TOOL_CACHE_DIR / "version_check.json"
YT_DLP_RELEASES_URL = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
FFMPEG_TAGS_URL = "https://api.github.com/repos/FFmpeg/FFmpeg/tags"


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _due_for_check(state: dict, key: str, interval_hours: float) -> bool:
    last = state.get(key)
    if not last:
        return True
    try:
        last_time = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now() - last_time > timedelta(hours=interval_hours)


def _check_yt_dlp(auto_upgrade: bool = True) -> None:
    import yt_dlp
    installed = yt_dlp.version.__version__

    try:
        resp = requests.get(YT_DLP_RELEASES_URL, timeout=10,
                             headers={"User-Agent": "video_tool-version-check"})
        resp.raise_for_status()
        latest = resp.json()["tag_name"].lstrip("v")
    except Exception as e:
        print(f"[version_check] Could not reach GitHub to check yt-dlp version "
              f"({e}). Continuing with installed version {installed}.")
        return

    if installed == latest:
        return

    print(f"[version_check] yt-dlp {installed} is behind latest ({latest}).")
    if not auto_upgrade:
        print("[version_check] Run: pip install --upgrade yt-dlp")
        return

    print("[version_check] Upgrading yt-dlp...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"[version_check] yt-dlp upgraded: {installed} -> {latest}")
    else:
        print(f"[version_check] yt-dlp upgrade failed, continuing with {installed}:\n"
              f"{result.stderr.strip()}")


def _parse_ffmpeg_version(ffmpeg_path: str) -> str | None:
    try:
        result = subprocess.run([ffmpeg_path, "-version"], capture_output=True,
                                 text=True, timeout=10)
        m = re.search(r"ffmpeg version (\S+)", result.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def _check_ffmpeg() -> None:
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        print("[version_check] No ffmpeg found on this system. AutoCompiler "
              "needs it to trim and merge clips.")
        return

    installed = _parse_ffmpeg_version(ffmpeg_path)
    if not installed:
        return  # couldn't parse; not worth failing over

    try:
        resp = requests.get(FFMPEG_TAGS_URL, timeout=10,
                             headers={"User-Agent": "video_tool-version-check"})
        resp.raise_for_status()
        tags = resp.json()
        latest = tags[0]["name"].lstrip("n") if tags else None
    except Exception as e:
        print(f"[version_check] Could not reach GitHub to check ffmpeg version "
              f"({e}). Continuing with installed version {installed}.")
        return

    if not latest or installed.startswith(latest) or latest.startswith(installed):
        return

    print(f"[version_check] ffmpeg {installed} may be behind latest ({latest}). "
          f"ffmpeg is a system binary, not auto-upgraded here — see "
          f"https://ffmpeg.org/download.html if you want to update it.")


def check_dependencies(auto_upgrade_ytdlp: bool = True,
                        interval_hours: float = CHECK_INTERVAL_HOURS) -> None:
    """Call once per AutoCompiler run. Skips entirely if checked recently."""
    state = _load_state()
    ran_any = False

    if _due_for_check(state, "yt_dlp_checked_at", interval_hours):
        _check_yt_dlp(auto_upgrade=auto_upgrade_ytdlp)
        state["yt_dlp_checked_at"] = datetime.now().isoformat()
        ran_any = True

    if _due_for_check(state, "ffmpeg_checked_at", interval_hours):
        _check_ffmpeg()
        state["ffmpeg_checked_at"] = datetime.now().isoformat()
        ran_any = True

    if ran_any:
        _save_state(state)

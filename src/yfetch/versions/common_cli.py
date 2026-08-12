"""Shared command-line helpers used by every YFetch version."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from ..core import is_valid_youtube_url


def parse_urls(values: Iterable[str]) -> list[str]:
    """Parse comma/space/newline-separated YouTube URLs."""
    urls: list[str] = []
    for value in values:
        for line in value.splitlines():
            for part in line.replace(",", " ").split():
                part = part.strip()
                if part and is_valid_youtube_url(part):
                    urls.append(part)
    return urls


def prompt_format(default: str = "MP4") -> str:
    """Ask for the output format when the CLI flag was not supplied."""
    default = default.upper()
    print()
    print("Select output format:")
    print("  1) MP4 (video)")
    print("  2) MP3 (audio)")
    print(f"Press Enter for {default}.")

    while True:
        choice = input("Format [1/2]: ").strip().lower()
        if not choice:
            return default
        if choice in {"1", "mp4"}:
            return "MP4"
        if choice in {"2", "mp3"}:
            return "MP3"
        print("Invalid choice. Enter 1 for MP4 or 2 for MP3.")


def add_common_download_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments shared by Basic and Pro."""
    parser.add_argument("urls", nargs="+", help="YouTube URL(s)")
    parser.add_argument(
        "-o", "--output",
        default=str(Path.home() / "Downloads" / "yfetch"),
        help="Output directory",
    )
    parser.add_argument(
        "-f", "--format",
        dest="format_type",
        choices=["MP4", "MP3"],
        default=None,
        help="Output format. If omitted, ask interactively.",
    )
    parser.add_argument(
        "-q", "--quality",
        choices=["Best", "1080p", "720p", "480p", "360p"],
        default="Best",
        help="Video quality",
    )
    parser.add_argument(
        "--cookie-browser",
        choices=["chrome", "firefox", "edge", "safari", "brave", "opera", "none"],
        default=None,
        help="Browser used for YouTube cookies",
    )
    parser.add_argument(
        "--cookie-file",
        help="Netscape cookies.txt file. If omitted, project-root youtube_cookies.txt is used when present.",
    )

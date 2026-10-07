
"""Shared command-line argument and input helpers."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path

from ..core import is_valid_youtube_url
from ..sources import detect_source


_LINK_EDGE = " \t\r\n\"'\u201c\u201d\u2018\u2019<>"


def clean_url(value: str) -> str:
    """Remove spaces, quote marks and angle brackets around a link."""

    return value.strip(_LINK_EDGE)


def parse_urls(values: Iterable[str]) -> list[str]:
    """Parse YouTube URLs from command-line values.

    This remains backward compatible with the original YouTube-only
    behaviour: URLs may be supplied as separate arguments or separated
    by commas, spaces, or newlines.
    """

    urls: list[str] = []

    for value in values:
        value = value.strip()

        if not value:
            continue

        for line in value.splitlines():
            for part in line.replace(",", " ").split():
                part = clean_url(part)

                if part and is_valid_youtube_url(part):
                    urls.append(part)

    return urls


def parse_sources(values: Iterable[str]) -> list[str]:
    """Parse YouTube URLs and existing local video files.

    Local paths are checked as complete arguments first so paths containing
    spaces can be supplied normally when quoted by the shell.

    YouTube URLs retain the existing convenience of being supplied as
    comma-, space-, or newline-separated values.

    Unsupported URLs and other unknown values are ignored.
    """

    sources: list[str] = []

    for value in values:
        value = value.strip()

        if not value:
            continue

        # First check the complete argument. This is important for local
        # filenames containing spaces, for example:
        #
        #     "My Match Footage/episode 1.mp4"
        #
        # The shell gives that to argparse as one argument.
        detected = detect_source(value)

        if detected.is_local or detected.is_youtube:
            sources.append(detected.ref)
            continue

        # If the complete argument was not a recognised source, retain
        # the original convenience of accepting multiple YouTube URLs
        # separated by spaces, commas, or newlines.
        for line in value.splitlines():
            for part in line.replace(",", " ").split():
                part = clean_url(part)

                if not part:
                    continue

                split_detected = detect_source(part)

                if split_detected.is_local or split_detected.is_youtube:
                    sources.append(split_detected.ref)

    return sources


def prompt_format() -> str:
    """Prompt the user for an output format."""

    while True:
        value = input("Format (MP4/MP3) [MP4]: ").strip().upper()

        if not value:
            return "MP4"

        if value in {"MP4", "MP3"}:
            return value

        print("Please enter MP4 or MP3.")


def add_common_download_arguments(
    parser: argparse.ArgumentParser,
    *,
    positional_name: str = "urls",
    positional_help: str = "YouTube URL(s)",
) -> argparse.ArgumentParser:
    """Add arguments shared by the video download tools."""

    parser.add_argument(
        positional_name,
        nargs="*",
        help=positional_help,
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("~/Downloads/video-tool").expanduser(),
        help="Output directory.",
    )

    parser.add_argument(
        "-f",
        "--format",
        dest="format_type",
        choices=("MP4", "MP3"),
        type=str.upper,
        help="Output format.",
    )

    parser.add_argument(
        "-q",
        "--quality",
        default="best",
        help="Download quality.",
    )

    parser.add_argument(
        "--cookies-from-browser",
        dest="cookie_browser",
        choices=(
            "chrome",
            "chromium",
            "edge",
            "firefox",
            "opera",
            "safari",
            "brave",
            "vivaldi",
        ),
        help="Use cookies from a browser.",
    )

    parser.add_argument(
        "--cookies",
        dest="cookie_file",
        type=Path,
        help="Use cookies from a Netscape-format cookie file.",
    )

    return parser
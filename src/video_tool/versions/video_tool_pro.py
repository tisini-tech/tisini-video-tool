
"""Video Tool Pro command-line application.

Pro supports:

- YouTube URL range extraction.
- Local video range extraction.
- Full YouTube downloads followed by local trimming.
- Cached YouTube source reuse.
- Configurable browser/cookie authentication.
- Automatic retry of failed downloads.
- Optional preservation of downloaded originals.

Local source files are always preserved.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..clip_source import (
    DEFAULT_ATTEMPTS,
    download_range,
    expected_length,
    extract_local_clip,
    fetch_clip,
    parse_segment,
    report_fetch_failure,
)
from ..core import (
    load_download_count,
    save_download_count,
)
from ..sources import detect_source
from ..youtube import DownloadOptions
from .common_cli import (
    add_common_download_arguments,
    parse_sources,
    prompt_format,
)


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 130


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

_parse_segment = parse_segment
_expected_length = expected_length
_download_range = download_range
_report_failure = report_fetch_failure


def _positive_int(value: str) -> int:
    """Argparse validator for positive integers."""

    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an integer"
        ) from exc

    if number < 1:
        raise argparse.ArgumentTypeError(
            "must be greater than zero"
        )

    return number


def build_parser() -> argparse.ArgumentParser:
    """Build the Pro command-line parser."""

    parser = argparse.ArgumentParser(
        prog="vt-pro",
        description=(
            "Extract a section from YouTube videos or local video files."
        ),
    )

    add_common_download_arguments(
        parser,
        positional_name="sources",
        positional_help="YouTube URL(s) or local video file(s)",
    )

    parser.add_argument(
        "--start",
        help="Start time, for example 00:35:20.",
    )

    parser.add_argument(
        "--end",
        help="End time, for example 00:37:45.",
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not use or update the YouTube source cache.",
    )

    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="Keep a full YouTube download after local trimming.",
    )

    parser.add_argument(
        "--full-download",
        action="store_true",
        help=(
            "Download the complete YouTube source before trimming "
            "instead of requesting only the selected range."
        ),
    )

    parser.add_argument(
        "--attempts",
        type=_positive_int,
        default=DEFAULT_ATTEMPTS,
        help=f"Number of download attempts (default: {DEFAULT_ATTEMPTS}).",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show additional technical information.",
    )

    return parser


def _build_download_options(
    args: argparse.Namespace,
    output_dir: Path,
    format_type: str,
) -> DownloadOptions:
    """Create DownloadOptions from parsed CLI arguments."""

    return DownloadOptions(
        output_dir=str(output_dir),
        format_type=format_type,
        quality=args.quality,
        cookie_browser=args.cookie_browser,
        cookie_file=args.cookie_file,
    )


def _increment_download_count() -> None:
    """Increment the persistent count of newly downloaded sources."""

    count = load_download_count()
    save_download_count(count + 1)


def _run(args: argparse.Namespace) -> int:
    """Run the Pro application."""

    sources = parse_sources(args.sources)

    if not sources:
        print(
            "No valid sources found. "
            "Provide a YouTube URL or an existing local video file."
        )
        return EXIT_USAGE

    try:
        start, end = parse_segment(
            args.start,
            args.end,
        )
    except ValueError as exc:
        print(f"Invalid time range: {exc}")
        return EXIT_USAGE

    format_type = args.format_type

    if format_type is None:
        format_type = prompt_format()

    output_dir = Path(args.output).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    options = _build_download_options(
        args,
        output_dir,
        format_type,
    )

    successful = 0
    total = len(sources)

    for source in sources:
        try:
            detected = detect_source(source)

            if detected.is_local:
                path = extract_local_clip(
                    Path(detected.ref),
                    output_dir,
                    start,
                    end,
                    format_type=format_type,
                )
                downloaded = False

            elif detected.is_youtube:
                path, downloaded = fetch_clip(
                    detected.ref,
                    output_dir,
                    start,
                    end,
                    options,
                    no_cache=args.no_cache,
                    keep_original=args.keep_original,
                    full_download=args.full_download,
                    attempts=args.attempts,
                )

            else:
                raise ValueError(
                    f"Unsupported source: {source}"
                )

        except KeyboardInterrupt:
            raise

        except Exception as exc:
            if args.verbose:
                print(
                    f"[verbose] {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

            report_fetch_failure(
                source,
                exc,
            )
            continue

        print(f"Saved: {path}")

        if downloaded:
            try:
                _increment_download_count()
            except Exception as exc:
                if args.verbose:
                    print(
                        f"[verbose] Could not update download count: {exc}",
                        file=sys.stderr,
                    )

        successful += 1

    print(
        f"Completed {successful}/{total} source"
        f"{'' if total == 1 else 's'}."
    )

    if successful == total:
        return EXIT_OK

    return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    parser = build_parser()

    try:
        args = parser.parse_args(argv)
        return _run(args)

    except KeyboardInterrupt:
        print("\nCancelled.")
        return EXIT_CANCELLED


if __name__ == "__main__":
    raise SystemExit(main())
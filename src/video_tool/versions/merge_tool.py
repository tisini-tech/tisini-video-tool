
"""Merge local video files and/or YouTube URLs into one file.

Local paths are used as-is. YouTube URLs are resolved through
clip_source.fetch_clip(), then all resolved clips are passed to the
shared core.merge_clips() implementation.

Merge strategy:
    1. Try stream-copy concatenation.
    2. Validate the result.
    3. If stream-copy is unsuitable, silently fall back to re-encoding.
    4. Report only the final method used.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ..clip_source import (
    DEFAULT_ATTEMPTS,
    fetch_clip,
    parse_segment,
)
from ..core import (
    VideoToolError,
    find_ffmpeg,
    is_valid_youtube_url,
    merge_clips,
)
from ..errors import report_error
from ..youtube import DownloadOptions

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 130


def _positive_int(text: str) -> int:
    """Validate a positive integer CLI argument."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a whole number"
        ) from None

    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")

    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the merge command-line parser."""
    parser = argparse.ArgumentParser(
        prog="vt-merge",
        description=(
            "Merge video clips. Each source may be a local file or a YouTube URL."
        ),
    )

    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output video path",
    )

    parser.add_argument(
        "--crf",
        type=int,
        default=18,
        help="CRF used only if automatic re-encoding is required",
    )

    parser.add_argument(
        "-q",
        "--quality",
        choices=["Best", "1080p", "720p", "480p", "360p"],
        default="Best",
        help="YouTube download quality (ignored for local files)",
    )

    parser.add_argument(
        "--start",
        help="Trim start for YouTube sources only",
    )

    parser.add_argument(
        "--end",
        help="Trim end for YouTube sources only",
    )

    parser.add_argument(
        "--full-download",
        action="store_true",
        help="For YouTube ranges: download the whole video, then cut locally",
    )

    parser.add_argument(
        "--attempts",
        type=_positive_int,
        default=DEFAULT_ATTEMPTS,
        help="Retries for a YouTube range download that fails or comes out short",
    )

    parser.add_argument(
        "--cookie-browser",
        choices=[
            "chrome",
            "firefox",
            "edge",
            "safari",
            "brave",
            "opera",
            "none",
        ],
        default=None,
        help="Browser used for YouTube cookies",
    )

    parser.add_argument(
        "--cookie-file",
        help=(
            "Netscape cookies.txt file. If omitted, project-root "
            "youtube_cookies.txt is used when present."
        ),
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not reuse a cached full YouTube download",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show technical detail for troubleshooting",
    )

    parser.add_argument(
        "videos",
        nargs="+",
        help="Local video files and/or YouTube URLs, in merge order",
    )

    return parser


def resolve_source(
    item: str,
    options: DownloadOptions,
    staging_dir: Path,
    *,
    start: float | None,
    end: float | None,
    use_cache: bool,
    full_download: bool,
    attempts: int,
) -> str:
    """Resolve one merge input into a local file path."""
    local = Path(item).expanduser()

    if local.is_file():
        print(f"Local file: {local}")
        return str(local.resolve())

    if is_valid_youtube_url(item):
        print(f"YouTube: {item}")

        path, _ = fetch_clip(
            item,
            staging_dir,
            start,
            end,
            options,
            no_cache=not use_cache,
            full_download=full_download,
            keep_original=True,
            attempts=attempts,
        )

        return str(path)

    raise VideoToolError(
        f"Not a local video file and not a YouTube URL: {item}"
    )


def _run(args: argparse.Namespace) -> int:
    """Execute the merge command."""
    if len(args.videos) < 2:
        print("Error: merge needs at least two sources.")
        return EXIT_USAGE

    try:
        start, end = parse_segment(args.start, args.end)
    except ValueError as exc:
        print(f"Error: {exc}")
        return EXIT_USAGE

    ffmpeg = find_ffmpeg()

    if not ffmpeg:
        print(
            "Error: ffmpeg is needed to merge videos, but it was not found. "
            "Install ffmpeg and make sure it is on your PATH."
        )
        return EXIT_FAILED

    output = Path(args.output).expanduser()
    staging = output.parent / ".video-tool-merge"

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report_error(exc, str(output.parent))
        return EXIT_FAILED

    options = DownloadOptions(
        output_dir=str(staging),
        format_type="MP4",
        quality=args.quality,
        cookie_browser=args.cookie_browser,
        cookie_file=args.cookie_file,
        bypass_no_auth=True,
    )

    clips: list[str] = []

    for index, item in enumerate(args.videos, 1):
        print(f"Source {index}/{len(args.videos)}: {item}")

        try:
            clip = resolve_source(
                item,
                options,
                staging,
                start=start,
                end=end,
                use_cache=not args.no_cache,
                full_download=args.full_download,
                attempts=args.attempts,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            report_error(exc, item)

            print(
                "Merge stopped. Every source has to work "
                "before the videos can be joined."
            )
            return EXIT_FAILED

        clips.append(clip)

    try:
        output_path = merge_clips(
            ffmpeg_path=ffmpeg,
            clip_paths=clips,
            output_path=str(output),
            re_encode=False,
            crf=args.crf,
            preset="slow",
        )

    except KeyboardInterrupt:
        raise

    except Exception as exc:
        logger.debug("Merge failed", exc_info=True)
        report_error(exc, verbose=args.verbose)
        return EXIT_FAILED

    print(f"Merged {len(clips)} videos: {output_path}")

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run the merge command."""
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return _run(args)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return EXIT_CANCELLED


if __name__ == "__main__":
    raise SystemExit(main())

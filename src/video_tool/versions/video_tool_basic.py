"""Video Tool Basic: the lightweight command-line downloader.

Basic deliberately keeps a small CLI surface while delegating all source
fetching, caching, and YouTube downloading to the shared source layer.

Unlike Pro, Basic does not expose trimming or range-download options.
It downloads complete YouTube videos/audio files.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..clip_source import fetch_clip
from ..errors import report_error
from ..core import (
    load_download_count,
    save_download_count,
)
from ..youtube import DownloadOptions
from .common_cli import (
    add_common_download_arguments,
    parse_urls,
    prompt_format,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the Basic CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="video_tool-basic",
        description="Download YouTube videos or audio.",
    )
    add_common_download_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the Basic downloader."""
    args = build_parser().parse_args(argv)

    urls = parse_urls(args.urls)

    if not urls:
        print("No valid YouTube URLs found.")
        return 1

    output = Path(args.output).expanduser()

    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report_error(exc, str(output))
        return 1

    try:
        format_type = args.format_type or prompt_format()
    except EOFError:
        print(
            "No format given and no terminal to ask on. "
            "Use -f MP4 or -f MP3."
        )
        return 1

    options = DownloadOptions(
        output_dir=str(output),
        format_type=format_type,
        quality=args.quality,
        cookie_browser=args.cookie_browser,
        cookie_file=args.cookie_file,
    )

    count = load_download_count()
    successful = 0

    for index, url in enumerate(urls, 1):
        print(f"Processing {index}/{len(urls)}: {url}")

        try:
            path, downloaded = fetch_clip(
                url,
                output,
                None,
                None,
                options,
                full_download=True,
                keep_original=True,
                cache_directories=[output],
            )
        except Exception as exc:
            report_error(exc, url)
            continue

        print(f"Saved: {path}")

        # Count real downloads, not cache reuses.
        if downloaded:
            count += 1
            save_download_count(count)

        successful += 1

    print(f"Completed: {successful}/{len(urls)}")
    print(f"Total downloads: {count}")

    return 0 if successful == len(urls) else 1


if __name__ == "__main__":
    raise SystemExit(main())
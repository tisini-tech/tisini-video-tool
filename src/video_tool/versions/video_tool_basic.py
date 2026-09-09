"""Video Tool Basic: the lightweight command-line downloader.
Download behaviour is delegated to the shared YouTube module so Basic and Pro cannot drift apart.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..core import (
    add_to_cache,
    extract_video_id,
    load_cache,
    load_download_count,
    resolve_cached_file,
    save_download_count,
)
from ..youtube import DownloadOptions, download_url
from .common_cli import add_common_download_arguments, parse_urls, prompt_format


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video_tool-basic",
        description="Download YouTube videos or audio.",
    )
    add_common_download_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    urls = parse_urls(args.urls)

    if not urls:
        print("No valid YouTube URLs found.")
        return 1

    output = Path(args.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)

    format_type = args.format_type or prompt_format()
    options = DownloadOptions(
        output_dir=str(output),
        format_type=format_type,
        quality=args.quality,
        cookie_browser=args.cookie_browser,
        cookie_file=args.cookie_file,
    )

    cache = load_cache()
    count = load_download_count()
    successful = 0

    for index, url in enumerate(urls, 1):
        print(f"Processing {index}/{len(urls)}: {url}")
        try:
            path = resolve_cached_file(
                url,
                format_type,
                cache=cache,
                directories=[str(output)],
            )
            if path:
                print(f"Using cached/local file: {path}")
            else:
                path = download_url(url, options)
                if not path:
                    raise RuntimeError("Could not locate downloaded file")
                video_id = extract_video_id(url)
                if video_id:
                    add_to_cache(cache, video_id, format_type, path)
                print(f"Saved: {path}")
            count += 1
            successful += 1
            save_download_count(count)
        except Exception as exc:
            print(f"Download failed: {exc}")

    print(f"Completed: {successful}/{len(urls)}")
    print(f"Total downloads: {count}")
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())

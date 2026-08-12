"""YFetch Pro command-line version.

Pro keeps the original additions over Basic: caching, segment trimming,
configurable browser authentication, and automatic deletion of the original
when requested.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..core import (
    add_to_cache,
    get_cached_path,
    resolve_cached_file,
    load_cache,
    load_config,
    load_download_count,
    find_ffmpeg,
    extract_video_id,
    parse_time_to_seconds,
    save_config,
    save_download_count,
    seconds_to_timestamp,
    trim_clip,
)
from ..youtube import DownloadOptions, download_url
from .common_cli import add_common_download_arguments, parse_urls, prompt_format

CONFIG_FILE = "yfetch_pro_config.json"


def _load_pro_config() -> dict:
    config = load_config()
    # The shared core historically points at the Pro state filename.
    # Keep the filename explicit here so the version remains self-contained.
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, json.JSONDecodeError):
            pass
    return config


def _save_pro_config(config: dict) -> None:
    save_config(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yfetch-pro",
        description="YFetch Pro command-line downloader with cache and trimming.",
    )
    add_common_download_arguments(parser)
    parser.add_argument("--start", help="Trim start time: seconds, MM:SS, or HH:MM:SS")
    parser.add_argument("--end", help="Trim end time: seconds, MM:SS, or HH:MM:SS")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not use an existing cached video",
    )
    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="Keep the downloaded original after trimming",
    )
    return parser


def _parse_segment(start_text: str | None, end_text: str | None) -> tuple[float | None, float | None]:
    start = parse_time_to_seconds(start_text)
    end = parse_time_to_seconds(end_text)

    if start is not None and start < 0:
        raise ValueError("Start time cannot be negative")
    if end is not None and start is not None and end <= start:
        raise ValueError("End time must be greater than start time")

    return start, end


def _trim_if_requested(
    path: str,
    output_dir: Path,
    start: float | None,
    end: float | None,
    *,
    delete_original: bool,
) -> str:
    if start is None and end is None:
        return path

    base = Path(path).stem
    extension = Path(path).suffix
    start_label = seconds_to_timestamp(start) if start else "start"
    end_label = seconds_to_timestamp(end) if end else "end"
    trimmed = output_dir / f"{base}_trim_{start_label}-{end_label}{extension}"

    trim_clip(
        find_ffmpeg(),
        path,
        str(trimmed),
        start,
        end,
        resolution="1080p",
        bitrate_kbps=4000,
        fps=30,
        crf=18,
    )

    if delete_original:
        os.remove(path)

    return str(trimmed)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    urls = parse_urls(args.urls)

    if not urls:
        print("No valid YouTube URLs found.")
        return 1

    start, end = _parse_segment(args.start, args.end)
    format_type = args.format_type or prompt_format()
    output = Path(args.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)

    config = _load_pro_config()
    config.update({
        "last_folder": str(output),
        "default_format": format_type,
        "use_cache": not args.no_cache,
        "cookie_browser": args.cookie_browser or config.get("cookie_browser", "None"),
    })
    _save_pro_config(config)

    options = DownloadOptions(
        output_dir=str(output),
        format_type=format_type,
        quality=args.quality,
        cookie_browser=args.cookie_browser,
        cookie_file=args.cookie_file,
        bypass_no_auth=True,
    )

    count = load_download_count()
    successful = 0

    for index, url in enumerate(urls, 1):
        video_id = extract_video_id(url)
        path = None
        is_from_cache = False

        if not args.no_cache:
            path = resolve_cached_file(
                url,
                format_type,
                cache=load_cache(),
                directories=[str(output)],
            )

        print(f"Processing {index}/{len(urls)}: {url}")

        try:
            if path:
                is_from_cache = True
                print(f"Using cached file: {path}")
            else:
                path = download_url(url, options)
                if not path:
                    raise RuntimeError("Could not locate downloaded file")

                if video_id:
                    cache = load_cache()
                    add_to_cache(cache, video_id, format_type, path)

            path = _trim_if_requested(
                path,
                output,
                start,
                end,
                delete_original=not args.keep_original and not is_from_cache,
            )
            print(f"Saved: {path}")
            count += 1
            successful += 1
            save_download_count(count)

        except Exception as exc:
            print(f"Failed: {exc}")

    print(f"Completed: {successful}/{len(urls)}")
    print(f"Total downloads: {count}")
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())

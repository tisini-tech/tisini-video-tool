"""One function per tool. Each job works in its own folder.

The API never reads or writes the shared download cache or the download
counter, so it cannot disturb the CLI or the GUI.
"""

from __future__ import annotations

from pathlib import Path

from ..clip_source import fetch_clip
from ..youtube import DownloadOptions
from .security import Settings


def make_runners(settings: Settings) -> dict:
    def run_basic(params: dict, folder: Path, progress) -> Path:
        def hook(data: dict) -> None:
            if data.get("status") != "downloading":
                return

            total = data.get("total_bytes") or data.get("total_bytes_estimate")

            if total:
                progress(min(data.get("downloaded_bytes", 0) / total, 0.99))

        options = DownloadOptions(
            output_dir=str(folder),
            format_type=params["format"],
            quality=params["quality"],
            cookie_file=settings.cookie_file,
            progress_hook=hook,
        )

        path, _ = fetch_clip(
            params["url"],
            folder,
            None,
            None,
            options,
            full_download=True,
            keep_original=True,
            no_cache=True,
        )

        return path

    return {"basic": run_basic}

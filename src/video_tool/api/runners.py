"""One function per tool. Each job works in its own folder.

The API never reads or writes the shared download cache or the download
counter, so it cannot disturb the CLI or the GUI.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..clip_source import fetch_clip
from ..core import find_ffmpeg, merge_clips
from ..youtube import DownloadOptions
from .security import Settings

# Re-encode settings, used only when the clips cannot be joined losslessly.
# The command line merge uses "slow"; a server cannot wait that long.
MERGE_CRF = 20
MERGE_PRESET = "veryfast"

# Share of a merge job's progress bar given to the downloads. The rest is
# the join itself.
DOWNLOAD_SHARE = 0.9


def _mark_clip(exc: Exception, number: int) -> None:
    """Remember which clip failed. The job store puts it in the error."""

    try:
        exc.clip_number = number  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


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

    def run_merge(params: dict, folder: Path, progress) -> Path:
        # Each source is {"url": link} to download, or {"path": file} for
        # an upload the job store already put on disk.
        sources = params["sources"]
        count = len(sources)
        clips: list[str] = []
        clip_folders: list[Path] = []

        def hook_for(index: int):
            def hook(data: dict) -> None:
                if data.get("status") != "downloading":
                    return

                total = (
                    data.get("total_bytes")
                    or data.get("total_bytes_estimate")
                )

                if total:
                    part = min(data.get("downloaded_bytes", 0) / total, 0.99)
                    progress(DOWNLOAD_SHARE * (index + part) / count)

            return hook

        # Every clip gets its own folder, so two links with the same title
        # can never overwrite each other.
        for index, source in enumerate(sources):
            if "path" in source:
                # Joined where it lies. The job store deletes it later.
                clips.append(source["path"])
                progress(DOWNLOAD_SHARE * (index + 1) / count)
                continue

            url = source["url"]
            clip_folder = folder / f"clip{index + 1}"
            clip_folders.append(clip_folder)

            options = DownloadOptions(
                output_dir=str(clip_folder),
                format_type="MP4",
                quality=params["quality"],
                cookie_file=settings.cookie_file,
                progress_hook=hook_for(index),
            )

            try:
                path, _ = fetch_clip(
                    url,
                    clip_folder,
                    None,
                    None,
                    options,
                    full_download=True,
                    keep_original=True,
                    no_cache=True,
                )
            except Exception as exc:
                _mark_clip(exc, index + 1)
                raise

            clips.append(str(path))
            progress(DOWNLOAD_SHARE * (index + 1) / count)

        output = folder / "merged.mp4"

        merged = merge_clips(
            ffmpeg_path=find_ffmpeg(),
            clip_paths=clips,
            output_path=str(output),
            re_encode=False,
            crf=MERGE_CRF,
            preset=MERGE_PRESET,
        )

        # The finished file is all the caller needs. Free the disk space.
        for clip_folder in clip_folders:
            shutil.rmtree(clip_folder, ignore_errors=True)

        return Path(merged)

    return {"basic": run_basic, "merge": run_merge}

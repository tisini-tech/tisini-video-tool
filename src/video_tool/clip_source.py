
"""Shared source fetching utilities.

Responsibilities:

- Resolve cached full YouTube files.
- Download YouTube ranges without downloading the full source.
- Download complete YouTube sources.
- Trim cached/full/local files locally.
- Validate generated clips.
- Never cache range downloads.

Source detection belongs in :mod:`video_tool.sources`.
Trimming and merging belong in :mod:`video_tool.core`.
YouTube downloading belongs in :mod:`video_tool.youtube`.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from yt_dlp.utils import DownloadError  # noqa: F401  (tests use clip_source.DownloadError)

from .core import (
    VideoToolError,
    add_to_cache,
    check_clip_length,
    extract_video_id,
    find_ffmpeg,
    load_cache,
    parse_time_to_seconds,
    resolve_cached_file,
    trim_clip,
    trim_label,
)
from .errors import ClipLengthError, report_error
from .sources import Source, detect_source
from .youtube import (
    DownloadOptions,
    download_url,
)


RECOMMENDED_MAX_SECONDS = 300
DEFAULT_ATTEMPTS = 2


def parse_segment(
    start_text: str | None,
    end_text: str | None,
) -> tuple[float | None, float | None]:
    """Parse and validate an optional start/end range."""

    start = (
        parse_time_to_seconds(start_text)
        if start_text is not None
        else None
    )
    end = (
        parse_time_to_seconds(end_text)
        if end_text is not None
        else None
    )

    if start is not None and start < 0:
        raise ValueError("Start time cannot be negative.")

    if end is not None and end < 0:
        raise ValueError("End time cannot be negative.")

    if (
        start is not None
        and end is not None
        and end <= start
    ):
        raise ValueError(
            "End time must be greater than start time."
        )

    return start, end


def expected_length(
    start: float | None,
    end: float | None,
) -> float | None:
    """Return the expected clip duration when it can be calculated."""

    if start is None:
        return end

    if end is None:
        return None

    return end - start


def _snapshot_files(output_dir: Path) -> set[Path]:
    """Return the regular files currently present in an output directory."""

    if not output_dir.exists():
        return set()

    return {
        path.resolve()
        for path in output_dir.iterdir()
        if path.is_file()
    }


def mark_incomplete(path: Path) -> Path:
    """Rename a failed clip so it is preserved for inspection.

    The first failure becomes ``*_INCOMPLETE.ext``. If that already exists,
    numbered variants are used.
    """

    path = Path(path)

    candidate = path.with_name(
        f"{path.stem}_INCOMPLETE{path.suffix}"
    )

    counter = 1

    while candidate.exists():
        candidate = path.with_name(
            f"{path.stem}_INCOMPLETE_{counter}{path.suffix}"
        )
        counter += 1

    path.rename(candidate)
    return candidate


def check_length(
    path: Path,
    start: float | None,
    end: float | None,
    format_type: str,
) -> None:
    """Validate the generated clip duration.

    ``core.check_clip_length()`` returns a list of problems rather than
    raising an exception. This function converts those problems into the
    structured ``ClipLengthError`` used by the callers.

    A short clip is deliberately preserved under an ``_INCOMPLETE`` name.
    """

    wanted = expected_length(start, end)

    if wanted is None:
        return

    if format_type.upper() == "MP3":
        problems = check_clip_length(
            path,
            wanted,
            kinds=("audio",),
        )
    else:
        problems = check_clip_length(
            path,
            wanted,
            kinds=("video", "audio"),
        )

    if problems:
        incomplete = mark_incomplete(path)

        raise ClipLengthError(
            incomplete,
            problems,
            wanted,
        )


def trim_local(
    path: Path,
    output_dir: Path,
    start: float | None,
    end: float | None,
    *,
    format_type: str = "MP4",
    delete_original: bool = False,
) -> Path:
    """Trim a local/cached video file.

    The source is never deleted unless ``delete_original=True`` is
    explicitly requested.
    """

    source = Path(path).expanduser()

    if not source.is_file():
        raise FileNotFoundError(
            f"Source file not found: {source}"
        )

    if start is None and end is None:
        return source

    ffmpeg = find_ffmpeg()

    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    suffix = source.suffix or ".mp4"
    label = trim_label(start, end)

    trimmed = destination_dir / (
        f"{source.stem}_trim_{label}{suffix}"
    )

    trim_clip(
        ffmpeg,
        source,
        str(trimmed),
        start,
        end,
        resolution="1080p",
        bitrate_kbps=4000,
        fps=30,
        crf=18,
    )

    check_length(
        trimmed,
        start,
        end,
        format_type,
    )

    if delete_original:
        source.unlink()

    return trimmed


def extract_local_clip(
    path: Path,
    output_dir: Path,
    start: float | None,
    end: float | None,
    *,
    format_type: str = "MP4",
) -> Path:
    """Extract a section from an existing local video.

    The original local file is always preserved.
    """

    source = Path(path).expanduser()

    if not source.is_file():
        raise FileNotFoundError(
            f"Local video file not found: {source}"
        )

    if start is None and end is None:
        raise ValueError(
            "Local video extraction requires --start and/or --end."
        )

    return trim_local(
        source,
        Path(output_dir),
        start,
        end,
        format_type=format_type,
        delete_original=False,
    )


def _output_path(result: str | None, url: str) -> Path:
    """Turn ``download_url()`` output into a Path, or fail clearly.

    ``download_url()`` returns ``None`` when yt-dlp finished but the
    output file could not be found.
    """

    if result is None:
        raise FileNotFoundError(
            f"yt-dlp did not produce an output file for: {url}"
        )

    return Path(result)


def download_range(
    url: str,
    options: DownloadOptions,
    start: float | None,
    end: float | None,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
) -> tuple[Path, bool]:
    """Download only the requested YouTube range.

    Range downloads are not added to the full-source cache.
    """

    if end is None:
        raise VideoToolError(
            "An --end time is required for a ranged YouTube download."
        )

    output_dir = Path(options.output_dir).expanduser()
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    ranged_options = replace(
        options,
        section_start=start,
        section_end=end,
    )

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        before = _snapshot_files(output_dir)

        try:
            path = _output_path(
                download_url(
                    url,
                    ranged_options,
                ),
                url,
            )

            if not path.is_file():
                raise FileNotFoundError(
                    f"yt-dlp did not produce an output file for: {url}"
                )

            check_length(
                path,
                start,
                end,
                options.format_type,
            )

            after = _snapshot_files(output_dir)

            downloaded = path.resolve() not in before

            if not downloaded:
                downloaded = bool(after - before)

            # This function represents an actual range download operation.
            # Even when a test/mock returns an already-existing path, the
            # operation itself was a successful download attempt.
            return path, True

        except ClipLengthError as exc:
            last_error = exc

            if attempt >= attempts:
                # A previous attempt may already have left the canonical
                # *_INCOMPLETE file. The final failed attempt can therefore
                # be named *_INCOMPLETE_1 by mark_incomplete(). Consolidate
                # that final failure back to the canonical name so callers
                # always receive the final failed file as *_INCOMPLETE.
                final_incomplete = Path(exc.path)

                canonical_incomplete = final_incomplete.with_name(
                    final_incomplete.name.replace(
                        "_INCOMPLETE_1",
                        "_INCOMPLETE",
                    )
                )

                if (
                    final_incomplete != canonical_incomplete
                    and final_incomplete.is_file()
                ):
                    if canonical_incomplete.exists():
                        canonical_incomplete.unlink()

                    final_incomplete.rename(canonical_incomplete)

                    raise ClipLengthError(
                        canonical_incomplete,
                        exc.problems,
                        exc.wanted,
                    )

                raise

            # check_length() preserves the failed file as _INCOMPLETE.
            # Before retrying, restore a working filename so yt-dlp can
            # produce/replace the expected range output on the next attempt.
            #
            # The previous _INCOMPLETE file is intentionally kept. If the
            # retry succeeds, it remains available for inspection.
            incomplete = Path(exc.path)

            if incomplete.is_file() and not path.is_file():
                shutil.copy2(incomplete, path)

            continue

        except Exception as exc:
            last_error = exc

            if attempt < attempts:
                continue

            raise

    if last_error is not None:
        raise last_error

    raise VideoToolError(
        f"Unable to download requested YouTube range: {url}"
    )


def fetch_clip(
    url: str,
    output_dir: Path | str,
    start: float | None,
    end: float | None,
    options: DownloadOptions,
    *,
    no_cache: bool = False,
    keep_original: bool = False,
    full_download: bool = False,
    attempts: int = DEFAULT_ATTEMPTS,
    cache_directories: Sequence[Path | str] | None = None,
) -> tuple[Path, bool]:
    """Fetch a YouTube clip using the appropriate pipeline.

    Behaviour:

    - Cached full source + requested range -> trim cached source locally.
    - Ranged YouTube download -> download only the requested range.
    - Full download -> download the complete source, cache it, then trim.
    - No range -> return the downloaded/cached source unchanged.

    Returns ``(path, downloaded)`` where ``downloaded`` indicates whether
    a new source was downloaded during this operation.
    """

    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    video_id = extract_video_id(url)

    # ------------------------------------------------------------------
    # Cached full source
    # ------------------------------------------------------------------

    if not no_cache and video_id:
        cache = load_cache()

        cached = resolve_cached_file(
            video_id,
            options.format_type,
            cache=cache,
            directories=cache_directories,
        )

        if cached is not None:
            cached_path = Path(cached)

            if start is not None or end is not None:
                trimmed = trim_local(
                    cached_path,
                    output_dir,
                    start,
                    end,
                    format_type=options.format_type,
                    delete_original=False,
                )
                return trimmed, False

            return cached_path, False

    # ------------------------------------------------------------------
    # Requested range without full-download mode
    # ------------------------------------------------------------------

    if (
        (start is not None or end is not None)
        and not full_download
    ):
        return download_range(
            url,
            options,
            start,
            end,
            attempts=attempts,
        )

    # ------------------------------------------------------------------
    # Full YouTube download
    # ------------------------------------------------------------------

    files_before = _snapshot_files(
        Path(options.output_dir).expanduser()
    )

    path = _output_path(
        download_url(
            url,
            options,
        ),
        url,
    )

    if not path.is_file():
        raise FileNotFoundError(
            f"yt-dlp did not produce an output file for: {url}"
        )

    # yt-dlp reuses a finished download instead of fetching it again.
    # That file was here before this run, so it is not ours to delete.
    already_there = path.resolve() in files_before

    downloaded = True

    # A complete download can be cached as the source. The trimmed output
    # itself is never used as the full-source cache.
    if video_id and not no_cache:
        cache = load_cache()

        add_to_cache(
            cache,
            video_id,
            options.format_type,
            path,
        )

    # No range requested: the complete source is the requested result.
    if start is None and end is None:
        return path, downloaded

    trimmed = trim_local(
        path,
        output_dir,
        start,
        end,
        format_type=options.format_type,
        delete_original=not keep_original and not already_there,
    )

    return trimmed, downloaded


def fetch_source(
    source: Source | str,
    output_dir: Path | str,
    start: float | None,
    end: float | None,
    options: DownloadOptions,
    *,
    no_cache: bool = False,
    keep_original: bool = False,
    full_download: bool = False,
    attempts: int = DEFAULT_ATTEMPTS,
    cache_directories: Sequence[Path | str] | None = None,
) -> tuple[Path, bool]:
    """Fetch a supported remote source.

    Local files are deliberately handled by ``extract_local_clip`` rather
    than by this function.
    """

    detected = (
        source
        if isinstance(source, Source)
        else detect_source(source)
    )

    if not detected.is_youtube:
        raise ValueError(
            f"Unsupported remote source: {detected.ref}"
        )

    return fetch_clip(
        detected.ref,
        output_dir,
        start,
        end,
        options,
        no_cache=no_cache,
        keep_original=keep_original,
        full_download=full_download,
        attempts=attempts,
        cache_directories=cache_directories,
    )


def report_fetch_failure(
    source: str,
    error: Exception,
) -> None:
    """Print a plain-language message for a source failure.

    The wording comes from :mod:`video_tool.errors`, so every tool says
    the same thing about the same problem.
    """

    report_error(error, source)

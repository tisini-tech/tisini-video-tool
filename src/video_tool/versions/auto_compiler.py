
"""Video Tool Auto-Compiler.

Compiles player or event clips from CSV timestamps.

The compiler coordinates:

    CSV
      ↓
    source detection
      ↓
    source fetching
      ↓
    local trimming
      ↓
    merging
      ↓
    watermark
      ↓
    final compilation

Source resolution and fetching are delegated to the shared source layer.
Video processing is delegated to core.py.

Clip sources
------------
Local files and cached full matches are trimmed locally.

Missing YouTube sources use one of:

* youtube-range — download only each tagged window
* youtube-full  — download the match once, then trim locally

Missing Google Drive sources use:

* gdrive-range — process the requested remote range
* gdrive-full  — download the complete remote source

``fetch_mode='auto'`` picks range when this compile is an event filter,
or when a source has few clips. Otherwise it downloads the match once.

``range`` / ``full`` force one side for YouTube sources.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    import yt_dlp  # noqa: F401
    from yt_dlp.utils import DownloadError

    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

    class DownloadError(Exception):
        """Fallback exception when yt-dlp is unavailable."""

        pass


from ..clip_source import (
    DEFAULT_ATTEMPTS,
    fetch_source,
)
from ..core import (
    MissingToolError,
    VideoToolError,
    apply_watermark,
    build_caption_filter,
    extract_video_id,
    find_caption_font,
    find_ffmpeg,
    load_cache,
    merge_clips,
    read_csv_entries,
    resolve_cached_file,
    trim_clip,
)
from ..csv_prep import get_compiler_csv, write_manifests
from ..errors import report_error
from ..gdrive.parser import GDriveError
from ..gdrive.trimmer import GDriveTrimmer
from ..sources import detect_source
from ..version_check import check_dependencies

logger = logging.getLogger(__name__)


DEFAULT_OUTPUT_FOLDER = os.path.join(
    os.path.expanduser("~"),
    "Downloads",
    "video-tool",
    "Compilations",
)

DEFAULT_DOWNLOAD_FOLDER = os.path.join(
    os.path.expanduser("~"),
    "Downloads",
    "video-tool",
)

# In auto mode, this many clips (or fewer) on one YouTube source uses
# range download instead of fetching the whole match.
RANGE_CLIP_LIMIT = 25


def _positive_int(text: str) -> int:
    """argparse validator for positive integers."""

    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a whole number"
        ) from None

    if value < 1:
        raise argparse.ArgumentTypeError(
            "must be 1 or more"
        )

    return value


def _load_pro_cookie_browser():
    """Load the shared Pro cookie-browser setting."""

    from ..core import load_config

    return load_config().get("cookie_browser")


def detect_source_type(match_id):
    """Backward-compatible wrapper around the shared source detector."""

    source = detect_source(match_id)

    return (
        source.kind,
        source.ref if source.kind != "cache_key" else source.id,
    )


def match_key(match_id: str | None) -> str:
    """Return a stable ID for grouping clips sharing one source."""

    if not match_id:
        return ""

    match_id = match_id.strip()

    return extract_video_id(match_id) or match_id


def count_by_match(entries: list) -> dict[str, int]:
    """Count how many clips belong to each source."""

    counts: dict[str, int] = {}

    for entry in entries:
        key = match_key(entry.get("match_id"))

        counts[key] = counts.get(key, 0) + 1

    return counts


def choose_fetch_strategy(
    *,
    has_local_or_cache: bool,
    is_youtube: bool,
    clips_on_this_source: int,
    event_filter: str | None,
    fetch_mode: str,
    range_clip_limit: int = RANGE_CLIP_LIMIT,
    has_end: bool = True,
    is_gdrive: bool = False,
) -> str:
    """Choose the source strategy for one clip.

    Returns one of:

        local-trim
        youtube-range
        youtube-full
        gdrive-range
        gdrive-full

    A file already on disk always wins.

    Range fetching requires an end time.

    In auto mode:
        event filters → range
        sparse sources → range
        dense sources → full download
    """

    if has_local_or_cache:
        return "local-trim"

    if is_gdrive:
        return (
            "gdrive-range"
            if has_end
            else "gdrive-full"
        )

    if not is_youtube:
        return "local-trim"

    if not has_end:
        return "youtube-full"

    if fetch_mode == "range":
        return "youtube-range"

    if fetch_mode == "full":
        return "youtube-full"

    if event_filter:
        return "youtube-range"

    if clips_on_this_source <= range_clip_limit:
        return "youtube-range"

    return "youtube-full"


def report_compiler_failure(
    exc: Exception,
    source: str | None = None,
) -> None:
    """Print a clip, download, or merge failure without raising.

    The source is optional. Failures such as a merge or a missing FFmpeg
    do not belong to one source.
    """

    report_error(exc, source)


class AutoCompiler:
    """Coordinate CSV clip generation and compilation."""

    def __init__(
        self,
        csv_path,
        output_folder=None,
        resolution="1080p",
        bitrate_kbps=8000,
        fps=30,
        download_missing=True,
        quality="Best",
        dry_run=False,
        keep_temp=False,
        jobs=1,
        cookie_browser=None,
        cookie_file=None,
        bypass_no_auth=True,
        crf=18,
        preset="slow",
        logo_path="assets/tisini-logo.png",
        watermark_position="bottomright",
        watermark_width=180,
        watermark_opacity=0.85,
        watermark_margin=48,
        captions=True,
        caption_font=None,
        caption_position="bottom",
        caption_event=None,
        fetch_mode="auto",
        range_clip_limit=RANGE_CLIP_LIMIT,
        range_attempts=DEFAULT_ATTEMPTS,
    ):
        check_dependencies()

        self.csv_path = get_compiler_csv(csv_path)

        self.output_folder = (
            output_folder
            or DEFAULT_OUTPUT_FOLDER
        )

        self.resolution = resolution
        self.bitrate_kbps = bitrate_kbps
        self.fps = fps

        self.download_missing = bool(
            download_missing
        )

        self.quality = quality
        self.dry_run = dry_run
        self.keep_temp = keep_temp
        self.jobs = max(1, jobs)

        self.bypass_no_auth = bypass_no_auth

        self.crf = crf
        self.preset = preset

        self.logo_path = logo_path
        self.watermark_position = watermark_position
        self.watermark_width = watermark_width
        self.watermark_opacity = watermark_opacity
        self.watermark_margin = watermark_margin

        self.captions = captions
        self.caption_position = caption_position
        self.caption_font = caption_font
        self.caption_event = caption_event

        self.fetch_mode = (
            fetch_mode
            if fetch_mode in {"auto", "range", "full"}
            else "auto"
        )

        self.range_clip_limit = max(
            1,
            int(range_clip_limit),
        )

        self.range_attempts = max(
            1,
            int(range_attempts),
        )

        self.use_cache = True

        self._batch_counts: dict[str, int] = {}

        self.cache = load_cache()

        self.ffmpeg_path = find_ffmpeg()

        self.temp_clips: list[str] = []

        self._stats_lock = threading.Lock()
        self._temp_lock = threading.Lock()

        self.stats = {
            "trimmed": 0,
            "failed": 0,
            "downloaded": 0,
            "local": 0,
            "cached": 0,
            "ranged": 0,
        }

        self.cookie_browser = cookie_browser
        self.cookie_file = cookie_file

        if self.captions and not self.caption_font:
            self.caption_font = find_caption_font()

            if self.caption_font:
                print(
                    f"Captions: using font "
                    f"{self.caption_font}"
                )
            else:
                print(
                    "Captions: no system font file found; "
                    "falling back to ffmpeg's default font "
                    "lookup. If captions come out blank, "
                    "install a font or pass --caption-font."
                )

        self.download_folder = os.path.dirname(
            self.output_folder
        )

        if self.download_folder == self.output_folder:
            self.download_folder = (
                DEFAULT_DOWNLOAD_FOLDER
            )

        os.makedirs(
            self.output_folder,
            exist_ok=True,
        )

        os.makedirs(
            self.download_folder,
            exist_ok=True,
        )

    # ------------------------------------------------------------------
    # Source resolution
    # ------------------------------------------------------------------

    def find_existing_full_file(
        self,
        match_id,
    ):
        """Return an existing local/full source, if one exists.

        No network operation is performed here.
        """

        if not match_id:
            return None

        match_id = match_id.strip()

        source = detect_source(match_id)

        if source.kind == "local":
            if source.ref and os.path.exists(source.ref):
                return source.ref

            return None

        if source.kind == "youtube":
            return resolve_cached_file(
                match_id,
                "MP4",
                cache=self.cache,
                directories=[
                    self.download_folder
                ],
            )

        if source.kind == "cache_key":
            cached = resolve_cached_file(
                source.id or source.ref,
                "MP4",
                cache=self.cache,
                directories=[
                    self.download_folder
                ],
            )

            if cached:
                return cached

            return None

        return None

    def _strategy_for_entry(
        self,
        entry,
    ) -> str:
        """Determine how one CSV entry should be fetched."""

        match_id = (
            entry.get("match_id") or ""
        ).strip()

        source = detect_source(match_id)

        existing = (
            self.find_existing_full_file(match_id)
        )

        key = match_key(match_id)

        clip_count = self._batch_counts.get(
            key,
            1,
        )

        return choose_fetch_strategy(
            has_local_or_cache=bool(existing),
            is_youtube=(
                source.kind == "youtube"
            ),
            is_gdrive=(
                source.kind == "gdrive"
            ),
            clips_on_this_source=clip_count,
            event_filter=self.caption_event,
            fetch_mode=self.fetch_mode,
            range_clip_limit=self.range_clip_limit,
            has_end=(
                entry.get("end") is not None
            ),
        )

    def ensure_video_available(
        self,
        match_id,
    ):
        """Resolve a complete source."""

        if not match_id:
            return None

        source = detect_source(match_id)

        # --------------------------------------------------------------
        # Local source
        # --------------------------------------------------------------

        if source.kind == "local":
            if not source.ref or not os.path.exists(
                source.ref
            ):
                print(
                    f"Local file not found: "
                    f"{source.ref}"
                )

                with self._stats_lock:
                    self.stats["failed"] += 1

                return None

            print(
                f"Local file: "
                f"{os.path.basename(source.ref)}"
            )

            with self._stats_lock:
                self.stats["local"] += 1

            return source.ref

        # --------------------------------------------------------------
        # Cache-key source
        # --------------------------------------------------------------

        if (
            source.kind == "cache_key"
            and self.use_cache
        ):
            cached = resolve_cached_file(
                source.id or source.ref,
                "MP4",
                cache=self.cache,
                directories=[
                    self.download_folder
                ],
            )

            if cached:
                print(
                    "Cached/local: "
                    f"{os.path.basename(cached)}"
                )

                with self._stats_lock:
                    self.stats["cached"] += 1

                return cached

        # --------------------------------------------------------------
        # YouTube complete source
        # --------------------------------------------------------------

        if (
            source.kind == "youtube"
            and self.download_missing
        ):
            if not HAS_YTDLP:
                report_compiler_failure(
                    VideoToolError(
                        "yt-dlp is not installed. "
                        "Cannot fetch the YouTube source."
                    ),
                    source.ref,
                )

                with self._stats_lock:
                    self.stats["failed"] += 1

                return None

            try:
                from ..youtube import DownloadOptions

                options = DownloadOptions(
                    output_dir=self.download_folder,
                    format_type="MP4",
                    quality=self.quality,
                    cookie_browser=(
                        self.cookie_browser
                    ),
                    cookie_file=self.cookie_file,
                    bypass_no_auth=(
                        self.bypass_no_auth
                    ),
                    remote_components=[
                        "ejs:github"
                    ],
                )

                # Check whether the complete source already exists
                # before asking the shared source layer to fetch it.
                # fetch_source() may legitimately return a cached path,
                # so a successful return does not necessarily mean that
                # a network download occurred.
                existing_cached = resolve_cached_file(
                    source.id or source.ref,
                    "MP4",
                    cache=self.cache,
                    directories=[
                        self.download_folder
                    ],
                )

                print(
                    "Fetching full source: "
                    f"YouTube "
                    f"{source.id or source.ref}"
                )

                source_path, _ = fetch_source(
                    source,
                    self.download_folder,
                    None,
                    None,
                    options,
                    no_cache=not self.use_cache,
                    full_download=True,
                    keep_original=True,
                    attempts=self.range_attempts,
                    cache_directories=[
                        self.download_folder
                    ],
                )

                if source_path:
                    self.cache = load_cache()

                    with self._stats_lock:
                        if existing_cached:
                            self.stats["cached"] += 1
                        else:
                            self.stats["downloaded"] += 1

                    return str(source_path)

            except (
                DownloadError,
                VideoToolError,
                OSError,
                ValueError,
                RuntimeError,
            ) as exc:
                report_compiler_failure(
                    exc,
                    source.ref,
                )

                with self._stats_lock:
                    self.stats["failed"] += 1

                return None

        # --------------------------------------------------------------
        # Google Drive complete source
        # --------------------------------------------------------------

        if (
            source.kind == "gdrive"
            and self.download_missing
        ):
            try:
                print(
                    "Fetching full source: "
                    f"Google Drive "
                    f"{source.id or source.ref}"
                )

                trimmer = GDriveTrimmer(
                    output_dir=self.download_folder,
                    ffmpeg_path=self.ffmpeg_path,
                )

                downloaded = trimmer.download_full(
                    source.ref,
                    use_stream_copy=True,
                )

                if not downloaded.success:
                    raise VideoToolError(
                        downloaded.error_message
                        or "Google Drive download failed."
                    )

                if not downloaded.output_path:
                    raise VideoToolError(
                        "Google Drive download completed "
                        "without an output path."
                    )

                with self._stats_lock:
                    self.stats["downloaded"] += 1

                return downloaded.output_path

            except (
                VideoToolError,
                GDriveError,
                OSError,
                ValueError,
                RuntimeError,
            ) as exc:
                report_compiler_failure(
                    exc,
                    source.ref,
                )

                with self._stats_lock:
                    self.stats["failed"] += 1

                return None

        with self._stats_lock:
            self.stats["failed"] += 1

        print(
            f"Could not resolve source: {match_id}"
        )

        return None

    # ------------------------------------------------------------------
    # Caption handling
    # ------------------------------------------------------------------

    def _prepare_caption(
        self,
        entry,
        temp_folder,
    ):
        """Create the caption filter/text file for one clip."""

        if not self.captions:
            return None, None

        caption_text = (
            self.caption_event
            or event_tags(entry["action"])[0]
        )

        caption_filter, caption_txt_path = (
            build_caption_filter(
                caption_text,
                temp_folder,
                fontfile=self.caption_font,
                position=self.caption_position,
            )
        )

        with self._temp_lock:
            self.temp_clips.append(
                caption_txt_path
            )

        return (
            caption_filter,
            caption_txt_path,
        )

    # ------------------------------------------------------------------
    # Local trimming
    # ------------------------------------------------------------------

    def _trim_local(
        self,
        video_path,
        clip_path,
        entry,
        caption_filter,
    ):
        """Trim an existing local/full source."""

        print(
            f"  Local trim: "
            f"{entry['player']} | "
            f"{entry['action']} | "
            f"{entry['start_str']} -> "
            f"{entry['end_str']}"
        )

        trim_clip(
            self.ffmpeg_path,
            video_path,
            clip_path,
            entry["start"],
            entry["end"],
            resolution=self.resolution,
            bitrate_kbps=self.bitrate_kbps,
            fps=self.fps,
            crf=self.crf,
            preset=self.preset,
            caption_filter=caption_filter,
        )

    # ------------------------------------------------------------------
    # YouTube range fetching
    # ------------------------------------------------------------------

    def _range_from_youtube(
        self,
        url,
        clip_path,
        entry,
        caption_filter,
        temp_folder,
    ):
        """Fetch a YouTube range through the shared clip-source layer."""

        print(
            f"  YouTube range: "
            f"{entry['player']} | "
            f"{entry['action']} | "
            f"{entry['start_str']} -> "
            f"{entry['end_str']}"
        )

        from ..youtube import DownloadOptions

        options = DownloadOptions(
            output_dir=temp_folder,
            format_type="MP4",
            quality=self.quality,
            cookie_browser=self.cookie_browser,
            cookie_file=self.cookie_file,
            bypass_no_auth=self.bypass_no_auth,
            remote_components=[
                "ejs:github"
            ],
        )

        fetched_path, _ = fetch_source(
            url,
            temp_folder,
            entry["start"],
            entry["end"],
            options,
            no_cache=not self.use_cache,
            full_download=False,
            keep_original=True,
            attempts=self.range_attempts,
            cache_directories=[
                self.download_folder
            ],
        )
        fetched = str(fetched_path)

        if os.path.abspath(fetched) != os.path.abspath(
            clip_path
        ):
            duration = (
                entry["end"]
                - (entry["start"] or 0)
            )

            trim_clip(
                self.ffmpeg_path,
                fetched,
                clip_path,
                0,
                duration,
                resolution=self.resolution,
                bitrate_kbps=self.bitrate_kbps,
                fps=self.fps,
                crf=self.crf,
                preset=self.preset,
                caption_filter=caption_filter,
            )

            with self._temp_lock:
                self.temp_clips.append(
                    fetched
                )

        with self._stats_lock:
            self.stats["ranged"] += 1

    # ------------------------------------------------------------------
    # Google Drive range fetching
    # ------------------------------------------------------------------

    def _range_from_gdrive(
        self,
        url,
        clip_path,
        entry,
        caption_filter,
    ):
        """Trim a requested range from a Google Drive source."""

        print(
            f"  Google Drive range: "
            f"{entry['player']} | "
            f"{entry['action']} | "
            f"{entry['start_str']} -> "
            f"{entry['end_str']}"
        )

        trimmer = GDriveTrimmer(
            output_dir=os.path.dirname(clip_path),
            ffmpeg_path=self.ffmpeg_path,
        )

        result = trimmer.trim(
            drive_url=url,
            start_time=entry["start"],
            end_time=entry["end"],
            output_filename=os.path.basename(
                clip_path
            ),
            mode="auto",
            crf=self.crf,
            preset=self.preset,
            include_audio=True,
        )

        if not result.success or not result.output_path:
            raise VideoToolError(
                result.error_message
                or "Google Drive range trim failed."
            )

        needs_normalization = bool(
            caption_filter
            or self.resolution
            or self.fps
        )

        if needs_normalization:
            normalized_path = (
                clip_path + ".normalized.mp4"
            )

            duration = (
                entry["end"]
                - (entry["start"] or 0)
            )

            trim_clip(
                self.ffmpeg_path,
                result.output_path,
                normalized_path,
                0,
                duration,
                resolution=self.resolution,
                bitrate_kbps=self.bitrate_kbps,
                fps=self.fps,
                crf=self.crf,
                preset=self.preset,
                caption_filter=caption_filter,
            )

            os.replace(
                normalized_path,
                clip_path,
            )

            if (
                result.output_path
                != clip_path
            ):
                with self._temp_lock:
                    self.temp_clips.append(
                        result.output_path
                    )

        with self._stats_lock:
            self.stats["ranged"] += 1

    # ------------------------------------------------------------------
    # Single clip generation
    # ------------------------------------------------------------------

    def generate_clip(
        self,
        entry,
    ):
        """Generate one trimmed clip.

        Returns:
            Path to generated clip, or None on failure.
        """

        match_id = entry["match_id"]

        strategy = self._strategy_for_entry(
            entry
        )

        temp_folder = os.path.join(
            self.output_folder,
            ".temp",
        )

        os.makedirs(
            temp_folder,
            exist_ok=True,
        )

        safe_player = re.sub(
            r"[^\w\s-]",
            "",
            entry["player"],
        ).strip().replace(
            " ",
            "_",
        )

        safe_action = re.sub(
            r"[^\w\s-]",
            "",
            entry["action"],
        ).strip().replace(
            " ",
            "_",
        )

        start_sec = int(
            entry["start"]
        )

        source = detect_source(
            (match_id or "").strip()
        )

        source_hash = hashlib.sha1(
            (
                source.id
                or source.ref
                or ""
            ).encode("utf-8")
        ).hexdigest()[:8]

        clip_name = (
            f"{start_sec:05d}_"
            f"{source_hash}_"
            f"{safe_player}_"
            f"{safe_action}_"
            f"{entry['start_str']}-"
            f"{entry['end_str']}.mp4"
        )

        clip_path = os.path.join(
            temp_folder,
            clip_name,
        )

        # --------------------------------------------------------------
        # Dry run
        # --------------------------------------------------------------

        if self.dry_run:
            label = {
                "local-trim": "local trim",
                "youtube-range": (
                    "YouTube range-download"
                ),
                "youtube-full": (
                    "full download then local trim"
                ),
                "gdrive-range": (
                    "Google Drive range trim"
                ),
                "gdrive-full": (
                    "Google Drive full download"
                ),
            }.get(
                strategy,
                strategy,
            )

            print(
                f"  [DRY-RUN] Would {label}: "
                f"{entry['player']} | "
                f"{entry['action']} | "
                f"{entry['start_str']} -> "
                f"{entry['end_str']}"
            )

            if self.captions:
                print(
                    "  [DRY-RUN] Would caption: "
                    f'"{self.caption_event or event_tags(entry["action"])[0]}"'
                )

            return clip_path

        # --------------------------------------------------------------
        # FFmpeg availability
        # --------------------------------------------------------------

        if not self.ffmpeg_path:
            report_compiler_failure(
                MissingToolError(
                    "ffmpeg is needed to compile clips, "
                    "but it was not found. Install ffmpeg "
                    "and make sure it is on your PATH."
                )
            )

            with self._stats_lock:
                self.stats["failed"] += 1

            return None

        caption_filter, _caption_txt = (
            self._prepare_caption(
                entry,
                temp_folder,
            )
        )

        try:
            # ----------------------------------------------------------
            # YouTube range
            # ----------------------------------------------------------

            if strategy == "youtube-range":
                source = detect_source(
                    (match_id or "").strip()
                )

                if source.kind != "youtube":
                    raise VideoToolError(
                        "YouTube range download requires "
                        "a YouTube source."
                    )

                if not HAS_YTDLP:
                    raise VideoToolError(
                        "yt-dlp is not installed. "
                        "Cannot range-download."
                    )

                self._range_from_youtube(
                    source.ref,
                    clip_path,
                    entry,
                    caption_filter,
                    temp_folder,
                )

            # ----------------------------------------------------------
            # Google Drive range
            # ----------------------------------------------------------

            elif strategy == "gdrive-range":
                source = detect_source(
                    (match_id or "").strip()
                )

                if source.kind != "gdrive":
                    raise VideoToolError(
                        "Google Drive range trim requires "
                        "a Google Drive source."
                    )

                self._range_from_gdrive(
                    source.ref,
                    clip_path,
                    entry,
                    caption_filter,
                )

            # ----------------------------------------------------------
            # Full/local source
            # ----------------------------------------------------------

            else:
                video_path = (
                    self.ensure_video_available(
                        match_id
                    )
                )

                if not video_path:
                    print(
                        f"  No video available for: "
                        f"{match_id}"
                    )

                    return None

                self._trim_local(
                    video_path,
                    clip_path,
                    entry,
                    caption_filter,
                )

            # ----------------------------------------------------------
            # Successful clip
            # ----------------------------------------------------------

            with self._temp_lock:
                self.temp_clips.append(
                    clip_path
                )

            with self._stats_lock:
                self.stats["trimmed"] += 1

            return clip_path

        except KeyboardInterrupt:
            raise

        except (
            DownloadError,
            VideoToolError,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            report_compiler_failure(
                exc,
                (
                    source.ref
                    if source.kind in {
                        "youtube",
                        "gdrive",
                    }
                    else None
                ),
            )

            with self._stats_lock:
                self.stats["failed"] += 1

            return None

    # ------------------------------------------------------------------
    # Compilation
    # ------------------------------------------------------------------

    def compile_player(
        self,
        player_name,
        entries,
    ):
        """Compile all clips for one player."""

        return self._compile(
            player_name,
            entries,
        )

    def compile_event(
        self,
        event_name,
        entries,
    ):
        """Compile all clips matching an event."""

        return self._compile(
            event_name,
            entries,
        )

    def _compile(
        self,
        name,
        entries,
    ):
        """Generate and merge a group of clips."""

        print(f"\n{'=' * 50}")
        print(f"Compiling: {name}")
        print(f"{'=' * 50}")

        entries = sorted(
            entries,
            key=lambda x: x["start"],
        )

        self._batch_counts = count_by_match(
            entries
        )

        strategies = {
            self._strategy_for_entry(entry)
            for entry in entries
        }

        print(
            f"Clip path: "
            f"{', '.join(sorted(strategies))} "
            f"(mode={self.fetch_mode}, "
            f"range-limit={self.range_clip_limit})"
        )

        # --------------------------------------------------------------
        # Generate clips
        # --------------------------------------------------------------

        clips: list[str] = []

        if (
            self.jobs > 1
            and not self.dry_run
        ):
            with ThreadPoolExecutor(
                max_workers=self.jobs
            ) as executor:
                future_to_entry = {
                    executor.submit(
                        self.generate_clip,
                        entry,
                    ): entry
                    for entry in entries
                }

                generated = []

                for future in as_completed(
                    future_to_entry
                ):
                    result = future.result()

                    if result:
                        entry = future_to_entry[
                            future
                        ]

                        generated.append(
                            (
                                entry["start"],
                                result,
                            )
                        )

                generated.sort(
                    key=lambda item: item[0]
                )

                clips = [
                    path
                    for _, path in generated
                ]

        else:
            for entry in entries:
                clip = self.generate_clip(
                    entry
                )

                if clip:
                    clips.append(clip)

        if not clips:
            print(
                f"No clips generated for {name}"
            )
            return None

        # --------------------------------------------------------------
        # Output path
        # --------------------------------------------------------------

        safe_name = re.sub(
            r"[^\w\s-]",
            "",
            name,
        ).strip().replace(
            " ",
            "_",
        )

        timestamp = (
            datetime.now()
            .astimezone()
            .strftime(
                "%Y%m%d_%H%M%S"
            )
        )

        output_name = (
            f"{safe_name}_Compilation_"
            f"{timestamp}.mp4"
        )

        output_path = os.path.join(
            self.output_folder,
            output_name,
        )

        # --------------------------------------------------------------
        # Dry run
        # --------------------------------------------------------------

        if self.dry_run:
            print(
                f"\n[DRY-RUN] Would merge "
                f"{len(clips)} clips into:\n"
                f"  {output_path}"
            )

            if self.logo_path:
                print(
                    f"[DRY-RUN] Would watermark with: "
                    f"{self.logo_path}"
                )

            return output_path

        # --------------------------------------------------------------
        # Merge
        # --------------------------------------------------------------

        print(
            f"\nMerging {len(clips)} clips..."
        )

        try:
            merge_result = merge_clips(
                self.ffmpeg_path,
                clips,
                output_path,
                re_encode=False,
                crf=self.crf,
                preset=self.preset,
            )

            if (
                not os.path.exists(output_path)
                or os.path.getsize(output_path)
                < 1000
            ):
                raise VideoToolError(
                    "Merge completed without producing "
                    "a valid output file."
                )

            if isinstance(
                merge_result,
                tuple,
            ):
                _merged_path, strategy = (
                    merge_result
                )

                logger.debug(
                    "Compilation merge strategy: %s",
                    strategy,
                )

        except KeyboardInterrupt:
            raise

        except (
            VideoToolError,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            report_compiler_failure(
                exc
            )

            print(
                "Merge stopped. Clips that already "
                "succeeded are still in .temp/"
            )

            return None

        # --------------------------------------------------------------
        # Watermark
        # --------------------------------------------------------------

        if self.logo_path:
            print(
                "\nApplying watermark..."
            )

            watermarked_path = (
                output_path
                + ".watermarked.mp4"
            )

            try:
                apply_watermark(
                    self.ffmpeg_path,
                    output_path,
                    watermarked_path,
                    self.logo_path,
                    position=(
                        self.watermark_position
                    ),
                    width=self.watermark_width,
                    opacity=(
                        self.watermark_opacity
                    ),
                    margin=self.watermark_margin,
                    crf=self.crf,
                    preset=self.preset,
                )

                os.replace(
                    watermarked_path,
                    output_path,
                )

            except (
                VideoToolError,
                OSError,
                ValueError,
                RuntimeError,
            ) as exc:
                report_compiler_failure(
                    exc
                )

                print(
                    "The compilation itself is fine -- "
                    "it is saved unwatermarked at:\n"
                    f"  {output_path}"
                )

        # --------------------------------------------------------------
        # Final result
        # --------------------------------------------------------------

        size_mb = (
            os.path.getsize(output_path)
            / (1024 * 1024)
        )

        print(
            f"Done! {output_path}"
        )

        print(
            f"Size: {size_mb:.1f} MB"
        )

        return output_path

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        entries=None,
        player_filter=None,
        event_filter=None,
    ):
        """Run the compiler.

        Grouping rules:

        * player_filter + event_filter:
            group the filtered clips by player.

        * player_filter only:
            compile that player's clips.

        * event_filter only:
            compile all matching event clips together.

        * neither:
            preserve the default player-based compilation behavior.
        """

        print(
            f"Reading: {self.csv_path}"
        )

        entries = (
            read_csv_entries(
                self.csv_path
            )
            if entries is None
            else entries
        )

        if not entries:
            print(
                "No valid entries found in CSV."
            )
            return {}

        print(
            f"Found {len(entries)} clip entries"
        )

        # --------------------------------------------------------------
        # Event compilation
        #
        # When the user explicitly requests an event without a player,
        # all matching clips belong to one event compilation.
        #
        # Example:
        #
        #   -e Goal
        #
        #   Goal clip 1
        #   Goal clip 2
        #   Goal clip 3
        #
        # becomes:
        #
        #   Goal_Compilation.mp4
        #
        # rather than one compilation per player.
        # --------------------------------------------------------------

        if event_filter and not player_filter:
            print(
                f"Event: {event_filter}"
            )

            output = self.compile_event(
                event_filter,
                entries,
            )

            results = {}

            if output:
                results[event_filter] = output

        # --------------------------------------------------------------
        # Player compilation
        #
        # This includes:
        #
        #   -p "Player"
        #
        # and:
        #
        #   -p "Player" -e "Goal"
        #
        # In the second case, the event acts as an additional filter,
        # while the player remains the compilation grouping key.
        # --------------------------------------------------------------

        else:
            by_player: dict[str, list] = {}

            for entry in entries:
                by_player.setdefault(
                    entry["player"],
                    [],
                ).append(entry)

            print(
                "Players: "
                + ", ".join(
                    by_player.keys()
                )
            )

            results = {}

            for player, player_entries in (
                by_player.items()
            ):
                output = self.compile_player(
                    player,
                    player_entries,
                )

                if output:
                    results[player] = output

        # --------------------------------------------------------------
        # Cleanup
        # --------------------------------------------------------------

        if not self.keep_temp:
            print(
                "\nCleaning up temp files..."
            )

            for clip in self.temp_clips:
                try:
                    if os.path.exists(clip):
                        os.remove(clip)
                except OSError:
                    pass

            temp_folder = os.path.join(
                self.output_folder,
                ".temp",
            )

            try:
                if (
                    os.path.exists(
                        temp_folder
                    )
                    and not os.listdir(
                        temp_folder
                    )
                ):
                    os.rmdir(
                        temp_folder
                    )
            except OSError:
                pass

        else:
            print(
                "\nKept temp files in: "
                f"{os.path.join(self.output_folder, '.temp')}"
            )

        # --------------------------------------------------------------
        # Summary
        # --------------------------------------------------------------

        print(
            f"\n{'=' * 50}"
        )

        print("SUMMARY")

        print(
            f"{'=' * 50}"
        )

        for name, path in results.items():
            if self.dry_run:
                print(
                    f"  [DRY-RUN] "
                    f"{name}: {path}"
                )

            elif os.path.exists(path):
                size = (
                    os.path.getsize(path)
                    / (1024 * 1024)
                )

                print(
                    f"  {name}: "
                    f"{size:.1f} MB -> "
                    f"{path}"
                )

            else:
                print(
                    f"  {name}: file not found "
                    f"at {path}"
                )

        print(
            f"\n  Trimmed: "
            f"{self.stats['trimmed']}  |  "
            f"Failed: "
            f"{self.stats['failed']}"
        )

        print(
            f"  Local: "
            f"{self.stats['local']}  |  "
            f"Cached: "
            f"{self.stats['cached']}  |  "
            f"Downloaded: "
            f"{self.stats['downloaded']}  |  "
            f"Ranged: "
            f"{self.stats['ranged']}"
        )

        # --------------------------------------------------------------
        # Manifest
        # --------------------------------------------------------------

        if results and not self.dry_run:
            raw_match_id = entries[0][
                "match_id"
            ]

            match_id = (
                extract_video_id(
                    raw_match_id
                )
                or raw_match_id
            )

            manifest_root = os.path.join(
                os.path.dirname(
                    self.output_folder
                ),
                "manifests",
            )

            write_manifests(
                match_id,
                raw_match_id,
                results,
                manifest_root=manifest_root,
            )

        return results


# ----------------------------------------------------------------------
# Event filtering
# ----------------------------------------------------------------------


def event_tags(action):
    """Return the primary event tags from an action label.

    Examples:

        Shot (Out-box Off Target)
            → ["Shot"]

        Blocks (Shot)
            → ["Blocks"]

        Goal (Free Kick) + Shot (In-box On Target)
            → ["Goal", "Shot"]
    """

    tags = []

    for part in action.split(" + "):
        part = part.strip()

        paren = part.find(" (")

        if paren != -1:
            tags.append(
                part[:paren].strip()
            )
        else:
            tags.append(part)

    return tags


def matches_event(
    action,
    term,
):
    """Return True when term exactly matches a primary event tag.

    Matching is case-insensitive.

    ``Shot`` therefore matches:
        Shot (...)
        Shot (Blocked)
        Shot (On Target)
        Shot (Off Target)

    But ``Blocks (Shot)`` matches only ``Blocks``.
    """

    term = term.lower()

    return any(
        tag.lower() == term
        for tag in event_tags(action)
    )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Video Tool Auto-Compiler v1.0 -- "
            "unified CSV for YouTube, local files, "
            "Google Drive, and cached videos"
        ),
        formatter_class=(
            argparse.RawDescriptionHelpFormatter
        ),
        epilog="""
UNIFIED CSV FORMAT:
  The match_id column accepts ANY of these:
    YouTube URL:     https://www.youtube.com/watch?v=ABC123
    Google Drive:    https://drive.google.com/file/d/ABC123/view
    Local path:      /home/lab/videos/match1.mp4
                     (or just match1.mp4)
    Cache key:       ABC123
                     (previously downloaded YouTube video ID)

  All sources work in the SAME CSV.
  The compiler auto-detects the type.

CLIP SOURCE (--source):
  auto   Local/cached full files are cut on disk.
         Missing YouTube sources range-download when this compile
         is an --event filter, or when that source has few clips
         (quiet player). Dense player compiles download the match
         once, then trim locally.

         Google Drive ranges are processed remotely when an end
         time is supplied.

  range  Always use YouTube range downloading unless a full file
         is already local/cached.

  full   Always download the YouTube match, then trim locally.

Examples:
  python -m video_tool.versions.auto_compiler tests/cape_verde.csv
  python -m video_tool.versions.auto_compiler tags.csv --event Goal
  python -m video_tool.versions.auto_compiler tags.csv -p "Quiet Player"
  python -m video_tool.versions.auto_compiler tags.csv --source full
        """,
    )

    parser.add_argument(
        "csv",
        help="Path to CSV file with timestamps",
    )

    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT_FOLDER,
        help=(
            "Output folder "
            "(default: ~/Downloads/video-tool/Compilations)"
        ),
    )

    parser.add_argument(
        "-r",
        "--resolution",
        default="1080p",
        choices=[
            "1080p",
            "720p",
            "480p",
            "360p",
        ],
        help="Output resolution",
    )

    parser.add_argument(
        "-b",
        "--bitrate",
        type=int,
        default=8000,
        help=(
            "Target video bitrate in kbps "
            "(default: 8000)"
        ),
    )

    parser.add_argument(
        "-f",
        "--fps",
        type=int,
        default=30,
        help="Output frame rate (default: 30)",
    )

    parser.add_argument(
        "-p",
        "--player",
        default=None,
        help="Compile only this specific player",
    )

    parser.add_argument(
        "-e",
        "--event",
        default=None,
        help=(
            'Compile all clips tagged with this '
            'exact event (e.g. "Goal")'
        ),
    )

    parser.add_argument(
        "-q",
        "--quality",
        default="Best",
        choices=[
            "Best",
            "1080p",
            "720p",
            "480p",
            "360p",
        ],
        help=(
            "Download quality when video needs "
            "to be fetched"
        ),
    )

    parser.add_argument(
        "--source",
        choices=[
            "auto",
            "range",
            "full",
        ],
        default="auto",
        help=(
            "How to get clips from YouTube "
            "(default: auto)"
        ),
    )

    parser.add_argument(
        "--range-limit",
        type=int,
        default=RANGE_CLIP_LIMIT,
        help=(
            "In auto mode, range-download when "
            "this source has this many clips or "
            f"fewer (default: {RANGE_CLIP_LIMIT})"
        ),
    )

    parser.add_argument(
        "--attempts",
        type=_positive_int,
        default=DEFAULT_ATTEMPTS,
        help=(
            "Range-download attempts per clip "
            f"(default: {DEFAULT_ATTEMPTS})"
        ),
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Do not reuse cached full YouTube files"
        ),
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Show technical detail for troubleshooting"
        ),
    )

    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help=(
            "Preview the plan without running ffmpeg"
        ),
    )

    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help=(
            "Preserve temporary trimmed clips"
        ),
    )

    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=1,
        help=(
            "Parallel trim jobs (default: 1)"
        ),
    )

    parser.add_argument(
        "--crf",
        type=int,
        default=18,
        choices=range(51),
        help=(
            "Quality: 18=visually lossless "
            "(default: 18)"
        ),
    )

    parser.add_argument(
        "--preset",
        default="slow",
        choices=[
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        ],
        help=(
            "Encoding speed/quality tradeoff "
            "(default: slow)"
        ),
    )

    parser.add_argument(
        "--cookie-browser",
        default=None,
        choices=[
            "chrome",
            "firefox",
            "edge",
            "safari",
            "brave",
            "opera",
            "none",
        ],
        help=(
            "Browser to extract cookies from "
            "for YouTube auth"
        ),
    )

    parser.add_argument(
        "--cookie-file",
        default=None,
        help=(
            "Path to a Netscape cookies.txt file "
            "for YouTube auth"
        ),
    )

    parser.add_argument(
        "--no-bypass",
        action="store_true",
        help=(
            "Disable no-auth bypass mode "
            "(default: bypass enabled)"
        ),
    )

    parser.add_argument(
        "--logo",
        default="assets/tisini-logo.png",
        help=(
            "Path to a logo image "
            "(default: assets/tisini-logo.png)"
        ),
    )

    parser.add_argument(
        "--no-logo",
        action="store_true",
        help=(
            "Disable the watermark for this run"
        ),
    )

    parser.add_argument(
        "--logo-position",
        default="bottomleft",
        choices=[
            "topleft",
            "topright",
            "bottomleft",
            "bottomright",
            "center",
        ],
        help=(
            "Where to place the logo "
            "(default: bottomleft)"
        ),
    )

    parser.add_argument(
        "--logo-width",
        type=int,
        default=180,
        help=(
            "Logo width in pixels "
            "(default: 180)"
        ),
    )

    parser.add_argument(
        "--logo-opacity",
        type=float,
        default=0.85,
        help=(
            "Logo opacity, 0.0 to 1.0 "
            "(default: 0.85)"
        ),
    )

    parser.add_argument(
        "--logo-margin",
        type=int,
        default=48,
        help=(
            "Gap in pixels between the logo "
            "and the frame edge (default: 48)"
        ),
    )

    parser.add_argument(
        "--captions",
        dest="captions",
        action="store_true",
        default=True,
        help=(
            "Burn the action label onto each clip "
            "(default: on)"
        ),
    )

    parser.add_argument(
        "--no-captions",
        dest="captions",
        action="store_false",
        help=(
            "Disable captions for this run"
        ),
    )

    parser.add_argument(
        "--caption-font",
        default=None,
        help=(
            "Path to a .ttf/.otf font file for "
            "captions (default: auto-detect)"
        ),
    )

    parser.add_argument(
        "--caption-position",
        default="bottom",
        choices=[
            "top",
            "bottom",
        ],
        help=(
            "Where to place captions "
            "(default: bottom)"
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=(
            logging.DEBUG
            if args.verbose
            else logging.WARNING
        ),
        format=(
            "%(levelname)s "
            "%(name)s: %(message)s"
        ),
    )

    cookie_browser = (
        args.cookie_browser
        or _load_pro_cookie_browser()
    )

    if (
        cookie_browser
        and cookie_browser.lower() == "none"
    ):
        cookie_browser = None

    compiler = AutoCompiler(
        csv_path=args.csv,
        output_folder=args.output,
        resolution=args.resolution,
        bitrate_kbps=args.bitrate,
        fps=args.fps,
        download_missing=True,
        quality=args.quality,
        dry_run=args.dry_run,
        keep_temp=args.keep_temp,
        jobs=args.jobs,
        cookie_browser=cookie_browser,
        cookie_file=args.cookie_file,
        bypass_no_auth=not args.no_bypass,
        crf=args.crf,
        preset=args.preset,
        logo_path=(
            None
            if args.no_logo
            else args.logo
        ),
        watermark_position=(
            args.logo_position
        ),
        watermark_width=(
            args.logo_width
        ),
        watermark_opacity=(
            args.logo_opacity
        ),
        watermark_margin=(
            args.logo_margin
        ),
        captions=args.captions,
        caption_font=args.caption_font,
        caption_position=(
            args.caption_position
        ),
        caption_event=args.event,
        fetch_mode=args.source,
        range_clip_limit=(
            args.range_limit
        ),
        range_attempts=max(
            1,
            args.attempts,
        ),
    )

    compiler.use_cache = (
        not args.no_cache
    )

    # --------------------------------------------------------------
    # Optional player/event filtering
    # --------------------------------------------------------------

    if args.player or args.event:
        entries = read_csv_entries(
            compiler.csv_path
        )

        if args.player:
            entries = [
                entry
                for entry in entries
                if entry["player"].lower()
                == args.player.lower()
            ]

        if args.event:
            entries = [
                entry
                for entry in entries
                if matches_event(
                    entry["action"],
                    args.event,
                )
            ]

        if not entries:
            what = " + ".join(
                filter(
                    None,
                    [
                        (
                            f"player '{args.player}'"
                            if args.player
                            else None
                        ),
                        (
                            f"event '{args.event}'"
                            if args.event
                            else None
                        ),
                    ],
                )
            )

            print(
                f"No clips found for {what}."
            )

            return 1

        compiler.run(
            entries=entries,
            player_filter=args.player,
            event_filter=args.event,
        )

    else:
        compiler.run()

    return (
        0
        if compiler.stats["failed"] == 0
        else 1
    )


if __name__ == "__main__":
    main()
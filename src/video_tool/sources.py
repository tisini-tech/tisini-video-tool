
"""Shared source detection for every Video Tool pipeline.

This module only identifies and normalizes an input source.

It does not:

    - download anything
    - contact YouTube or Google Drive
    - probe media
    - trim media
    - access the cache
    - perform any FFmpeg operation

The actual processing remains in the appropriate pipeline modules.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Source patterns
# ---------------------------------------------------------------------------

_YOUTUBE_RE = re.compile(
    r"(?:https?://)?"
    r"(?:www\.)?"
    r"(?:"
    r"youtube\.com/watch\?v="
    r"|youtu\.be/"
    r"|youtube\.com/embed/"
    r"|youtube\.com/shorts/"
    r"|youtube\.com/live/"
    r")"
    r"([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

_DRIVE_RE = re.compile(
    r"(?:https?://)?"
    r"(?:www\.)?"
    r"(?:"
    r"drive\.google\.com/file/d/"
    r"|drive\.google\.com/open\?id="
    r"|drive\.google\.com/uc\?(?:[^#]*&)?id="
    r"|googledrive\.com/host/"
    r")"
    r"([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Source model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """Normalized representation of an input source.

    Attributes:
        kind:
            One of:

                - ``youtube``
                - ``gdrive``
                - ``local``
                - ``cache_key``
                - ``unknown``

        ref:
            The usable/original reference supplied by the caller.

            For local files this is the resolved filesystem path.
            For remote sources this is the original URL.

        id:
            Source-specific identifier.

            YouTube:
                YouTube video ID.

            Google Drive:
                Drive file ID.

            Local:
                None.

            Cache key:
                The supplied key.

            Unknown:
                None.
    """

    kind: str
    ref: str
    id: str | None = None

    @property
    def is_remote(self) -> bool:
        """Return True for sources requiring remote access."""

        return self.kind in {"youtube", "gdrive"}

    @property
    def is_local(self) -> bool:
        """Return True for an actual local file."""

        return self.kind == "local"

    @property
    def is_youtube(self) -> bool:
        """Return True when this is a YouTube source."""

        return self.kind == "youtube"

    @property
    def is_gdrive(self) -> bool:
        """Return True when this is a Google Drive source."""

        return self.kind == "gdrive"

    @property
    def is_cache_key(self) -> bool:
        """Return True when the input was interpreted as a cache key."""

        return self.kind == "cache_key"

    @property
    def is_unknown(self) -> bool:
        """Return True when the source could not be identified."""

        return self.kind == "unknown"


# ---------------------------------------------------------------------------
# Local path detection
# ---------------------------------------------------------------------------


def _local_path(value: str) -> str | None:
    """Resolve ``value`` to an existing local file when possible.

    Relative paths are checked from the current working directory.

    For convenience, ``.mp4`` is also tried when the supplied path does not
    already end in ``.mp4``.

    Returns:
        Absolute resolved path when the file exists, otherwise ``None``.
    """

    candidate = Path(value).expanduser()

    candidates = [candidate]

    if candidate.suffix.lower() != ".mp4":
        candidates.append(
            candidate.with_name(candidate.name + ".mp4")
        )

    for path in candidates:
        if path.is_file():
            return str(path.resolve())

    return None


# ---------------------------------------------------------------------------
# Public detection API
# ---------------------------------------------------------------------------


def detect_source(
    value: str | os.PathLike[str] | None,
) -> Source:
    """Classify an input source.

    Detection order is deliberate:

        1. Existing local file
        2. YouTube URL
        3. Google Drive URL
        4. Cache key / unresolved reference
        5. Unknown empty input

    This function has no side effects beyond checking whether a local path
    exists.

    Args:
        value:
            URL, filesystem path, cache key, or ``None``.

    Returns:
        A normalized :class:`Source`.
    """

    if value is None:
        return Source(
            kind="unknown",
            ref="",
            id=None,
        )

    text = str(value).strip()

    if not text:
        return Source(
            kind="unknown",
            ref="",
            id=None,
        )

    # Existing local files take precedence.
    #
    # This is important because local paths can contain strings that happen
    # to resemble URLs or IDs.
    local = _local_path(text)

    if local:
        return Source(
            kind="local",
            ref=local,
            id=None,
        )

    # YouTube
    youtube_match = _YOUTUBE_RE.search(text)

    if youtube_match:
        return Source(
            kind="youtube",
            ref=text,
            id=youtube_match.group(1),
        )

    # Google Drive
    drive_match = _DRIVE_RE.search(text)

    if drive_match:
        return Source(
            kind="gdrive",
            ref=text,
            id=drive_match.group(1),
        )

    # Anything non-empty that isn't a recognized URL or local file is
    # retained as a cache key/reference rather than immediately rejected.
    #
    # This keeps source detection separate from validation. The caller can
    # decide whether an unresolved reference is acceptable.
    return Source(
        kind="cache_key",
        ref=text,
        id=text,
    )


def is_supported_source(
    value: str | os.PathLike[str] | None,
) -> bool:
    """Return whether ``value`` is usable at the detection layer.

    Cache keys are deliberately considered supported here because existing
    cache-based workflows may pass them directly to the pipeline.

    Empty input is the only unsupported category at this layer.
    """

    return not detect_source(value).is_unknown

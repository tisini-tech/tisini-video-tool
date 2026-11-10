"""Checks for uploaded video files. No web code and no job store here.

An upload is a file chosen by someone else, so it is treated as hostile
until two checks pass:

1. Its first bytes must be those of a real video container. A text file
   that ffmpeg would read as a playlist or script, and so follow to other
   files, is turned away before ffmpeg ever opens it.
2. ffprobe must read it as one of the allowed containers and find a video
   stream in it.

The server never uses the name the sender gave the file.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

# The ID the server gives an upload: secrets.token_urlsafe(16).
UPLOAD_ID = r"^[A-Za-z0-9_-]{22}$"

HEAD_BYTES = 2048

# ffprobe's names for the containers a merge accepts: MP4 and MOV, MKV and
# WebM, AVI, FLV, MPEG-TS and MPEG-PS. Playlists, scripts and image
# sequences are not on the list.
ALLOWED_FORMATS = {
    "mov", "mp4", "m4a", "3gp", "3g2", "mj2",
    "matroska", "webm",
    "avi",
    "flv",
    "mpegts",
    "mpeg",
}

_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot")


class UploadRejected(Exception):
    """An upload was refused. ``status`` is the HTTP code to answer with."""

    def __init__(self, status: int, title: str, message: str):
        super().__init__(message)
        self.status = status
        self.title = title
        self.message = message


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):,.0f} MB"


def looks_like_video(head: bytes) -> bool:
    """True when ``head`` starts like a video container we accept."""

    if len(head) < 12:
        return False

    # MP4 and MOV: a 4-byte box size, then the box name.
    if head[4:8] in _MP4_BOXES:
        return True

    # Matroska and WebM.
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return True

    # AVI.
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return True

    # FLV.
    if head[:3] == b"FLV":
        return True

    # MPEG program stream.
    if head[:4] == b"\x00\x00\x01\xba":
        return True

    # MPEG transport stream: a sync byte every 188 bytes, or every 192
    # bytes when each packet has a 4-byte time stamp in front (M2TS).
    for start, step in ((0, 188), (4, 192)):
        spots = [start + step * n for n in range(3)]

        if spots[-1] < len(head) and all(head[i] == 0x47 for i in spots):
            return True

    return False


async def save_stream(
    chunks: AsyncIterator[bytes], destination: Path, limit: int
) -> int:
    """Write the chunks to ``destination``. Return the size in bytes.

    Stops at once, without reading the rest, when more than ``limit``
    bytes arrive. The caller deletes the partial file.
    """

    import asyncio

    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)

    with open(destination, "wb") as out:
        async for chunk in chunks:
            if not chunk:
                continue

            size += len(chunk)

            if size > limit:
                raise UploadRejected(
                    413,
                    "File too big",
                    f"This upload may be at most {_megabytes(limit)}.",
                )

            await asyncio.to_thread(out.write, chunk)

    if size == 0:
        raise UploadRejected(
            422, "Empty file", "The upload had no data in it."
        )

    return size


def check_video(path: Path) -> None:
    """Raise ``UploadRejected`` unless ``path`` is a readable video."""

    from ..core import find_ffprobe

    not_video = UploadRejected(
        415,
        "Not a video file",
        "This file is not a video the server can read. Send an MP4, "
        "MOV, MKV, WebM, AVI, FLV or TS file.",
    )

    with open(path, "rb") as handle:
        head = handle.read(HEAD_BYTES)

    if not looks_like_video(head):
        raise not_video

    ffprobe = find_ffprobe()

    if not ffprobe:
        raise UploadRejected(
            503,
            "Cannot check videos",
            "The server cannot read videos right now. Try again later.",
        )

    command = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=format_name:stream=codec_type",
        "-of", "json",
        str(path),
    ]

    try:
        run = subprocess.run(
            command, capture_output=True, text=True, timeout=60
        )
        data = json.loads(run.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError, OSError):
        raise not_video from None

    names = {
        name
        for name in str(data.get("format", {}).get("format_name", "")).split(",")
        if name
    }
    kinds = {stream.get("codec_type") for stream in data.get("streams", [])}

    if not names or not names <= ALLOWED_FORMATS or "video" not in kinds:
        raise not_video

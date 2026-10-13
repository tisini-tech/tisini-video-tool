"""Check a YouTube link and rebuild it in one fixed form.

The API fetches only the link this module returns. It always has the same
host, path and shape, so a request cannot steer the fetch to another site
or pass extra options to yt-dlp through the link.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from ..versions.common_cli import clean_url

NOT_YOUTUBE = "This is not a YouTube link."

MAX_LENGTH = 2048

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_SHORT_HOST = "youtu.be"

# Paths of the form /<word>/<video id>.
_ID_AFTER = {"shorts", "live", "embed"}


def video_id_from(link: str) -> str:
    """Return the video ID in ``link``. Raise ValueError if there is none."""

    value = clean_url(link)

    if not value or len(value) > MAX_LENGTH:
        raise ValueError(NOT_YOUTUBE)

    if "://" not in value:
        value = "https://" + value

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError(NOT_YOUTUBE) from None

    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(NOT_YOUTUBE)

    # "https://youtube.com@other.site/..." has a user name, not a host.
    if parts.username is not None or parts.password is not None:
        raise ValueError(NOT_YOUTUBE)

    if port not in (None, 443):
        raise ValueError(NOT_YOUTUBE)

    host = (parts.hostname or "").lower().rstrip(".")
    segments = [part for part in parts.path.split("/") if part]
    found = ""

    if host == _SHORT_HOST:
        if len(segments) == 1:
            found = segments[0]

    elif host in _HOSTS:
        if segments == ["watch"]:
            found = parse_qs(parts.query).get("v", [""])[0]

        elif len(segments) == 2 and segments[0] in _ID_AFTER:
            found = segments[1]

    if not _VIDEO_ID.fullmatch(found):
        raise ValueError(NOT_YOUTUBE)

    return found


def canonical_url(link: str) -> str:
    """Return the one standard link for the video in ``link``."""

    return f"https://www.youtube.com/watch?v={video_id_from(link)}"

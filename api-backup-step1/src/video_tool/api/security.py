"""Settings, limits, API key check and user ID check."""

from __future__ import annotations

import hmac
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

_USER_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def _number(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    """Rules that protect the server. Times are in seconds."""

    per_hour: int = 10       # jobs one user may start in an hour
    fail_streak: int = 3     # failures in a row before a pause
    fail_pause: int = 300    # length of that pause
    fail_memory: int = 300   # how long a failed link is remembered
    job_timeout: int = 900   # longest a job may run
    sweep_every: int = 30    # how often the server tidies up


@dataclass(frozen=True)
class Settings:
    """Server settings. Requests never change these."""

    api_key: str
    work_dir: Path = Path(tempfile.gettempdir()) / "video-tool-api"
    cookie_file: str | None = None
    ttl: int = 3600
    max_workers: int = 2
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(find_dotenv(usecwd=True), override=True)

        return cls(
            api_key=os.environ.get("VT_API_KEY", ""),
            work_dir=Path(
                os.environ.get("VT_API_WORK")
                or Path(tempfile.gettempdir()) / "video-tool-api"
            ),
            cookie_file=os.environ.get("VT_API_COOKIE_FILE") or None,
            ttl=_number("VT_API_TTL", 3600),
            max_workers=_number("VT_API_WORKERS", 2),
            limits=Limits(
                per_hour=_number("VT_API_PER_HOUR", 10),
                fail_streak=_number("VT_API_FAIL_STREAK", 3),
                fail_pause=_number("VT_API_FAIL_PAUSE", 300),
                fail_memory=_number("VT_API_FAIL_MEMORY", 300),
                job_timeout=_number("VT_API_JOB_TIMEOUT", 900),
            ),
        )


def key_matches(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(
        given.encode(), expected.encode()
    )


def valid_user_id(value: str) -> bool:
    return bool(_USER_ID.match(value))

"""Settings, limits, API key check and user ID check."""

from __future__ import annotations

import hmac
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Shorter keys are too easy to guess. The vt-api command refuses them.
MIN_KEY_LENGTH = 32

_USER_ID = re.compile(r"[A-Za-z0-9_.@-]{1,64}")


def _number(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass(frozen=True)
class Limits:
    """Rules that protect the server. Times are in seconds."""

    per_hour: int = 10          # jobs one user may start in an hour
    fail_streak: int = 3        # failures in a row before a pause
    fail_pause: int = 300       # length of that pause
    fail_memory: int = 300      # how long a failed link is remembered
    job_timeout: int = 900      # longest a job may run
    sweep_every: int = 30       # how often the server tidies up
    calls_per_min: int = 60     # calls one user may make in a minute
    key_fails: int = 5          # wrong keys from one address ...
    key_fail_window: int = 60   # ... within this many seconds ...
    key_block: int = 900        # ... block that address for this long


@dataclass(frozen=True)
class Settings:
    """Server settings. Requests never change these."""

    api_key: str
    work_dir: Path = Path(tempfile.gettempdir()) / "video-tool-api"
    cookie_file: str | None = None
    ttl: int = 3600
    max_workers: int = 2
    limits: Limits = field(default_factory=Limits)
    api_key_next: str = ""       # a second key, so keys can be rotated
    trust_proxy: bool = False    # read the caller's address from the proxy

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(find_dotenv(usecwd=True), override=True)

        return cls(
            api_key=os.environ.get("VT_API_KEY", "").strip(),
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
                calls_per_min=_number("VT_API_CALLS_PER_MIN", 60),
                key_fails=_number("VT_API_KEY_FAILS", 5),
                key_block=_number("VT_API_KEY_BLOCK", 900),
            ),
            api_key_next=os.environ.get("VT_API_KEY_NEXT", "").strip(),
            trust_proxy=_flag("VT_API_TRUST_PROXY"),
        )


def key_matches(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(
        given.encode(), expected.encode()
    )


def key_ok(given: str, settings: Settings) -> bool:
    """True when ``given`` is the current key or the next key.

    Both are always compared, so the time taken does not show which
    one matched.
    """

    return key_matches(given, settings.api_key) | key_matches(
        given, settings.api_key_next
    )


def valid_user_id(value: str) -> bool:
    return bool(_USER_ID.fullmatch(value))

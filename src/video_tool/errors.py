"""Shared errors and plain-language error messages.

This module provides:

- application-specific exception classes
- ``explain()``, which turns any exception into an ``ErrorInfo``
- ``ffmpeg_failure()``, which turns ffmpeg's long log into one sentence
- ``format_cli()`` and ``report_error()`` for the command line

Every front end uses the same rules, so the same problem reads the same
way in Basic, Pro, merge, the compiler and the GUI.

An ``ErrorInfo`` holds text for people who are not technical. Technical
text, such as yt-dlp's own words or the end of ffmpeg's log, goes in
``detail``. The GUI never shows ``detail``. The command line shows it only
for errors that Video Tool cannot explain, or when asked.

Only ``report_error()`` prints. Nothing here exits, retries, or touches
files.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

try:
    from yt_dlp.utils import DownloadError
except ImportError:  # pragma: no cover

    class DownloadError(Exception):  # type: ignore[no-redef]
        """Fallback used when yt-dlp is not installed."""


class VideoToolError(RuntimeError):
    """Base class for errors Video Tool raises on purpose.

    The message is written for the person using the tool, so a front end
    can show it as it is. It subclasses RuntimeError so existing code that
    catches RuntimeError keeps working.

    ``detail`` may hold technical text for logs and ``--verbose``. It is
    never part of the message.
    """

    def __init__(self, *args, detail: str | None = None):
        super().__init__(*args)
        self.detail = detail


class MissingToolError(VideoToolError):
    """A program the task needs, such as ffmpeg, cannot be found."""


class ClipLengthError(VideoToolError):
    """A finished clip is shorter than the requested range.

    ``path`` is the file that was kept, so a caller can tell the user where
    it is. ``problems`` holds one plain sentence per stream that is short.
    ``wanted`` stores the requested duration.
    """

    def __init__(self, path, problems, wanted):
        self.path = path
        self.problems = list(problems)
        self.wanted = wanted

        super().__init__(
            "The clip does not match the range you asked for: "
            + "; ".join(self.problems)
            + "."
        )


@dataclass(frozen=True)
class ErrorInfo:
    """Structured information suitable for CLI or GUI presentation.

    ``message`` and ``hint`` are plain text for anyone. ``cli_hint`` names
    command-line options, so only the command line shows it. ``detail`` is
    technical text. ``code`` is a short stable name for the kind of problem.
    """

    level: str
    title: str
    message: str
    hint: str | None = None
    code: str = "unknown"
    detail: str | None = None
    cli_hint: str | None = None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_ANSI_CODES = re.compile(r"\x1b\[[0-9;]*m")


def clean_text(text: object) -> str:
    """Strip colour codes and yt-dlp's bug-report request from a message."""

    message = _ANSI_CODES.sub("", str(text)).strip()
    message = message.split("; please report this issue")[0].strip()

    return message.removeprefix("ERROR: ").strip()


# ---------------------------------------------------------------------------
# yt-dlp download errors
# ---------------------------------------------------------------------------

_LOGIN_CLI = (
    "Pass your login with --cookies-from-browser BROWSER "
    "or --cookies FILE."
)

# Each rule: code, lowercase phrases, level, title, message, hint, cli_hint.
# The first rule whose phrase appears in the error wins, so order matters:
# "confirm your age" must come before the general sign-in rule.
_DOWNLOAD_RULES = (
    (
        "age_restricted",
        ("age-restricted", "age restricted", "confirm your age"),
        "warning",
        "Age-restricted video",
        "YouTube only lets signed-in viewers watch this video.",
        "Video Tool cannot get it without your YouTube login.",
        _LOGIN_CLI,
    ),
    (
        "sign_in",
        ("sign in to confirm", "not a bot"),
        "warning",
        "YouTube wants you to sign in",
        "YouTube is asking for a login before it lets this video download.",
        "This sometimes works if you wait a while and try again.",
        "Or " + _LOGIN_CLI[0].lower() + _LOGIN_CLI[1:],
    ),
    (
        "private",
        ("private video", "video is private"),
        "error",
        "Private video",
        "This video is private. Only its owner and people they chose "
        "can watch it.",
        "Ask the owner to share it with you.",
        None,
    ),
    (
        "region",
        (
            "available in your country",
            "available in your region",
            "blocked it in your country",
            "blocked it in your region",
        ),
        "error",
        "Not available in your country",
        "YouTube blocks this video where you are.",
        "Try a different video.",
        None,
    ),
    (
        "copyright",
        ("copyright grounds", "copyright claim"),
        "error",
        "Blocked for copyright",
        "A copyright claim blocks this video.",
        "Try a different video.",
        None,
    ),
    (
        "not_started",
        ("live event will begin", "premieres in"),
        "warning",
        "Not ready yet",
        "This video is a live stream or premiere that has not ended.",
        "Try again after it ends.",
        None,
    ),
    (
        "unavailable",
        ("video unavailable", "video is unavailable", "video is not available"),
        "error",
        "Video not available",
        "The video was removed, or it is blocked where you are.",
        "Check that the link opens in your browser.",
        None,
    ),
    (
        "no_format",
        ("requested format is not available",),
        "warning",
        "Quality not available",
        "YouTube has no version of this video at the quality you picked.",
        "Pick a lower quality and try again.",
        "Try a lower quality, such as -q 720p.",
    ),
    (
        "http_403",
        ("http error 403",),
        "warning",
        "YouTube said no",
        "YouTube refused the request. The link may have expired.",
        "Run it again.",
        None,
    ),
    (
        "http_429",
        ("http error 429", "too many requests"),
        "warning",
        "Too many requests",
        "YouTube is limiting requests from this connection.",
        "Wait a few minutes, then try again.",
        None,
    ),
    (
        "http_404",
        ("http error 404",),
        "error",
        "Video not found",
        "YouTube could not find this video.",
        "Check the link.",
        None,
    ),
    (
        "disk_full",
        ("no space left on device",),
        "error",
        "Disk is full",
        "There is no space left on the disk.",
        "Free some space or pick another folder.",
        None,
    ),
    (
        "network",
        (
            "unable to download webpage",
            "unable to download api page",
            "certificate_verify_failed",
            "getaddrinfo failed",
            "temporary failure in name resolution",
            "name or service not known",
            "network is unreachable",
            "connection reset",
            "connection refused",
            "connection aborted",
            "remote end closed connection",
            "timed out",
            "timeout",
        ),
        "warning",
        "No connection to YouTube",
        "Video Tool could not reach YouTube.",
        "Check your internet connection and try again.",
        None,
    ),
    (
        "ffmpeg_stopped",
        ("ffmpeg exited with code",),
        "error",
        "Video processing stopped",
        "The program that cuts and joins video stopped with an error.",
        "Make sure FFmpeg is installed and up to date, then try again.",
        None,
    ),
)


_YOUTUBE_LINK = re.compile(r"youtube\.com|youtu\.be", re.IGNORECASE)


def explain_download_error(exc: BaseException) -> ErrorInfo:
    """Explain a yt-dlp failure. Unknown ones keep yt-dlp's text as detail."""

    raw = clean_text(exc)
    lower = raw.lower()

    # yt-dlp says "Unsupported URL" when no extractor fits the link. For a
    # YouTube link that nearly always means the video ID is wrong.
    if "is not a valid url" in lower:
        return ErrorInfo(
            level="error",
            title="Link not valid",
            message="This text is not a web address Video Tool can open.",
            hint=(
                "Remove any quote marks or spaces around the link, "
                "then copy the full link again."
            ),
            code="invalid_link",
            detail=raw or None,
        )

    if "unsupported url" in lower:
        if _YOUTUBE_LINK.search(raw):
            return ErrorInfo(
                level="error",
                title="Link not recognised",
                message=(
                    "This link does not lead to a YouTube video. "
                    "The video ID may be wrong or cut short."
                ),
                hint=(
                    "Copy the full link from your browser, "
                    "or use Share on the video."
                ),
                code="bad_link",
                detail=raw or None,
            )

        return ErrorInfo(
            level="error",
            title="Site not supported",
            message="Video Tool cannot download from this site.",
            hint="Check the link, or try a YouTube link.",
            code="unsupported_site",
            detail=raw or None,
        )

    for code, needles, level, title, message, hint, cli in _DOWNLOAD_RULES:
        if any(needle in lower for needle in needles):
            return ErrorInfo(
                level=level,
                title=title,
                message=message,
                hint=hint,
                code=code,
                detail=raw or None,
                cli_hint=cli,
            )

    return ErrorInfo(
        level="error",
        title="Download failed",
        message="Something went wrong while downloading this video.",
        hint="Try again. If it keeps happening, update yt-dlp.",
        code="download_failed",
        detail=raw or "The download failed.",
    )


# ---------------------------------------------------------------------------
# ffmpeg failures
# ---------------------------------------------------------------------------

# (lowercase phrases in ffmpeg's log, plain reason)
_FFMPEG_REASONS = (
    (
        ("invalid data found when processing input", "moov atom not found"),
        "The video file looks damaged or incomplete.",
    ),
    (
        ("no space left on device",),
        "There is no space left on the disk.",
    ),
    (
        ("permission denied",),
        "Video Tool is not allowed to write to that folder or file.",
    ),
    (
        ("no such file or directory",),
        "A file it needed was missing or had moved.",
    ),
    (
        ("unknown encoder", "encoder not found", "unrecognized option",
         "no such filter"),
        "This copy of FFmpeg is too old or lacks a feature Video Tool needs.",
    ),
    (
        ("do not match the corresponding output link", "input link parameters"),
        "The clips have different sizes and could not be matched.",
    ),
    (
        ("matches no streams", "stream specifier"),
        "A clip is missing the picture or sound track that was expected.",
    ),
    (
        ("server returned 403", "http error 403"),
        "The video link was refused or has expired.",
    ),
    (
        ("server returned 404", "http error 404"),
        "The video link no longer works.",
    ),
    (
        (
            "connection reset",
            "connection timed out",
            "connection refused",
            "network is unreachable",
            "name or service not known",
        ),
        "The internet connection dropped while the video was loading.",
    ),
)

_FFMPEG_ERROR_WORDS = (
    "error", "invalid", "failed", "no such", "cannot", "unable",
    "not found", "unrecognized", "unknown", "denied", "matches no",
    "does not", "not match",
)


def _ffmpeg_detail(stderr: str) -> str:
    """Keep the few lines of ffmpeg's log that say what went wrong."""

    lines = [line.strip() for line in str(stderr).splitlines()]
    lines = [line for line in lines if line]

    picked = [
        line
        for line in lines
        if any(word in line.lower() for word in _FFMPEG_ERROR_WORDS)
    ][-4:]

    if not picked:
        picked = lines[-3:]

    return " | ".join(picked)[:500]


def ffmpeg_failure(action: str, stderr: str) -> VideoToolError:
    """Build an error from a failed ffmpeg run.

    ``action`` finishes the sentence "Could not ...", for example
    ``"join the clips"``. The long log goes in ``detail``.
    """

    lower = str(stderr).lower()
    reason = "FFmpeg reported a problem."

    for needles, plain in _FFMPEG_REASONS:
        if any(needle in lower for needle in needles):
            reason = plain
            break

    return VideoToolError(
        f"Could not {action}. {reason}",
        detail=_ffmpeg_detail(stderr),
    )


# ---------------------------------------------------------------------------
# One translator for everything
# ---------------------------------------------------------------------------


def _name_of(exc: OSError) -> str:
    """Name the file or folder an operating-system error is about."""

    name = getattr(exc, "filename", None)

    return f"'{name}'" if name else "that file or folder"


def explain(exc: BaseException) -> ErrorInfo:
    """Convert any exception into plain, actionable text.

    ``level`` is one of ``info``, ``success``, ``warning`` or ``error``.
    """

    detail = getattr(exc, "detail", None)

    # FFmpeg is missing
    if isinstance(exc, MissingToolError):
        return ErrorInfo(
            level="error",
            title="FFmpeg is missing",
            message=str(exc) or "FFmpeg could not be found.",
            hint="Install FFmpeg, then try again.",
            code="missing_tool",
            cli_hint="Make sure it is on your PATH.",
        )

    # A clip came out shorter than requested
    if isinstance(exc, ClipLengthError):
        path = getattr(exc, "path", None)

        return ErrorInfo(
            level="warning",
            title="Clip is incomplete",
            message=str(exc)
            or "The clip is shorter than you asked for.",
            hint=(
                f"The incomplete file was kept at {path}."
                if path
                else "The incomplete file was kept so you can check it."
            ),
            code="clip_short",
        )

    # yt-dlp
    if isinstance(exc, DownloadError):
        return explain_download_error(exc)

    # Something ran too long
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return ErrorInfo(
            level="error",
            title="Took too long",
            message="The job ran for too long, so Video Tool stopped it.",
            hint="Try a shorter clip, or try again.",
            code="timeout",
        )

    # Operating-system errors. Errors Video Tool raises itself have no
    # errno and already carry a plain message.
    if isinstance(exc, PermissionError):
        return ErrorInfo(
            level="error",
            title="Permission denied",
            message=f"Video Tool is not allowed to use {_name_of(exc)}.",
            hint="Choose a folder you can write to.",
            code="permission",
        )

    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return ErrorInfo(
            level="error",
            title="Disk is full",
            message="There is no space left on the disk.",
            hint="Free some space or pick another folder.",
            code="disk_full",
        )

    if isinstance(exc, FileNotFoundError):
        own_message = getattr(exc, "errno", None) is None and str(exc)

        return ErrorInfo(
            level="error",
            title="File not found",
            message=(
                own_message
                or f"Video Tool could not find {_name_of(exc)}."
            ),
            hint="Check that the file still exists.",
            code="not_found",
        )

    # Errors Video Tool raises on purpose
    if isinstance(exc, VideoToolError):
        return ErrorInfo(
            level="error",
            title="Video Tool error",
            message=str(exc) or "The job could not be finished.",
            code="video_tool",
            detail=detail,
        )

    # Bad input. Video Tool's own messages are already plain.
    if isinstance(exc, ValueError):
        return ErrorInfo(
            level="error",
            title="Invalid input",
            message=str(exc) or "The input is not valid.",
            code="invalid_input",
        )

    if isinstance(exc, OSError):
        reason = getattr(exc, "strerror", None) or str(exc)

        return ErrorInfo(
            level="error",
            title="File problem",
            message=f"A file operation failed: {reason}".rstrip(": ")
            if reason
            else "A file operation failed.",
            code="file_error",
        )

    # Last resort
    return ErrorInfo(
        level="error",
        title="Unexpected error",
        message="Something unexpected went wrong.",
        hint="Try again. If it keeps happening, report the problem.",
        code="unexpected",
        detail=f"{type(exc).__name__}: {exc}".rstrip(": "),
        cli_hint="Run again with --verbose for technical details.",
    )


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

_SHOW_DETAIL_FOR = {"unexpected", "download_failed"}


def format_cli(
    info: ErrorInfo,
    source: str | None = None,
    *,
    verbose: bool = False,
) -> str:
    """Return the lines the command line prints for an error."""

    icon = "⚠️" if info.level == "warning" else "❌"
    where = f" ({source})" if source else ""

    lines = [f"{icon} {info.title}{where}", f"   {info.message}"]

    advice = " ".join(
        part for part in (info.hint, info.cli_hint) if part
    )

    if advice:
        lines.append(f"   What to do: {advice}")

    if info.detail and (verbose or info.code in _SHOW_DETAIL_FOR):
        first = info.detail.splitlines()[0][:300]
        lines.append(f"   Details: {first}")

    return "\n".join(lines)


def plain_message(exc: BaseException) -> str:
    """Return the message and the advice for ``exc`` as one short text."""

    info = explain(exc)

    return " ".join(part for part in (info.message, info.hint) if part)


def report_error(
    exc: BaseException,
    source: str | None = None,
    *,
    verbose: bool = False,
) -> None:
    """Print a plain-language error for ``exc``."""

    print(format_cli(explain(exc), source, verbose=verbose))

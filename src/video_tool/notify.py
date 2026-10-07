
"""Small notification interface shared by CLI and GUI front ends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .errors import ErrorInfo

NotificationCallback = Callable[[str, str], None]


def format_toast(info: ErrorInfo) -> str:
    """Join an explained error into toast text.

    The first line is the title. Only plain text is used: ``cli_hint`` and
    ``detail`` are for the command line and never reach a toast.
    """

    return "\n".join(
        part for part in (info.title, info.message, info.hint) if part
    )


@dataclass
class Notifier:
    """Notification sink.

    GUI front ends can provide a callback that receives:

        callback(level, message)

    CLI and other non-GUI callers can omit the callback and messages
    will be written to stdout.
    """

    callback: NotificationCallback | None = None

    def send(self, level: str, message: str) -> None:
        """Send a notification through the configured output."""

        if self.callback is not None:
            self.callback(level, message)
            return

        print(message)

    def info(self, message: str) -> None:
        """Send an informational notification."""

        self.send("info", message)

    def success(self, message: str) -> None:
        """Send a success notification."""

        self.send("success", message)

    def warn(self, message: str) -> None:
        """Send a warning notification."""

        self.send("warning", message)

    def error(self, message: str) -> None:
        """Send an error notification."""

        self.send("error", message)

    def progress(self, message: str) -> None:
        """Send a progress notification."""

        self.send("progress", message)

    def report(self, info: ErrorInfo) -> None:
        """Send an explained error at the level it asks for."""

        self.send(info.level, format_toast(info))

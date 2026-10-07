"""Timing and ordering rules for toasts, with no GUI code.

A toast is a short message that appears over the window and goes away by
itself. This module decides which toasts show, for how long, and how
repeats are merged. ``toast.py`` draws them.
"""

from __future__ import annotations

from dataclasses import dataclass

# Seconds a toast stays on screen. Errors stay longest, since they ask the
# person to do something.
LIFETIME = {
    "progress": 3.0,
    "info": 4.0,
    "success": 4.0,
    "warning": 15.0,
    "error": 30.0,
}

DEFAULT_LIFETIME = 5.0


@dataclass
class Toast:
    """One message. The first line of ``text`` is its title."""

    level: str
    text: str
    lifetime: float
    count: int = 1
    shown_at: float | None = None

    @property
    def title(self) -> str:
        first, _, _ = self.text.partition("\n")

        return first if "\n" in self.text else ""

    @property
    def body(self) -> str:
        _, _, rest = self.text.partition("\n")

        return rest if "\n" in self.text else self.text


class ToastQueue:
    """Keep a few toasts on screen and the rest waiting."""

    def __init__(self, max_visible: int = 3, max_waiting: int = 20) -> None:
        self.max_visible = max_visible
        self.max_waiting = max_waiting
        self._visible: list[Toast] = []
        self._waiting: list[Toast] = []

    @property
    def visible(self) -> list[Toast]:
        return list(self._visible)

    @property
    def waiting(self) -> list[Toast]:
        return list(self._waiting)

    def add(self, level: str, text: str, now: float) -> Toast:
        """Add a message.

        The same message twice does not stack. The first toast counts the
        repeats, so ten failed links show "x10", not ten toasts.
        """

        for toast in self._visible + self._waiting:
            if toast.level == level and toast.text == text:
                toast.count += 1

                if toast.shown_at is not None:
                    toast.shown_at = now

                return toast

        toast = Toast(
            level=level,
            text=text,
            lifetime=LIFETIME.get(level, DEFAULT_LIFETIME),
        )
        self._waiting.append(toast)

        while len(self._waiting) > self.max_waiting:
            self._waiting.pop(0)

        self.update(now)

        return toast

    def update(self, now: float) -> list[Toast]:
        """Retire old toasts, show waiting ones, and return what is visible."""

        self._visible = [
            toast
            for toast in self._visible
            if toast.shown_at is None
            or now - toast.shown_at < toast.lifetime
        ]

        while self._waiting and len(self._visible) < self.max_visible:
            toast = self._waiting.pop(0)
            toast.shown_at = now
            self._visible.append(toast)

        return self.visible

    def dismiss(self, toast: Toast, now: float) -> None:
        """Remove a toast the person clicked."""

        if toast in self._visible:
            self._visible.remove(toast)

        if toast in self._waiting:
            self._waiting.remove(toast)

        self.update(now)

    def clear(self) -> None:
        self._visible.clear()
        self._waiting.clear()

    def __bool__(self) -> bool:
        return bool(self._visible or self._waiting)

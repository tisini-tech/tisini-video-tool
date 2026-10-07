"""Toast messages for the Video Tool GUI.

Toasts show short messages at the top centre of the window, one
below the other, and fade out by themselves. A click removes one early.

Call :meth:`ToastHost.show` on the Tkinter main thread only. A worker
thread should send a ``("toast", (level, text))`` message through the
window's queue instead.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import customtkinter as ctk

from .toast_logic import Toast, ToastQueue

# (card colour, stripe colour) for each level
COLOURS = {
    "info": ("#26344f", "#4a90e2"),
    "progress": ("#26344f", "#4a90e2"),
    "success": ("#1d3b2a", "#3ecf8e"),
    "warning": ("#47391a", "#f5a623"),
    "error": ("#4a2226", "#ff5c5c"),
}

TEXT_COLOUR = "#f2f2f2"
WRAP = 400
TICK_MS = 250
MARGIN = 16
GAP = 8


class ToastHost:
    """Draws a :class:`ToastQueue` in the corner of ``window``."""

    def __init__(
        self,
        window: ctk.CTk,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_visible: int = 3,
    ) -> None:
        self.window = window
        self.clock = clock
        self.queue = ToastQueue(max_visible=max_visible)

        # Each card sits directly on the window. A shared container would
        # draw a box behind the cards in the window's background colour.
        self._cards: list[ctk.CTkFrame] = []
        self._shown: list[Toast] = []
        self._counts: list[int] = []
        self._ticking = False

        self._title_font = ctk.CTkFont(size=16, weight="bold")
        self._body_font = ctk.CTkFont(size=15)

    # ------------------------------------------------------------------

    def show(self, level: str, text: str) -> None:
        """Show a message. Main thread only."""

        self.queue.add(level, text, self.clock())
        self._refresh()

    def dismiss(self, toast: Toast) -> None:
        self.queue.dismiss(toast, self.clock())
        self._refresh()

    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        visible = self.queue.update(self.clock())
        counts = [toast.count for toast in visible]

        if visible != self._shown or counts != self._counts:
            self._redraw(visible)
            self._shown = list(visible)
            self._counts = counts

        if self.queue and not self._ticking:
            self._ticking = True
            self.window.after(TICK_MS, self._tick)

    def _tick(self) -> None:
        self._ticking = False

        try:
            if not self.window.winfo_exists():
                return
        except Exception:
            return

        self._refresh()

    def _redraw(self, visible: list[Toast]) -> None:
        for card in self._cards:
            card.destroy()

        self._cards = []

        # The oldest toast sits at the top, newer ones below it.
        offset = MARGIN

        for toast in visible:
            card = self._card(toast)
            card.update_idletasks()

            card.place(relx=0.5, rely=0.0, x=0, y=offset, anchor="n")
            card.lift()

            offset += card.winfo_reqheight() + GAP
            self._cards.append(card)

    def _card(self, toast: Toast) -> ctk.CTkFrame:
        card_colour, stripe_colour = COLOURS.get(
            toast.level, COLOURS["info"]
        )

        card = ctk.CTkFrame(
            self.window,
            width=1,
            height=1,
            corner_radius=10,
            fg_color=card_colour,
        )

        stripe = ctk.CTkFrame(
            card, width=6, height=1, corner_radius=3, fg_color=stripe_colour
        )
        stripe.pack(side="left", fill="y", padx=(8, 0), pady=8)

        inner = ctk.CTkFrame(
            card, width=1, height=1, fg_color="transparent"
        )
        inner.pack(side="left", padx=(10, 14), pady=8)

        widgets = [card, stripe, inner]

        title = toast.title

        if toast.count > 1:
            title = f"{title}  (x{toast.count})" if title else ""

        if title:
            label = ctk.CTkLabel(
                inner,
                text=title,
                font=self._title_font,
                text_color=TEXT_COLOUR,
                anchor="w",
                justify="left",
                wraplength=WRAP,
            )
            label.pack(anchor="w")
            widgets.append(label)

        body = toast.body

        if toast.count > 1 and not toast.title:
            body = f"{body}  (x{toast.count})"

        body_label = ctk.CTkLabel(
            inner,
            text=body,
            font=self._body_font,
            text_color=TEXT_COLOUR,
            anchor="w",
            justify="left",
            wraplength=WRAP,
        )
        body_label.pack(anchor="w", pady=(2, 0))
        widgets.append(body_label)

        for widget in widgets:
            widget.bind(
                "<Button-1>",
                lambda _event, item=toast: self.dismiss(item),
            )

        return card

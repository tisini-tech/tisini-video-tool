"""Tests that draw real toasts. They skip when there is no screen."""

import pytest

ctk = pytest.importorskip("customtkinter")

from video_tool.gui.toast import ToastHost  # noqa: E402
from video_tool.gui.toast_logic import LIFETIME  # noqa: E402


@pytest.fixture
def window():
    try:
        root = ctk.CTk()
    except Exception:
        pytest.skip("no display available")

    root.withdraw()
    yield root
    root.destroy()


def test_toasts_are_drawn_stacked_and_removed_when_they_expire(window):
    now = [0.0]
    host = ToastHost(window, clock=lambda: now[0])

    host.show("error", "Private video\nThis video is private.")
    host.show("success", "Download finished\nvideo.mp4")
    window.update()

    assert len(host._cards) == 2

    now[0] = LIFETIME["success"] + 1
    host._refresh()
    window.update()

    assert len(host._cards) == 1

    now[0] = LIFETIME["error"] + 1
    host._refresh()

    assert host._cards == []


def test_a_repeat_updates_the_count_instead_of_adding_a_card(window):
    host = ToastHost(window, clock=lambda: 0.0)

    host.show("error", "Private video\nThis video is private.")
    host.show("error", "Private video\nThis video is private.")
    window.update()

    assert len(host._cards) == 1
    assert host.queue.visible[0].count == 2


def test_clicking_removes_a_toast(window):
    host = ToastHost(window, clock=lambda: 0.0)

    host.show("info", "Hello")
    toast = host.queue.visible[0]
    host.dismiss(toast)

    assert host._cards == []

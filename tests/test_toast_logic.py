"""Tests for toast timing and ordering. No window is needed."""

from video_tool.gui.toast_logic import LIFETIME, ToastQueue


def test_a_new_toast_shows_at_once():
    queue = ToastQueue()
    toast = queue.add("info", "Hello", now=0)

    assert queue.visible == [toast]
    assert toast.shown_at == 0


def test_only_three_show_and_the_rest_wait():
    queue = ToastQueue(max_visible=3)

    for number in range(5):
        queue.add("info", f"message {number}", now=0)

    assert [t.text for t in queue.visible] == [
        "message 0", "message 1", "message 2",
    ]
    assert [t.text for t in queue.waiting] == ["message 3", "message 4"]


def test_a_waiting_toast_starts_its_clock_when_it_appears():
    queue = ToastQueue(max_visible=1)
    queue.add("info", "first", now=0)
    second = queue.add("info", "second", now=0)

    assert second.shown_at is None

    queue.update(now=LIFETIME["info"] + 1)

    assert [t.text for t in queue.visible] == ["second"]
    assert second.shown_at == LIFETIME["info"] + 1


def test_toasts_expire_and_errors_last_longest():
    queue = ToastQueue()
    queue.add("success", "done", now=0)
    queue.add("error", "failed", now=0)

    queue.update(now=LIFETIME["success"] + 0.1)
    assert [t.text for t in queue.visible] == ["failed"]

    queue.update(now=LIFETIME["error"] + 0.1)
    assert queue.visible == []
    assert not queue


def test_the_same_message_counts_up_instead_of_stacking():
    queue = ToastQueue()
    queue.add("error", "Private video", now=0)
    queue.add("error", "Private video", now=3)
    toast = queue.add("error", "Private video", now=6)

    assert len(queue.visible) == 1
    assert toast.count == 3


def test_a_repeat_gives_the_toast_more_time():
    queue = ToastQueue()
    toast = queue.add("error", "Private video", now=0)

    queue.add("error", "Private video", now=LIFETIME["error"] - 1)
    queue.update(now=LIFETIME["error"] + 1)

    assert toast in queue.visible


def test_same_text_at_another_level_is_a_different_toast():
    queue = ToastQueue()
    queue.add("info", "Ready", now=0)
    queue.add("error", "Ready", now=0)

    assert len(queue.visible) == 2


def test_clicking_a_toast_removes_it_and_lets_a_waiting_one_in():
    queue = ToastQueue(max_visible=1)
    first = queue.add("info", "first", now=0)
    queue.add("info", "second", now=0)

    queue.dismiss(first, now=1)

    assert [t.text for t in queue.visible] == ["second"]


def test_waiting_list_is_capped():
    queue = ToastQueue(max_visible=1, max_waiting=3)

    for number in range(10):
        queue.add("info", f"m{number}", now=0)

    assert len(queue.waiting) == 3
    assert queue.waiting[-1].text == "m9"


def test_first_line_is_the_title_when_there_are_several_lines():
    queue = ToastQueue()
    two = queue.add("error", "Private video\nThis video is private.", now=0)
    one = queue.add("info", "Saved.", now=0)

    assert (two.title, two.body) == ("Private video", "This video is private.")
    assert (one.title, one.body) == ("", "Saved.")

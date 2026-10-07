"""Tests for the shared plain-language error messages."""

import shutil
import subprocess

import pytest
from yt_dlp.utils import DownloadError

from video_tool import core
from video_tool.errors import (
    ClipLengthError,
    ErrorInfo,
    MissingToolError,
    VideoToolError,
    clean_text,
    explain,
    explain_download_error,
    ffmpeg_failure,
    format_cli,
    plain_message,
    report_error,
)
from video_tool.notify import Notifier, format_toast

# A shortened copy of what ffmpeg really prints when it cannot read a file.
FFMPEG_LOG = """\
ffmpeg version 6.1.1 Copyright (c) 2000-2023 the FFmpeg developers
  built with gcc 13
  configuration: --prefix=/usr --enable-gpl --enable-libx264 --enable-libx265
  libavutil      58. 29.100 / 58. 29.100
Input #0, lavfi, from 'testsrc':
  Duration: N/A, start: 0.000000, bitrate: N/A
[mov,mp4,m4a,3gp,3g2,mj2 @ 0x55] moov atom not found
[in#1 @ 0x55] Error opening input: Invalid data found when processing input
Error opening input file /tmp/broken.mp4.
"""


# ---------------------------------------------------------------- yt-dlp


@pytest.mark.parametrize(
    "raw, code",
    [
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "sign_in"),
        ("ERROR: [youtube] x: Sign in to confirm your age", "age_restricted"),
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private"),
        ("ERROR: [youtube] x: Video unavailable", "unavailable"),
        ("ERROR: [youtube] x: The uploader has not made this video available in your country", "region"),
        ("ERROR: Requested format is not available", "no_format"),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "http_403"),
        ("ERROR: HTTP Error 429: Too Many Requests", "http_429"),
        ("ERROR: [youtube] x: Unable to download API page: getaddrinfo failed", "network"),
        ("ERROR: ffmpeg exited with code 1", "ffmpeg_stopped"),
        ("ERROR: Premieres in 3 hours", "not_started"),
    ],
)
def test_download_errors_get_a_plain_explanation(raw, code):
    info = explain_download_error(DownloadError(raw))

    assert info.code == code
    assert info.message and info.title


def test_age_rule_wins_over_the_general_sign_in_rule():
    info = explain_download_error(
        DownloadError("Sign in to confirm your age")
    )

    assert info.code == "age_restricted"


def test_unknown_download_error_hides_yt_dlp_text_but_keeps_it_as_detail():
    info = explain_download_error(
        DownloadError("\x1b[0;31mERROR:\x1b[0m something odd happened")
    )

    assert info.code == "download_failed"
    assert "odd" not in info.message
    assert info.detail == "something odd happened"


def test_clean_text_strips_colour_prefix_and_bug_report_request():
    raw = "\x1b[0;31mERROR:\x1b[0m odd; please report this issue on the tracker"

    assert clean_text(raw) == "odd"


def test_text_the_gui_shows_never_names_command_line_options():
    """Options belong in cli_hint, which only the command line prints."""
    samples = [
        DownloadError("Sign in to confirm you're not a bot"),
        DownloadError("Sign in to confirm your age"),
        DownloadError("Requested format is not available"),
        MissingToolError("FFmpeg was not found."),
        RuntimeError("boom"),
        PermissionError(13, "Permission denied", "/x"),
    ]

    for exc in samples:
        info = explain(exc)
        shown = format_toast(info)

        assert "--" not in shown, shown
        assert "Traceback" not in shown


# ---------------------------------------------------------------- explain


def test_missing_ffmpeg():
    info = explain(MissingToolError("FFmpeg was not found."))

    assert info.code == "missing_tool"
    assert "Install FFmpeg" in info.hint


def test_clip_length_error_names_the_kept_file():
    exc = ClipLengthError("/tmp/a_INCOMPLETE.mp4", ["video is short"], 10)
    info = explain(exc)

    assert info.level == "warning"
    assert "_INCOMPLETE" in info.hint


def test_permission_error_names_the_file():
    info = explain(PermissionError(13, "Permission denied", "/root/video"))

    assert "'/root/video'" in info.message
    assert "Errno" not in info.message


def test_disk_full():
    info = explain(OSError(28, "No space left on device"))

    assert info.code == "disk_full"


def test_missing_file_from_the_operating_system_is_rewritten():
    info = explain(FileNotFoundError(2, "No such file", "/tmp/a.mp4"))

    assert info.message == "Video Tool could not find '/tmp/a.mp4'."


def test_missing_file_message_written_by_video_tool_is_kept():
    info = explain(FileNotFoundError("Input not found: /tmp/a.mp4"))

    assert info.message == "Input not found: /tmp/a.mp4"


def test_video_tool_error_keeps_its_own_message_and_detail():
    exc = VideoToolError("Could not join the clips.", detail="log tail")
    info = explain(exc)

    assert info.message == "Could not join the clips."
    assert info.detail == "log tail"


def test_unknown_exception_is_unexpected_and_keeps_detail():
    info = explain(RuntimeError("boom"))

    assert info.title == "Unexpected error"
    assert info.detail == "RuntimeError: boom"
    assert "boom" not in info.message


def test_timeout():
    info = explain(subprocess.TimeoutExpired("ffmpeg", 5))

    assert info.code == "timeout"


def test_plain_message_joins_message_and_hint():
    text = plain_message(MissingToolError("FFmpeg was not found."))

    assert text.startswith("FFmpeg was not found.")
    assert "Install FFmpeg" in text


# ---------------------------------------------------------------- ffmpeg


def test_ffmpeg_failure_is_one_sentence_not_the_whole_log():
    exc = ffmpeg_failure("join the clips", FFMPEG_LOG)

    assert isinstance(exc, VideoToolError)
    assert str(exc) == (
        "Could not join the clips. "
        "The video file looks damaged or incomplete."
    )
    assert "configuration" not in str(exc)


def test_ffmpeg_failure_keeps_the_useful_lines_as_detail():
    exc = ffmpeg_failure("join the clips", FFMPEG_LOG)

    assert "moov atom not found" in exc.detail
    assert "configuration" not in exc.detail
    assert len(exc.detail) <= 500


def test_ffmpeg_failure_with_an_unknown_reason_still_reads_well():
    exc = ffmpeg_failure("cut the clip", "something nobody has seen\n")

    assert str(exc) == "Could not cut the clip. FFmpeg reported a problem."


# ---------------------------------------------------------------- output


def test_format_cli_shows_title_message_advice_and_source():
    info = ErrorInfo(
        level="warning",
        title="Network down",
        message="Could not reach YouTube.",
        hint="Check the connection.",
        cli_hint="Try --attempts 3.",
        code="network",
        detail="socket stuff",
    )

    text = format_cli(info, "https://youtu.be/x")

    assert "Network down (https://youtu.be/x)" in text
    assert "Could not reach YouTube." in text
    assert "Check the connection. Try --attempts 3." in text
    assert "socket stuff" not in text
    assert "socket stuff" in format_cli(info, verbose=True)


def test_format_cli_shows_details_for_errors_it_cannot_explain():
    text = format_cli(explain(RuntimeError("boom")))

    assert "Unexpected error" in text
    assert "Details: RuntimeError: boom" in text


def test_report_error_prints(capsys):
    report_error(MissingToolError("FFmpeg was not found."), "job")

    assert "FFmpeg is missing (job)" in capsys.readouterr().out


def test_notifier_report_sends_plain_toast_text():
    seen = []
    notifier = Notifier(lambda level, text: seen.append((level, text)))

    notifier.report(explain(DownloadError("Private video")))

    level, text = seen[0]
    assert level == "error"
    assert text.startswith("Private video\n")
    assert "--" not in text


# ------------------------------------------------- the real failure it fixes


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is needed")
def test_a_real_failed_merge_does_not_dump_ffmpegs_log(tmp_path):
    good = tmp_path / "good.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(good),
        ],
        check=True,
    )
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(bytes(range(256)) * 200)

    with pytest.raises(VideoToolError) as caught:
        core.merge_clips(
            core.find_ffmpeg(),
            [str(good), str(broken)],
            str(tmp_path / "out.mp4"),
            preset="ultrafast",
        )

    message = str(caught.value)

    assert len(message) < 150, message
    assert "configuration" not in message
    assert "damaged" in message


# ------------------------------------------------------- unsupported links

UNSUPPORTED_YOUTUBE = (
    "ERROR: Unsupported URL: "
    "https://www.youtube.com/watch?v=VIDEO_ID&feature=youtu.be"
)


def test_unsupported_youtube_link_says_the_link_is_wrong():
    info = explain_download_error(DownloadError(UNSUPPORTED_YOUTUBE))

    assert info.code == "bad_link"
    assert "video ID" in info.message
    assert "yt-dlp" not in info.message
    assert "yt-dlp" not in (info.hint or "")
    assert info.detail.startswith("Unsupported URL")


def test_unsupported_other_site_says_the_site_is_not_supported():
    info = explain_download_error(
        DownloadError("ERROR: Unsupported URL: https://example.com/page")
    )

    assert info.code == "unsupported_site"
    assert "yt-dlp" not in (info.hint or "")


def test_unsupported_link_reads_plainly_on_the_command_line_and_in_a_toast():
    from video_tool.errors import format_cli

    info = explain_download_error(DownloadError(UNSUPPORTED_YOUTUBE))
    cli = format_cli(info, "https://youtu.be/VIDEO_ID")
    toast = format_toast(info)

    assert "What to do:" in cli
    assert "Something went wrong" not in cli
    assert toast.startswith("Link not recognised\n")
    assert "Unsupported URL" not in toast


# ------------------------------------------------- "video is unavailable"


@pytest.mark.parametrize(
    "raw",
    [
        "ERROR: [youtube] VIDhjgEO_ID: This video is unavailable",
        "ERROR: [youtube] x: Video unavailable",
        "ERROR: [youtube] x: This video is not available",
    ],
)
def test_unavailable_video_wordings_all_match(raw):
    info = explain_download_error(DownloadError(raw))

    assert info.code == "unavailable"
    assert "yt-dlp" not in (info.hint or "")


def test_unavailable_video_reads_plainly_on_the_command_line():
    from video_tool.errors import format_cli

    info = explain_download_error(
        DownloadError("ERROR: [youtube] VIDhjgEO_ID: This video is unavailable")
    )
    shown = format_cli(info, "https://youtu.be/VIDhjgEO_ID")

    assert "Something went wrong" not in shown
    assert "update yt-dlp" not in shown


# ------------------------------------------------------- "not a valid URL"


def test_link_with_quote_marks_gets_a_plain_message():
    from video_tool.errors import format_cli

    info = explain_download_error(
        DownloadError(
            "ERROR: [generic] '\"https://youtu.be/VIDEO_ID\"' "
            "is not a valid URL"
        )
    )
    toast = format_toast(info)

    assert info.code == "invalid_link"
    assert "yt-dlp" not in toast
    assert toast.startswith("Link not valid\n")
    assert "Something went wrong" not in format_cli(info)


# ------------------------------------------------------------ clean_url


def test_clean_url_removes_quotes_spaces_and_brackets():
    from video_tool.versions.common_cli import clean_url

    link = "https://youtu.be/abcdefghijk"

    assert clean_url(f'"{link}"') == link
    assert clean_url(f"'{link}'") == link
    assert clean_url(f"  <{link}>  ") == link
    assert clean_url(f"\u201c{link}\u201d") == link
    assert clean_url(link) == link


def test_parse_urls_accepts_a_quoted_link():
    from video_tool.versions.common_cli import parse_urls

    link = "https://youtu.be/abcdefghijk"

    assert parse_urls([f'"{link}"']) == [link]

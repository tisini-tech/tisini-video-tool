import pytest

import video_tool.youtube as youtube
from video_tool.core import MissingToolError
from video_tool.youtube import (
    DownloadOptions,
    build_ydl_options,
    describe_download_error,
    has_section,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Do not read the real youtube_cookies.txt, and pretend ffmpeg exists."""
    monkeypatch.setattr(youtube, "project_path", lambda *parts: "/nonexistent/x")
    monkeypatch.setattr(youtube, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")


def make_options(tmp_path, **changes):
    return DownloadOptions(output_dir=str(tmp_path), **changes)


def test_whole_video_options_are_unchanged(tmp_path):
    opts = build_ydl_options(make_options(tmp_path))

    assert "download_ranges" not in opts
    assert "external_downloader_args" not in opts
    assert "format_sort" not in opts
    assert "_trim_" not in opts["outtmpl"]


def test_has_section(tmp_path):
    assert not has_section(make_options(tmp_path))
    assert has_section(make_options(tmp_path, section_end=60))
    assert has_section(make_options(tmp_path, section_start=0))


def test_range_options_add_reconnect_and_encoder_settings(tmp_path):
    opts = build_ydl_options(
        make_options(tmp_path, section_start=30760, section_end=32000)
    )

    assert opts["force_keyframes_at_cuts"] is True
    assert "download_ranges" in opts
    assert opts["format_sort"][:3] == ["res:1080", "fps", "vcodec:h264"]

    args = opts["external_downloader_args"]
    assert "-reconnect" in args["ffmpeg_i"]
    assert args["ffmpeg_o"][args["ffmpeg_o"].index("-preset") + 1] == "veryfast"
    assert "-vf" not in args["ffmpeg_o"]


def test_range_file_name_is_marked_and_has_no_colons(tmp_path):
    opts = build_ydl_options(
        make_options(tmp_path, section_start=30760, section_end=32000)
    )

    name = opts["outtmpl"].rsplit("/", 1)[-1]
    assert "_trim_8h32m40s-8h53m20s" in name
    assert ":" not in name


def test_range_fps_option_adds_a_filter(tmp_path):
    opts = build_ydl_options(
        make_options(tmp_path, section_end=60, range_fps=30)
    )

    out = opts["external_downloader_args"]["ffmpeg_o"]
    assert out[out.index("-vf") + 1] == "fps=30"


def test_audio_range_has_no_video_settings(tmp_path):
    opts = build_ydl_options(
        make_options(tmp_path, format_type="MP3", section_start=10, section_end=20)
    )

    assert "format_sort" not in opts
    assert "ffmpeg_o" not in opts["external_downloader_args"]
    assert "ffmpeg_i" in opts["external_downloader_args"]


@pytest.mark.parametrize(
    ("start", "end"),
    [(60, 30), (60, 60), (-5, 30)],
)
def test_invalid_ranges_are_rejected(tmp_path, start, end):
    with pytest.raises(ValueError, match="time"):
        build_ydl_options(make_options(tmp_path, section_start=start, section_end=end))


def test_range_without_ffmpeg_fails_with_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "find_ffmpeg", lambda: None)

    with pytest.raises(MissingToolError, match="ffmpeg"):
        build_ydl_options(make_options(tmp_path, section_end=60))


def test_whole_video_without_ffmpeg_still_builds(tmp_path, monkeypatch):
    # Existing behaviour: only range downloads insist on ffmpeg up front.
    monkeypatch.setattr(youtube, "find_ffmpeg", lambda: None)

    assert "outtmpl" in build_ydl_options(make_options(tmp_path))


def test_describe_download_error_gives_advice():
    bot = Exception("ERROR: [youtube] x: Sign in to confirm you're not a bot")
    assert "cookie" in describe_download_error(bot)

    fmt = Exception("ERROR: Requested format is not available")
    assert "-q 720p" in describe_download_error(fmt)


def test_describe_download_error_keeps_unknown_text_and_strips_colour():
    exc = Exception("\x1b[0;31mERROR:\x1b[0m something odd happened")

    assert describe_download_error(exc) == "something odd happened"


def test_describe_download_error_explains_network_problems():
    exc = Exception(
        "ERROR: [youtube] x: Unable to download API page: "
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed; "
        "please report this issue on  https://github.com/yt-dlp/yt-dlp/issues"
    )

    assert "connection" in describe_download_error(exc)


def test_describe_download_error_drops_the_bug_report_request():
    exc = Exception("ERROR: odd failure; please report this issue on the tracker")

    assert describe_download_error(exc) == "odd failure"
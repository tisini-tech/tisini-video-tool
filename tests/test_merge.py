from pathlib import Path

import pytest

from video_tool.core import VideoToolError, find_ffmpeg, find_ffprobe
from video_tool.versions import merge_tool as merge
from video_tool.youtube import DownloadOptions

needs_ffmpeg = pytest.mark.skipif(
    find_ffmpeg() is None or find_ffprobe() is None,
    reason="FFmpeg and FFprobe are not installed",
)


@pytest.fixture
def options(tmp_path):
    return DownloadOptions(output_dir=str(tmp_path))


def test_resolve_local_file(tmp_path, options):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    path = merge.resolve_source(
        str(clip), options, tmp_path,
        start=None, end=None, use_cache=True,
        full_download=False, attempts=1,
    )
    assert Path(path) == clip.resolve()


def test_resolve_rejects_unknown_source(tmp_path, options):
    with pytest.raises(VideoToolError, match="not a YouTube URL"):
        merge.resolve_source(
            "not-a-file-or-url", options, tmp_path,
            start=None, end=None, use_cache=True,
            full_download=False, attempts=1,
        )


def test_resolve_youtube_calls_fetch_clip(tmp_path, options, monkeypatch):
    seen = {}

    def fake_fetch(url, staging, start, end, opts, **kwargs):
        seen["url"] = url
        seen["staging"] = Path(staging)
        out = tmp_path / "from-yt.mp4"
        out.write_bytes(b"y")
        return out, True

    monkeypatch.setattr(merge, "fetch_clip", fake_fetch)

    path = merge.resolve_source(
        "https://youtu.be/abc123", options, tmp_path,
        start=None, end=None, use_cache=True,
        full_download=False, attempts=1,
    )
    assert seen["url"] == "https://youtu.be/abc123"
    assert Path(path).name == "from-yt.mp4"


def test_main_needs_two_sources(tmp_path, capsys):
    clip = tmp_path / "only.mp4"
    clip.write_bytes(b"x")
    code = merge.main(["-o", str(tmp_path / "out.mp4"), str(clip)])
    assert code == 2
    assert "at least two" in capsys.readouterr().out


def test_main_bad_times_exit_2(tmp_path, capsys):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    code = merge.main([
        "-o", str(tmp_path / "out.mp4"),
        "--start", "50", "--end", "10",
        str(a), str(b),
    ])
    assert code == 2
    assert "End time" in capsys.readouterr().out


def test_one_bad_source_does_not_merge(tmp_path, monkeypatch, capsys):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")

    def boom(*a, **k):
        raise VideoToolError("download exploded")

    monkeypatch.setattr(merge, "fetch_clip", boom)
    monkeypatch.setattr(
        merge, "merge_clips",
        lambda **k: pytest.fail("must not merge after a failed source"),
    )

    code = merge.main([
        "-o", str(tmp_path / "out.mp4"),
        str(clip),
        "https://youtu.be/abc123",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "Merge stopped" in out
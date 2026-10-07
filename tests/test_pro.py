
from __future__ import annotations

from pathlib import Path

import pytest

from video_tool import clip_source
from video_tool.errors import ClipLengthError, VideoToolError
from video_tool.versions import video_tool_pro as pro


def make_clip(path: Path, duration: float) -> Path:
    """Create a small valid MP4 test clip using ffmpeg."""

    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return path


def test_expected_length():
    assert clip_source.expected_length(10, 70) == 60
    assert clip_source.expected_length(None, 70) == 70
    assert clip_source.expected_length(10, None) is None


def test_range_download_returns_a_complete_clip(
    tmp_path,
    monkeypatch,
):
    good = make_clip(
        tmp_path / "clip_trim_x.mp4",
        10,
    )

    calls = []

    def fake_download(url, options):
        calls.append((url, options))
        return str(good)

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    path, downloaded = clip_source.download_range(
        "u",
        clip_source.DownloadOptions(
            output_dir=tmp_path,
            format_type="MP4",
        ),
        0,
        10,
    )

    assert path == good
    assert downloaded is True
    assert len(calls) == 1
    assert calls[0][0] == "u"
    assert calls[0][1].section_start == 0
    assert calls[0][1].section_end == 10


def test_short_clip_is_kept_renamed_and_retried(
    tmp_path,
    monkeypatch,
):
    first = make_clip(
        tmp_path / "clip_trim_x.mp4",
        4,
    )

    second = make_clip(
        tmp_path / "clip_trim_x_retry.mp4",
        10,
    )

    paths = iter([first, second])
    calls = []

    def fake_download(url, options):
        calls.append((url, options))

        path = next(paths)

        # Simulate yt-dlp producing the same output filename on every
        # attempt. The first file is renamed by validation.
        if len(calls) == 2:
            target = tmp_path / "clip_trim_x.mp4"
            path.rename(target)
            return str(target)

        return str(path)

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    path, downloaded = clip_source.download_range(
        "u",
        clip_source.DownloadOptions(
            output_dir=tmp_path,
            format_type="MP4",
        ),
        0,
        10,
        attempts=2,
    )

    assert downloaded is True
    assert len(calls) == 2
    assert path.is_file()
    assert "_INCOMPLETE" not in path.name

    incomplete_files = list(
        tmp_path.glob("*_INCOMPLETE*.mp4")
    )

    assert incomplete_files


def test_still_short_after_all_attempts_raises_and_keeps_file(
    tmp_path,
    monkeypatch,
):
    short = make_clip(
        tmp_path / "clip_trim_x.mp4",
        4,
    )

    def fake_download(url, options):
        # Re-create the expected output after a failed attempt so that
        # each retry has a fresh file to validate.
        target = tmp_path / "clip_trim_x.mp4"

        if not target.exists():
            short.rename(target)
        elif target != short:
            replacement = make_clip(
                target,
                4,
            )
            return str(replacement)

        return str(target)

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    with pytest.raises(ClipLengthError) as caught:
        clip_source.download_range(
            "u",
            clip_source.DownloadOptions(
                output_dir=tmp_path,
                format_type="MP4",
            ),
            0,
            10,
            attempts=2,
        )

    assert caught.value.path.exists()
    assert caught.value.path.name.endswith(
        "_INCOMPLETE.mp4"
    )

    assert "does not match the range" in str(caught.value)


def test_download_error_is_retried(
    tmp_path,
    monkeypatch,
):
    calls = []

    def fake_download(url, options):
        calls.append((url, options))
        raise clip_source.DownloadError(
            "temporary failure"
        )

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    with pytest.raises(clip_source.DownloadError):
        clip_source.download_range(
            "u",
            clip_source.DownloadOptions(
                output_dir=tmp_path,
                format_type="MP4",
            ),
            0,
            10,
            attempts=3,
        )

    assert len(calls) == 3


def test_missing_end_raises_video_tool_error(
    tmp_path,
):
    with pytest.raises(VideoToolError, match="--end"):
        clip_source.download_range(
            "u",
            clip_source.DownloadOptions(
                output_dir=tmp_path,
                format_type="MP4",
            ),
            0,
            None,
        )


def test_main_success_returns_zero(
    tmp_path,
    monkeypatch,
    capsys,
):
    output = tmp_path / "download.mp4"

    def fake_download(url, options):
        output.write_bytes(b"video")
        return str(output)

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    result = pro.main(
        [
            "https://youtube.com/watch?v=test",
            "-o",
            str(tmp_path),
            "-f",
            "MP4",
        ]
    )

    captured = capsys.readouterr()

    assert result == 0
    assert "Saved:" in captured.out


def test_main_reports_short_clip_and_exits_1(
    tmp_path,
    monkeypatch,
    capsys,
):
    short = make_clip(
        tmp_path / "short.mp4",
        4,
    )

    def fake_download(url, options):
        target = tmp_path / "clip_trim_x.mp4"
        short.rename(target)
        return str(target)

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    result = pro.main(
        [
            "https://youtube.com/watch?v=test",
            "-o",
            str(tmp_path),
            "-f",
            "MP4",
            "--start",
            "0",
            "--end",
            "10",
            "--attempts",
            "1",
        ]
    )

    captured = capsys.readouterr()

    assert result == 1
    assert "does not match the range" in captured.out
    assert "_INCOMPLETE" in captured.out
    assert "Saved:" not in captured.out


def test_main_bad_times_exit_2(
    tmp_path,
):
    result = pro.main(
        [
            "https://youtube.com/watch?v=test",
            "-o",
            str(tmp_path),
            "--start",
            "20",
            "--end",
            "10",
        ]
    )

    assert result == 2


def test_main_no_valid_url_exits_2(
    tmp_path,
):
    result = pro.main(
        [
            "https://example.com",
            "-o",
            str(tmp_path),
        ]
    )

    assert result == 2


def test_main_range_without_end_exits_1(
    tmp_path,
    monkeypatch,
    capsys,
):
    def fake_download(url, options):
        raise AssertionError(
            "download_url should not be called"
        )

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    result = pro.main(
        [
            "https://youtube.com/watch?v=test",
            "-o",
            str(tmp_path),
            "-f",
            "MP4",
            "--start",
            "10",
        ]
    )

    captured = capsys.readouterr()

    assert result == 1
    assert "--end" in captured.out


def test_main_unexpected_error_does_not_crash(
    tmp_path,
    monkeypatch,
    capsys,
):
    def fake_download(url, options):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    result = pro.main(
        [
            "https://youtube.com/watch?v=test",
            "-o",
            str(tmp_path),
            "-f",
            "MP4",
        ]
    )

    captured = capsys.readouterr()

    assert result == 1
    assert "Unexpected error" in captured.out


def test_main_ctrl_c_exits_130(
    tmp_path,
    monkeypatch,
    capsys,
):
    def fake_download(url, options):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    result = pro.main(
        [
            "https://youtube.com/watch?v=test",
            "-o",
            str(tmp_path),
            "-f",
            "MP4",
        ]
    )

    captured = capsys.readouterr()

    assert result == 130
    assert "Cancelled" in captured.out


def test_one_failed_url_does_not_stop_the_next(
    tmp_path,
    monkeypatch,
    capsys,
):
    good = tmp_path / "ok.mp4"
    good.write_bytes(b"x")

    calls = []

    def fake_download(url, options):
        calls.append(url)

        if "bad" in url:
            raise clip_source.DownloadError(
                "Private video"
            )

        return str(good)

    monkeypatch.setattr(
        clip_source,
        "download_url",
        fake_download,
    )

    result = pro.main(
        [
            "https://youtube.com/watch?v=bad",
            "https://youtube.com/watch?v=good",
            "-o",
            str(tmp_path),
            "-f",
            "MP4",
        ]
    )

    captured = capsys.readouterr()

    assert result == 1
    assert len(calls) == 2
    assert "private" in captured.out.lower()
    assert "Completed 1/2" in captured.out


# ---------------------------------------------------------------------------
# Local-file Pro tests
# ---------------------------------------------------------------------------


def test_main_extracts_section_from_local_file(
    tmp_path,
    capsys,
):
    source = make_clip(
        tmp_path / "episode.mp4",
        10,
    )

    output_dir = tmp_path / "clips"

    result = pro.main(
        [
            str(source),
            "-o",
            str(output_dir),
            "-f",
            "MP4",
            "--start",
            "2",
            "--end",
            "6",
        ]
    )

    captured = capsys.readouterr()

    assert result == 0
    assert "Saved:" in captured.out

    outputs = list(output_dir.glob("*.mp4"))

    assert len(outputs) == 1
    assert outputs[0].is_file()

    # The local original must always remain untouched.
    assert source.is_file()


def test_local_file_with_spaces_is_accepted(
    tmp_path,
    capsys,
):
    source_dir = tmp_path / "My Match Footage"
    source = make_clip(
        source_dir / "Episode 1.mp4",
        10,
    )

    output_dir = tmp_path / "clips"

    result = pro.main(
        [
            str(source),
            "-o",
            str(output_dir),
            "-f",
            "MP4",
            "--start",
            "2",
            "--end",
            "5",
        ]
    )

    captured = capsys.readouterr()

    assert result == 0
    assert "Saved:" in captured.out
    assert source.is_file()


def test_local_file_requires_a_range(
    tmp_path,
    capsys,
):
    source = make_clip(
        tmp_path / "episode.mp4",
        10,
    )

    result = pro.main(
        [
            str(source),
            "-o",
            str(tmp_path / "clips"),
            "-f",
            "MP4",
        ]
    )

    captured = capsys.readouterr()

    assert result == 1
    assert "requires --start and/or --end" in captured.out
    assert source.is_file()
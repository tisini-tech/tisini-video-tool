"""Tests for core.merge_clips with clips in different formats."""

import shutil
import subprocess
from pathlib import Path

import pytest

from video_tool import core

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are needed",
)


def make_clip(
    path: Path,
    seconds: int,
    size: str,
    rate: int,
    sample_rate: int | None = 48000,
) -> Path:
    """Make a small test clip. ``sample_rate=None`` gives no audio."""
    cmd = [
        "ffmpeg", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"testsrc=duration={seconds}:size={size}:rate={rate}",
    ]

    if sample_rate:
        cmd += [
            "-f", "lavfi",
            "-i", f"sine=duration={seconds}:sample_rate={sample_rate}",
            "-c:a", "aac", "-shortest",
        ]
    else:
        cmd += ["-an"]

    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def frame_seconds(path: Path) -> set[int]:
    """Whole seconds of the file that hold at least one video frame."""
    run = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=pts_time", "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    times = (line.split(",")[0] for line in run.stdout.split())
    return {int(float(t)) for t in times if t}


def size_of(path: Path) -> tuple[int, int]:
    run = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    width, height = run.stdout.strip().split(",")[:2]
    return int(width), int(height)


def test_different_formats_join_without_gaps(tmp_path):
    a = make_clip(tmp_path / "a.mp4", 4, "640x360", 25, 44100)
    b = make_clip(tmp_path / "b.mp4", 2, "1280x720", 30, 48000)
    out = tmp_path / "out.mp4"

    core.merge_clips(
        core.find_ffmpeg(), [str(a), str(b)], str(out),
        preset="ultrafast",
    )

    length = core.probe_durations(str(out))["file"]
    assert length == pytest.approx(6.0, abs=0.3)
    assert size_of(out) == (640, 360)
    assert frame_seconds(out) == set(range(6))


def test_matching_clips_stay_lossless(tmp_path, capsys):
    a = make_clip(tmp_path / "a.mp4", 3, "640x360", 25)
    b = make_clip(tmp_path / "b.mp4", 2, "640x360", 25)
    out = tmp_path / "out.mp4"

    core.merge_clips(core.find_ffmpeg(), [str(a), str(b)], str(out))

    assert "Lossless merge complete" in capsys.readouterr().out
    assert core.probe_durations(str(out))["file"] == pytest.approx(
        5.0, abs=0.3
    )


def test_clip_without_audio_gets_silence(tmp_path):
    a = make_clip(tmp_path / "a.mp4", 3, "640x360", 25)
    b = make_clip(tmp_path / "b.mp4", 2, "640x360", 25, None)
    out = tmp_path / "out.mp4"

    core.merge_clips(
        core.find_ffmpeg(), [str(a), str(b)], str(out),
        preset="ultrafast",
    )

    lengths = core.probe_durations(str(out))
    assert lengths["file"] == pytest.approx(5.0, abs=0.3)
    assert lengths["audio"] == pytest.approx(5.0, abs=0.3)


def test_short_lossless_result_falls_back(tmp_path, monkeypatch, capsys):
    """If the comparison misses a mismatch, the length check catches it."""
    a = make_clip(tmp_path / "a.mp4", 4, "640x360", 25, 44100)
    b = make_clip(tmp_path / "b.mp4", 2, "1280x720", 30, 48000)
    out = tmp_path / "out.mp4"

    monkeypatch.setattr(core, "_can_copy_merge", lambda streams: True)

    core.merge_clips(
        core.find_ffmpeg(), [str(a), str(b)], str(out),
        preset="ultrafast",
    )

    assert "not as long as its parts" in capsys.readouterr().out
    assert core.probe_durations(str(out))["file"] == pytest.approx(
        6.0, abs=0.3
    )
    assert frame_seconds(out) == set(range(6))

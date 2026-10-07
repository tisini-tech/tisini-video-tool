import csv
import os

import pytest

from video_tool.core import (
    add_to_cache,
    check_clip_length,
    extract_video_id,
    find_ffmpeg,
    find_ffprobe,
    get_cached_path,
    is_valid_youtube_url,
    load_cache,
    parse_time_to_seconds,
    probe_durations,
    read_csv_entries,
    resolve_cached_file,
    seconds_to_timestamp,
    trim_label,
)


def test_time_parsing():
    assert parse_time_to_seconds("45") == 45.0
    assert parse_time_to_seconds("1:30") == 90.0
    assert parse_time_to_seconds("1:23:45") == 5025.0
    assert parse_time_to_seconds("") is None


def test_timestamp_round_trip():
    assert parse_time_to_seconds(seconds_to_timestamp(90)) == 90.0
    assert parse_time_to_seconds(seconds_to_timestamp(5025)) == 5025.0


def test_youtube_url_helpers():
    assert is_valid_youtube_url("https://www.youtube.com/watch?v=abc123")
    assert is_valid_youtube_url("https://youtu.be/abc123")
    assert is_valid_youtube_url("https://www.youtube.com/shorts/abc123")
    assert not is_valid_youtube_url("https://google.com")
    assert extract_video_id("https://youtu.be/abc123") == "abc123"


def test_live_urls_are_accepted():
    url = "https://www.youtube.com/live/fSeAM-mDvqo?si=abc"
    assert is_valid_youtube_url(url)
    assert extract_video_id(url) == "fSeAM-mDvqo"


def test_trim_label_has_no_colons():
    assert trim_label(30760, 32000) == "8h32m40s-8h53m20s"
    assert trim_label(90, 120) == "1m30s-2m00s"
    assert trim_label(None, 60) == "start-1m00s"
    assert trim_label(10, None) == "0m10s-end"
    assert ":" not in trim_label(30760, 32000)


def test_cache_roundtrip(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    real_video = tmp_path / "video.mp4"
    real_video.write_bytes(b"video")

    import video_tool.core as core

    monkeypatch.setattr(core, "CACHE_FILE", str(cache_file))

    cache = {}
    add_to_cache(cache, "abc123", "MP4", str(real_video))

    assert get_cached_path(cache, "abc123", "MP4") == str(real_video)
    assert get_cached_path(cache, "abc123", "MP3") is None
    assert load_cache() == cache


def test_csv_reading(tmp_path):
    csv_path = tmp_path / "clips.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["player", "match_id", "action", "start_time", "end_time"])
        writer.writerow(["Player1", "vid1", "goal", "0:05", "0:10"])
        writer.writerow(["Player1", "vid1", "shot", "1:30", "1:35"])

    entries = read_csv_entries(str(csv_path))

    assert len(entries) == 2
    assert entries[0]["start"] == 5.0
    assert entries[1]["end"] == 95.0


@pytest.mark.skipif(find_ffmpeg() is None, reason="FFmpeg is not installed")
def test_ffmpeg_available():
    assert os.path.basename(find_ffmpeg()).startswith("ffmpeg")


def test_resolve_cached_file_from_directory(tmp_path):
    local = tmp_path / "abc123.mp4"
    local.write_bytes(b"video")

    assert resolve_cached_file(
        "https://youtu.be/abc123",
        "MP4",
        cache={},
        directories=[str(tmp_path)],
    ) == str(local)


needs_ffmpeg = pytest.mark.skipif(
    find_ffmpeg() is None or find_ffprobe() is None,
    reason="FFmpeg and FFprobe are not installed",
)


def _make_clip(path, video_seconds, audio_seconds):
    """Write a tiny test clip whose video and audio lengths can differ."""
    import subprocess

    video_source = f"testsrc2=duration={video_seconds}:size=160x90:rate=10"
    subprocess.run(
        [
            find_ffmpeg(), "-v", "error", "-y",
            "-f", "lavfi", "-i", video_source,
            "-f", "lavfi", "-i", f"sine=duration={audio_seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            str(path),
        ],
        check=True,
    )


@needs_ffmpeg
def test_check_clip_length_accepts_a_full_clip(tmp_path):
    clip = tmp_path / "full.mp4"
    _make_clip(clip, 10, 10)

    assert check_clip_length(str(clip), 10) == []


@needs_ffmpeg
def test_check_clip_length_flags_a_short_clip(tmp_path):
    clip = tmp_path / "short.mp4"
    _make_clip(clip, 4, 4)

    problems = check_clip_length(str(clip), 10)

    assert len(problems) == 2
    assert "video" in problems[0] and "audio" in problems[1]


@needs_ffmpeg
def test_check_clip_length_flags_video_that_stops_early(tmp_path):
    # The "picture freezes while sound plays" case: audio is complete,
    # so a check on the whole file length alone would pass.
    clip = tmp_path / "video_short.mp4"
    _make_clip(clip, 4, 10)

    problems = check_clip_length(str(clip), 10)

    assert len(problems) == 1
    assert problems[0].startswith("video")


@needs_ffmpeg
def test_check_clip_length_can_check_audio_only(tmp_path):
    clip = tmp_path / "video_short.mp4"
    _make_clip(clip, 4, 10)

    assert check_clip_length(str(clip), 10, kinds=("audio",)) == []


def test_check_clip_length_reports_unreadable_file(tmp_path):
    bad = tmp_path / "not_a_video.mp4"
    bad.write_bytes(b"not a video")

    assert probe_durations(str(bad)) == {"file": None, "video": None, "audio": None}
    assert check_clip_length(str(bad), 10) != []
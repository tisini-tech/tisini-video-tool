import csv
import os

import pytest

from video_tool.core import (
    add_to_cache,
    extract_video_id,
    find_ffmpeg,
    get_cached_path,
    is_valid_youtube_url,
    load_cache,
    parse_time_to_seconds,
    read_csv_entries,
    resolve_cached_file,
    seconds_to_timestamp,
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

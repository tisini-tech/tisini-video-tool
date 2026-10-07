"""Shared test setup."""

import pytest

from video_tool import core


@pytest.fixture(autouse=True)
def isolate_runtime_state(tmp_path, monkeypatch):
    """Keep tests away from the real cache, count and config files.

    Without this, tests read and write ~/.cache/video-tool, so a leftover
    entry from one run can change the result of the next.
    """
    config_dir = tmp_path / "vt-config"
    cache_dir = tmp_path / "vt-cache"

    monkeypatch.setattr(core, "VIDEO_TOOL_CONFIG_DIR", config_dir)
    monkeypatch.setattr(core, "VIDEO_TOOL_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        core, "CACHE_FILE", str(cache_dir / "cache.json")
    )
    monkeypatch.setattr(
        core, "CONFIG_FILE", str(config_dir / "config.json")
    )
    monkeypatch.setattr(
        core,
        "DOWNLOAD_COUNT_FILE",
        str(cache_dir / "download_count.json"),
    )

    # find_local_cached_file() also scans ~/Downloads/video-tool for any
    # file whose name contains the video id. Tests use ids such as "test"
    # and "good", so a real download with that text in its name would be
    # picked up and trimmed with ffmpeg. Never scan real folders in tests.
    monkeypatch.setattr(
        core, "default_download_directories", lambda: []
    )

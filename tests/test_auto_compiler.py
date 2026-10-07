from pathlib import Path

import pytest

from video_tool.versions import auto_compiler as ac
from video_tool.versions.auto_compiler import (
    RANGE_CLIP_LIMIT,
    AutoCompiler,
    choose_fetch_strategy,
    count_by_match,
    detect_source_type,
    event_tags,
    match_key,
    matches_event,
)

URL = "https://www.youtube.com/watch?v=abc123"
URL_SHORT = "https://youtu.be/abc123"


def make_entry(**changes):
    row = {
        "player": "Ada",
        "match_id": URL,
        "action": "Goal (Header)",
        "start": 10.0,
        "end": 20.0,
        "start_str": "0:10",
        "end_str": "0:20",
    }
    row.update(changes)
    return row


def strategy(**changes):
    values = dict(
        has_local_or_cache=False,
        is_youtube=True,
        clips_on_this_source=3,
        event_filter=None,
        fetch_mode="auto",
        range_clip_limit=RANGE_CLIP_LIMIT,
        has_end=True,
    )
    values.update(changes)
    return choose_fetch_strategy(**values)


# --- pure helpers ---------------------------------------------------------------

def test_detect_youtube_and_unknown():
    assert detect_source_type(URL)[0] == "youtube"
    assert detect_source_type("not-a-file") == ("cache_key", "not-a-file")
    assert detect_source_type("")[0] == "unknown"


def test_detect_local_file(tmp_path):
    video = tmp_path / "match.mp4"
    video.write_bytes(b"x")
    kind, path = detect_source_type(str(video))
    assert kind == "local"
    assert path == str(video.resolve())


def test_match_key_groups_url_forms():
    assert match_key(URL) == match_key(URL_SHORT) == "abc123"


def test_count_by_match_groups_same_video():
    entries = [
        make_entry(match_id=URL),
        make_entry(match_id=URL_SHORT),
        make_entry(match_id="https://youtu.be/other1"),
    ]
    counts = count_by_match(entries)
    assert counts["abc123"] == 2
    assert counts["other1"] == 1


def test_event_tags_and_matches():
    assert event_tags("Shot (Out-box Off Target)") == ["Shot"]
    assert event_tags("Goal (Free Kick) + Shot (In-box On Target)") == ["Goal", "Shot"]
    assert event_tags("Blocks (Shot)") == ["Blocks"]
    assert matches_event("Goal (Header)", "Goal")
    assert matches_event("Shot (Blocked)", "shot")
    assert not matches_event("Goal Kick (Long)", "Goal")
    assert not matches_event("Blocks (Shot)", "Shot")


# --- chooser: auto --------------------------------------------------------------

def test_auto_quiet_player_uses_range():
    assert strategy(clips_on_this_source=3) == "youtube-range"
    assert strategy(clips_on_this_source=RANGE_CLIP_LIMIT) == "youtube-range"


def test_auto_busy_player_uses_full_download():
    assert strategy(clips_on_this_source=RANGE_CLIP_LIMIT + 1) == "youtube-full"


def test_auto_event_filter_uses_range_even_when_busy():
    assert strategy(clips_on_this_source=40, event_filter="Goal") == "youtube-range"


def test_local_or_cache_always_trims_on_disk():
    assert strategy(has_local_or_cache=True, clips_on_this_source=2) == "local-trim"
    assert strategy(
        has_local_or_cache=True, event_filter="Goal", fetch_mode="range"
    ) == "local-trim"


def test_missing_end_cannot_range():
    assert strategy(has_end=False, clips_on_this_source=2) == "youtube-full"


def test_force_range_and_full():
    assert strategy(fetch_mode="range", clips_on_this_source=40) == "youtube-range"
    assert strategy(fetch_mode="full", clips_on_this_source=2) == "youtube-full"


def test_non_youtube_without_file_stays_local_trim():
    assert strategy(is_youtube=False, has_local_or_cache=False) == "local-trim"


# --- AutoCompiler wiring (no network, no ffmpeg) --------------------------------

@pytest.fixture
def compiler(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "check_dependencies", lambda *a, **k: None)
    monkeypatch.setattr(ac, "get_compiler_csv", lambda path: path)
    monkeypatch.setattr(ac, "find_caption_font", lambda: None)
    monkeypatch.setattr(ac, "load_cache", lambda: {})
    monkeypatch.setattr(ac, "find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(ac, "resolve_cached_file", lambda *a, **k: None)

    dummy = tmp_path / "tags.csv"
    dummy.write_text("player,match_id,action,start_time,end_time\n")
    out = tmp_path / "compilations"
    return AutoCompiler(
        csv_path=str(dummy),
        output_folder=str(out),
        captions=False,
        dry_run=False,
        fetch_mode="auto",
        logo_path=None,
    )


def test_strategy_for_quiet_player_batch(compiler):
    entries = [make_entry(start=i, end=i + 5) for i in range(3)]
    compiler._batch_counts = count_by_match(entries)
    assert compiler._strategy_for_entry(entries[0]) == "youtube-range"


def test_strategy_for_busy_player_batch(compiler):
    entries = [make_entry(start=i, end=i + 5) for i in range(RANGE_CLIP_LIMIT + 1)]
    compiler._batch_counts = count_by_match(entries)
    assert compiler._strategy_for_entry(entries[0]) == "youtube-full"


def test_strategy_for_event_compile(compiler):
    compiler.caption_event = "Goal"
    entries = [make_entry(start=i, end=i + 5) for i in range(20)]
    compiler._batch_counts = count_by_match(entries)
    assert compiler._strategy_for_entry(entries[0]) == "youtube-range"


def test_cached_full_file_uses_local_trim(compiler, tmp_path, monkeypatch):
    cached = tmp_path / "cached.mp4"
    cached.write_bytes(b"video")
    monkeypatch.setattr(ac, "resolve_cached_file", lambda *a, **k: str(cached))
    compiler._batch_counts = {match_key(URL): 2}
    assert compiler._strategy_for_entry(make_entry()) == "local-trim"


def test_dry_run_quiet_player_announces_range(compiler, capsys):
    compiler.dry_run = True
    compiler._batch_counts = {match_key(URL): 2}
    path = compiler.generate_clip(make_entry())
    out = capsys.readouterr().out
    assert "YouTube range-download" in out
    assert path.endswith(".mp4")


def test_dry_run_busy_player_announces_full(compiler, capsys):
    compiler.dry_run = True
    compiler._batch_counts = {match_key(URL): RANGE_CLIP_LIMIT + 1}
    compiler.generate_clip(make_entry())
    assert "full download then local trim" in capsys.readouterr().out


def test_generate_clip_range_does_not_full_download(compiler, monkeypatch):
    compiler._batch_counts = {match_key(URL): 2}
    calls = {"range": 0, "trim": 0}

    def fake_range(url, clip_path, entry, caption_filter, temp_folder):
        calls["range"] += 1
        Path(clip_path).write_bytes(b"clip")

    monkeypatch.setattr(compiler, "_range_from_youtube", fake_range)
    monkeypatch.setattr(
        compiler,
        "_trim_local",
        lambda *a, **k: calls.__setitem__("trim", 1),
    )

    path = compiler.generate_clip(make_entry())

    assert Path(path).exists()
    assert calls == {"range": 1, "trim": 0}
    assert compiler.stats["trimmed"] == 1


def test_generate_clip_full_does_not_range(compiler, tmp_path, monkeypatch):
    compiler._batch_counts = {match_key(URL): RANGE_CLIP_LIMIT + 1}
    source = tmp_path / "full.mp4"
    source.write_bytes(b"full")
    ranged = {"called": False}

    monkeypatch.setattr(
        compiler,
        "ensure_video_available",
        lambda match_id: str(source),
    )
    monkeypatch.setattr(
        compiler, "_trim_local", lambda *a, **k: Path(a[1]).write_bytes(b"clip")
    )
    monkeypatch.setattr(
        compiler,
        "_range_from_youtube",
        lambda *a, **k: ranged.__setitem__("called", True),
    )

    path = compiler.generate_clip(make_entry())
    assert path and Path(path).exists()
    assert ranged["called"] is False
    assert compiler.stats["trimmed"] == 1


def test_local_file_entry_trims_on_disk(compiler, tmp_path, monkeypatch):
    video = tmp_path / "handed.mp4"
    video.write_bytes(b"local")
    ranged = {"called": False}
    monkeypatch.setattr(
        compiler, "_trim_local", lambda *a, **k: Path(a[1]).write_bytes(b"clip")
    )
    monkeypatch.setattr(
        compiler,
        "_range_from_youtube",
        lambda *a, **k: ranged.__setitem__("called", True),
    )

    path = compiler.generate_clip(make_entry(match_id=str(video)))
    assert path
    assert ranged["called"] is False
    assert compiler.stats["local"] == 1
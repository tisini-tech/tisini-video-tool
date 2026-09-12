"""Turns a raw tagger export into the compiler's CSV format.

Sits next to core.py because it is shared by every entry point that accepts
a CSV (currently AutoCompiler, via versions/auto_compiler.py). Detection and
time formatting reuse the same rules core.read_csv_entries already uses, so
the two stay in agreement.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

from .core import VIDEO_TOOL_CACHE_DIR, parse_time_to_seconds, seconds_to_timestamp

PAD_BEFORE = 2.5   # seconds kept before each tagged moment
PAD_AFTER = 4.0    # seconds kept after each tagged moment
LOCK_TIMEOUT = 10  # seconds to wait for a manifest file lock before giving up
MAX_MATCH_HOURS = 4  # no real match video runs longer than this

# Same fixed location core.py already uses for cache.json and
# download_count.json — matches.csv is the same kind of thing (internal
# state the pipeline manages), so it lives next to them, not wherever the
# command happened to be run from.
DEFAULT_REGISTRY_PATH = VIDEO_TOOL_CACHE_DIR / "matches.csv"

# Same synonym sets core.read_csv_entries recognizes, so the two agree on
# what counts as a compiler-ready column.
_START_SYNONYMS = {"start_time", "start", "starttime", "begin"}
_END_SYNONYMS = {"end_time", "end", "endtime", "finish"}
_PLAYER_SYNONYMS = {"player", "name", "player_name"}
_TIME_SYNONYMS = {"video time", "time", "timestamp", "time_seconds"}
_EVENT_SYNONYMS = {"event", "action", "clip_name", "description"}
_SUBEVENT_SYNONYMS = {"sub event", "sub_event", "subevent"}


def _read_header(csv_path: Path) -> list[str]:
    with open(csv_path, encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("#"):
                continue
            return next(csv.reader([line]))
    return []


def _match(header: list[str], synonyms: set[str]) -> str | None:
    for col in header:
        if col.lower().strip() in synonyms:
            return col
    return None


def _is_compiler_ready(header: list[str]) -> bool:
    return bool(_match(header, _START_SYNONYMS) and _match(header, _END_SYNONYMS))


def _is_raw_tagger_export(header: list[str]) -> tuple[bool, dict]:
    player_col = _match(header, _PLAYER_SYNONYMS)
    time_col = _match(header, _TIME_SYNONYMS)
    event_col = _match(header, _EVENT_SYNONYMS)
    if player_col and time_col:
        return True, {
            "player": player_col,
            "time": time_col,
            "event": event_col,
            "sub_event": _match(header, _SUBEVENT_SYNONYMS),
        }
    return False, {}


def _merge_overlapping_windows(windows: list) -> list:
    """Merge windows for the same player when one starts before the
    previous one (for that player) ends. Actions in a merged group are
    joined with ' + ', in the order the tags happened."""
    by_player: dict = {}
    for w in windows:
        by_player.setdefault(w["player"], []).append(w)

    merged = []
    for player, player_windows in by_player.items():
        player_windows.sort(key=lambda w: w["start"])
        current = None
        for w in player_windows:
            if current is None:
                current = {"player": player, "action": w["action"],
                           "start": w["start"], "end": w["end"]}
                continue
            if w["start"] <= current["end"]:
                current["end"] = max(current["end"], w["end"])
                current["action"] += f" + {w['action']}"
            else:
                merged.append(current)
                current = {"player": player, "action": w["action"],
                           "start": w["start"], "end": w["end"]}
        if current is not None:
            merged.append(current)
    return merged


def _infer_time_divisor(raw_values: list[float]) -> tuple[float, str]:
    """Given every raw Video Time value from one file, work out whether
    they're seconds, milliseconds, or microseconds, based on how large the
    biggest one is. Whichever scale brings the max value under
    MAX_MATCH_HOURS is treated as the real unit."""
    ceiling_seconds = MAX_MATCH_HOURS * 3600
    max_val = max(raw_values)
    for divisor, label in [
        (1, "seconds"), (1000, "milliseconds"), (1_000_000, "microseconds")
    ]:
        if max_val / divisor <= ceiling_seconds:
            return float(divisor), label
    raise ValueError(
        f"Largest Video Time value ({max_val}) doesn't fit seconds, "
        f"milliseconds, or microseconds within a {MAX_MATCH_HOURS}-hour ceiling. "
        f"Check the tagger's export format, or raise MAX_MATCH_HOURS if a "
        f"longer video is genuinely expected."
    )


def _load_registry(registry_path: Path) -> dict:
    if not registry_path.exists():
        return {}
    with open(registry_path, encoding="utf-8-sig") as f:
        return {r["export_filename"].strip(): r["youtube_url"].strip()
                for r in csv.DictReader(f)}


def _save_to_registry(registry_path: Path, filename: str, url: str):
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not registry_path.exists()
    with open(registry_path, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["export_filename", "youtube_url"])
        w.writerow([filename, url])


def get_compiler_csv(csv_path: str, registry_path: str | Path | None = None,
                      pad_before: float = PAD_BEFORE,
                      pad_after: float = PAD_AFTER) -> str:
    """Given any CSV, return a path the compiler can read directly.

    - Already compiler-ready (has start_time/end_time): returned unchanged.
    - Raw tagger export (has player + a single time column): converted,
      padded into a start/end window, and written alongside the original.
      The YouTube URL is pulled from the registry by filename, or asked
      for once and saved, so later runs of the same file skip the prompt.

    registry_path defaults to a fixed location (next to core.py's own
    cache.json), not the current working directory, so the same registry
    is used no matter where this is run from. Pass a path explicitly to
    override, e.g. for tests.
    """
    input_path = Path(csv_path)
    header = _read_header(input_path)

    if _is_compiler_ready(header):
        return str(input_path)

    is_raw, cols = _is_raw_tagger_export(header)
    if not is_raw:
        raise ValueError(
            f"{input_path.name} matches neither the compiler format "
            f"(needs start_time/end_time) nor a raw tagger export "
            f"(needs a player column and a time column). Header seen: {header}"
        )

    registry_path = Path(registry_path) if registry_path else DEFAULT_REGISTRY_PATH
    registry = _load_registry(registry_path)
    url = registry.get(input_path.name)
    if not url:
        url = input(
            f"No YouTube URL on file for '{input_path.name}'.\n"
            f"Paste the URL for this match: "
        ).strip()
        _save_to_registry(registry_path, input_path.name, url)

    # Read every row's raw fields first — the time unit (seconds vs.
    # milliseconds vs. microseconds) can only be known once every value in
    # the file has been seen, not row by row.
    raw_rows = []
    with open(input_path, encoding="utf-8-sig") as f:
        for raw_row in csv.DictReader(
            ln for ln in f if ln.strip() and not ln.startswith(("//", "#"))
        ):
            raw_time = raw_row[cols["time"]].strip()
            if not raw_time:
                continue
            player = raw_row[cols["player"]].strip()
            event = raw_row[cols["event"]].strip() if cols["event"] else ""
            sub = raw_row[cols["sub_event"]].strip() if cols["sub_event"] else ""
            action = f"{event} ({sub})" if sub else event
            raw_rows.append({"player": player, "action": action, "raw_time": raw_time})

    if raw_rows and ":" in raw_rows[0]["raw_time"]:
        # Already clock-style (HH:MM:SS or MM:SS) — no unit to guess.
        for r in raw_rows:
            r["seconds"] = parse_time_to_seconds(r["raw_time"])
    else:
        numeric_values = [float(r["raw_time"]) for r in raw_rows]
        divisor, unit_label = (
            _infer_time_divisor(numeric_values) if numeric_values else (1.0, "seconds")
        )
        if divisor != 1:
            implied_hours = max(numeric_values) / divisor / 3600
            print(f"[csv_prep] Video Time values look like {unit_label} "
                  f"(scaling by 1/{int(divisor)} implies a "
                  f"{implied_hours:.1f}h video). "
                  f"Dividing every value by {int(divisor)}.")
        for r, v in zip(raw_rows, numeric_values):
            r["seconds"] = v / divisor

    raw_windows = [
        {
            "player": r["player"],
            "action": r["action"],
            "start": max(0.0, r["seconds"] - pad_before),
            "end": r["seconds"] + pad_after,
        }
        for r in raw_rows
    ]

    merged = _merge_overlapping_windows(raw_windows)

    rows = [
        [w["player"], url, w["action"],
         seconds_to_timestamp(w["start"]), seconds_to_timestamp(w["end"]), w["start"]]
        for w in merged
    ]
    rows.sort(key=lambda r: r[5])

    out_path = input_path.with_name(input_path.stem + "_converted.csv")
    with open(out_path, "w", newline="") as f:
        f.write("// Video Tool Unified CSV Format v3.0\n")
        w = csv.writer(f)
        w.writerow(["player", "match_id", "action", "start_time", "end_time"])
        w.writerows(r[:5] for r in rows)

    print(f"[csv_prep] Converted {len(rows)} rows from raw tagger export -> {out_path}")
    return str(out_path)


# --- Player/match manifest, written after AutoCompiler finishes -----------
#
# Two files, written for different reasons:
#   matches/<match_id>.json   one per match, written once, never edited again
#   players/<player_id>.json  one per player, appended to on every new match
#
# The app only ever reads player files. Match files exist so each match's
# detail lives somewhere durable, without ever having to rewrite a growing
# file as more matches come in.
#
# manifest_root has no fixed default here on purpose: AutoCompiler passes
# in a path derived from wherever it actually wrote the compilation files
# this run, so a manifest and the videos it points to always sit together,
# even if the output folder was overridden with -o.
#
# Each player file is read-modify-written, which is only safe if two
# matches sharing a player can't do that at the same time. A lock file
# next to each player's JSON enforces that: two matches touching different
# players never wait on each other, only two matches touching the SAME
# player do, and only for as long as one write takes.

def _slugify(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name).strip().lower()
    return re.sub(r"[\s_]+", "-", slug) or "unknown-player"


def write_manifests(match_id: str, match_url: str, results: dict,
                     manifest_root: str = "manifests") -> None:
    """results: {player_name: output_video_path} as returned by AutoCompiler.run().

    Safe to call once per completed match, including from two matches
    running at the same time. Does not touch any directory other than
    manifest_root/matches and manifest_root/players.
    """
    root = Path(manifest_root)
    matches_dir = root / "matches"
    players_dir = root / "players"
    matches_dir.mkdir(parents=True, exist_ok=True)
    players_dir.mkdir(parents=True, exist_ok=True)

    compiled_at = datetime.now().isoformat(timespec="seconds")
    match_players = []
    updated_count = 0

    for player_name, output_path in results.items():
        player_id = _slugify(player_name)
        match_players.append({
            "player_id": player_id,
            "player_name": player_name,
            "compilation_file": output_path,
        })

        player_file = players_dir / f"{player_id}.json"
        lock_path = str(player_file) + ".lock"
        try:
            with FileLock(lock_path, timeout=LOCK_TIMEOUT):
                if player_file.exists():
                    with open(player_file, encoding="utf-8") as f:
                        data = json.load(f)
                else:
                    data = {
                        "player_id": player_id,
                        "player_name": player_name,
                        "matches": [],
                    }

                # Re-running the same match overwrites its entry, not duplicates it.
                data["matches"] = [
                    m for m in data["matches"] if m["match_id"] != match_id
                ]
                data["matches"].append({
                    "match_id": match_id,
                    "date": compiled_at,
                    "compilation_file": output_path,
                })
                with open(player_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            updated_count += 1
        except Timeout:
            print(f"[csv_prep] WARNING: another compile is holding the lock on "
                  f"{player_file.name} after {LOCK_TIMEOUT}s. Skipped updating this "
                  f"player's manifest — the video itself compiled fine, only the "
                  f"index entry is missing. Re-run write_manifests for this match "
                  f"later to fix it.")

    match_file = matches_dir / f"{match_id}.json"
    match_lock_path = str(match_file) + ".lock"
    try:
        with FileLock(match_lock_path, timeout=LOCK_TIMEOUT):
            with open(match_file, "w", encoding="utf-8") as f:
                json.dump({
                    "match_id": match_id,
                    "match_url": match_url,
                    "compiled_at": compiled_at,
                    "players": match_players,
                }, f, indent=2)
    except Timeout:
        print(f"[csv_prep] WARNING: could not write {match_file.name}, "
              f"lock held after {LOCK_TIMEOUT}s.")

    print(f"[csv_prep] Wrote {match_file} and updated "
          f"{updated_count}/{len(results)} player file(s)")

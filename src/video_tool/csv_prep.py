
"""Prepare raw tagger exports for the Video Tool compiler.

This module sits next to core.py because CSV preparation is shared by
entry points that accept compiler CSV files.

Detection and time formatting reuse the same rules as
core.read_csv_entries(), keeping CSV preparation and CSV consumption
consistent.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

from .core import (
    VIDEO_TOOL_CACHE_DIR,
    parse_time_to_seconds,
    seconds_to_timestamp,
)

PAD_BEFORE = 0.5
PAD_AFTER = 3.5

LOCK_TIMEOUT = 10
MAX_MATCH_HOURS = 4

# Application-managed registry state belongs with the other Video Tool
# cache files rather than in the current working directory.
DEFAULT_REGISTRY_PATH = VIDEO_TOOL_CACHE_DIR / "matches.csv"


# Keep these synonym sets aligned with core.read_csv_entries().
_START_SYNONYMS = {
    "start_time",
    "start",
    "starttime",
    "begin",
}

_END_SYNONYMS = {
    "end_time",
    "end",
    "endtime",
    "finish",
}

_PLAYER_SYNONYMS = {
    "player",
    "name",
    "player_name",
}

_TIME_SYNONYMS = {
    "video time",
    "time",
    "timestamp",
    "time_seconds",
}

_EVENT_SYNONYMS = {
    "event",
    "action",
    "clip_name",
    "description",
}

_SUBEVENT_SYNONYMS = {
    "sub event",
    "sub_event",
    "subevent",
}


def _read_header(csv_path: Path) -> list[str]:
    """Read the first meaningful CSV header row."""

    with open(csv_path, encoding="utf-8-sig") as file:
        for line in file:
            stripped = line.strip()

            if (
                not stripped
                or stripped.startswith("//")
                or stripped.startswith("#")
            ):
                continue

            return next(csv.reader([line]))

    return []


def _match(
    header: list[str],
    synonyms: set[str],
) -> str | None:
    """Return the first header column matching a synonym."""

    for column in header:
        if column.lower().strip() in synonyms:
            return column

    return None


def _is_compiler_ready(header: list[str]) -> bool:
    """Return whether a CSV already contains compiler time columns."""

    return bool(
        _match(header, _START_SYNONYMS)
        and _match(header, _END_SYNONYMS)
    )


def _is_raw_tagger_export(
    header: list[str],
) -> tuple[bool, dict]:
    """Detect and map columns from a raw tagger export."""

    player_col = _match(header, _PLAYER_SYNONYMS)
    time_col = _match(header, _TIME_SYNONYMS)
    event_col = _match(header, _EVENT_SYNONYMS)

    if player_col and time_col:
        return True, {
            "player": player_col,
            "time": time_col,
            "event": event_col,
            "sub_event": _match(
                header,
                _SUBEVENT_SYNONYMS,
            ),
        }

    return False, {}


def _merge_overlapping_windows(
    windows: list[dict],
) -> list[dict]:
    """Merge overlapping windows belonging to the same player.

    When overlapping tags are merged, their actions are joined in the
    order in which the tags occurred.
    """

    by_player: dict[str, list[dict]] = {}

    for window in windows:
        by_player.setdefault(
            window["player"],
            [],
        ).append(window)

    merged: list[dict] = []

    for player, player_windows in by_player.items():
        player_windows.sort(
            key=lambda window: window["start"],
        )

        current = None

        for window in player_windows:
            if current is None:
                current = {
                    "player": player,
                    "action": window["action"],
                    "start": window["start"],
                    "end": window["end"],
                }
                continue

            if window["start"] <= current["end"]:
                current["end"] = max(
                    current["end"],
                    window["end"],
                )
                current["action"] += (
                    f" + {window['action']}"
                )
            else:
                merged.append(current)
                current = {
                    "player": player,
                    "action": window["action"],
                    "start": window["start"],
                    "end": window["end"],
                }

        if current is not None:
            merged.append(current)

    return merged


def _infer_time_divisor(
    raw_values: list[float],
) -> tuple[float, str]:
    """Infer whether numeric video times are seconds or smaller units.

    The largest value is compared against MAX_MATCH_HOURS. The first scale
    that produces a value within that ceiling is treated as the source unit.
    """

    ceiling_seconds = MAX_MATCH_HOURS * 3600
    max_value = max(raw_values)

    for divisor, label in (
        (1, "seconds"),
        (1000, "milliseconds"),
        (1_000_000, "microseconds"),
    ):
        if max_value / divisor <= ceiling_seconds:
            return float(divisor), label

    raise ValueError(
        f"Largest Video Time value ({max_value}) doesn't fit "
        f"seconds, milliseconds, or microseconds within a "
        f"{MAX_MATCH_HOURS}-hour ceiling. Check the tagger's "
        f"export format, or raise MAX_MATCH_HOURS if a longer "
        "video is genuinely expected."
    )


def _load_registry(
    registry_path: Path,
) -> dict[str, str]:
    """Load filename-to-YouTube-URL mappings."""

    if not registry_path.exists():
        return {}

    with open(
        registry_path,
        encoding="utf-8-sig",
    ) as file:
        return {
            row["export_filename"].strip(): row["youtube_url"].strip()
            for row in csv.DictReader(file)
        }


def _save_to_registry(
    registry_path: Path,
    filename: str,
    url: str,
) -> None:
    """Save a filename-to-YouTube-URL mapping."""

    registry_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    is_new = not registry_path.exists()

    with open(
        registry_path,
        "a",
        newline="",
    ) as file:
        writer = csv.writer(file)

        if is_new:
            writer.writerow(
                [
                    "export_filename",
                    "youtube_url",
                ]
            )

        writer.writerow(
            [
                filename,
                url,
            ]
        )


def get_compiler_csv(
    csv_path: str,
    registry_path: str | Path | None = None,
    pad_before: float = PAD_BEFORE,
    pad_after: float = PAD_AFTER,
) -> str:
    """Return a CSV path that AutoCompiler can consume directly.

    An already compiler-ready CSV is returned unchanged.

    A raw tagger export is converted into start/end windows, padded around
    each tagged moment, and written alongside the original CSV.

    The YouTube URL is loaded from the application registry when available.
    Otherwise the user is prompted once and the URL is stored for future
    runs of the same export filename.

    ``registry_path`` defaults to the application cache directory but can
    be overridden for tests or other controlled workflows.
    """

    input_path = Path(csv_path)
    header = _read_header(input_path)

    if _is_compiler_ready(header):
        return str(input_path)

    is_raw, columns = _is_raw_tagger_export(header)

    if not is_raw:
        raise ValueError(
            f"{input_path.name} matches neither the compiler format "
            "(needs start_time/end_time) nor a raw tagger export "
            "(needs a player column and a time column). "
            f"Header seen: {header}"
        )

    registry_path = (
        Path(registry_path)
        if registry_path
        else DEFAULT_REGISTRY_PATH
    )

    registry = _load_registry(registry_path)
    url = registry.get(input_path.name)

    if not url:
        url = input(
            f"No YouTube URL on file for "
            f"'{input_path.name}'.\n"
            "Paste the URL for this match: "
        ).strip()

        _save_to_registry(
            registry_path,
            input_path.name,
            url,
        )

    # Read all raw times before converting them. The unit cannot reliably
    # be inferred until the complete file has been examined.
    raw_rows: list[dict] = []

    with open(
        input_path,
        encoding="utf-8-sig",
    ) as file:
        rows = csv.DictReader(
            line
            for line in file
            if line.strip()
            and not line.startswith(("//", "#"))
        )

        for raw_row in rows:
            raw_time = raw_row[
                columns["time"]
            ].strip()

            if not raw_time:
                continue

            player = raw_row[
                columns["player"]
            ].strip()

            event = (
                raw_row[columns["event"]].strip()
                if columns["event"]
                else ""
            )

            sub_event = (
                raw_row[columns["sub_event"]].strip()
                if columns["sub_event"]
                else ""
            )

            action = (
                f"{event} ({sub_event})"
                if sub_event
                else event
            )

            raw_rows.append(
                {
                    "player": player,
                    "action": action,
                    "raw_time": raw_time,
                }
            )

    if raw_rows and ":" in raw_rows[0]["raw_time"]:
        # Clock-style values such as HH:MM:SS or MM:SS already have an
        # unambiguous unit.
        for row in raw_rows:
            row["seconds"] = parse_time_to_seconds(
                row["raw_time"]
            )

    else:
        numeric_values = [
            float(row["raw_time"])
            for row in raw_rows
        ]

        if numeric_values:
            divisor, unit_label = _infer_time_divisor(
                numeric_values
            )
        else:
            divisor, unit_label = 1.0, "seconds"

        if divisor != 1:
            implied_hours = (
                max(numeric_values)
                / divisor
                / 3600
            )

            print(
                "[csv_prep] Video Time values look like "
                f"{unit_label} (scaling by 1/"
                f"{int(divisor)} implies a "
                f"{implied_hours:.1f}h video). "
                f"Dividing every value by {int(divisor)}."
            )

        for row, value in zip(
            raw_rows,
            numeric_values,
        ):
            row["seconds"] = value / divisor

    raw_windows = [
        {
            "player": row["player"],
            "action": row["action"],
            "start": max(
                0.0,
                row["seconds"] - pad_before,
            ),
            "end": row["seconds"] + pad_after,
        }
        for row in raw_rows
    ]

    merged = _merge_overlapping_windows(
        raw_windows
    )

    rows = [
        [
            window["player"],
            url,
            window["action"],
            seconds_to_timestamp(
                window["start"]
            ),
            seconds_to_timestamp(
                window["end"]
            ),
            window["start"],
        ]
        for window in merged
    ]

    rows.sort(key=lambda row: row[5])

    output_path = input_path.with_name(
        input_path.stem + "_converted.csv"
    )

    with open(
        output_path,
        "w",
        newline="",
    ) as file:
        file.write(
            "// Video Tool CSV Format\n"
        )

        writer = csv.writer(file)

        writer.writerow(
            [
                "player",
                "match_id",
                "action",
                "start_time",
                "end_time",
            ]
        )

        writer.writerows(
            row[:5]
            for row in rows
        )

    print(
        f"[csv_prep] Converted {len(rows)} rows "
        f"from raw tagger export -> {output_path}"
    )

    return str(output_path)


# ---------------------------------------------------------------------------
# Player/match manifest
# ---------------------------------------------------------------------------
#
# Two files are written for different purposes:
#
#   matches/<match_id>.json
#       One file per match.
#
#   players/<player_id>.json
#       One file per player, updated whenever that player appears in a new
#       completed match.
#
# The application primarily reads player files. Match files preserve the
# durable details of each individual compilation.
#
# manifest_root intentionally has no fixed default here. AutoCompiler passes
# a path based on the output directory used for the current compilation, so
# the manifest stays alongside the videos even when -o changes the output
# location.
#
# Player files are read-modify-written. A per-player lock prevents two
# simultaneous matches involving the same player from overwriting one
# another. Matches involving different players do not block each other.


def _slugify(name: str) -> str:
    """Convert a player name into a filesystem-friendly identifier."""

    slug = re.sub(
        r"[^\w\s-]",
        "",
        name,
    ).strip().lower()

    return (
        re.sub(r"[\s_]+", "-", slug)
        or "unknown-player"
    )


def write_manifests(
    match_id: str,
    match_url: str,
    results: dict,
    manifest_root: str = "manifests",
) -> None:
    """Write match and player manifests for a completed compilation.

    ``results`` is expected to contain:

        {player_name: output_video_path}

    The operation is safe to call for concurrent matches. Player manifests
    use individual lock files so only compilations touching the same player
    need to wait for each other.

    A repeated run for the same match replaces that match's existing player
    entry instead of creating a duplicate.
    """

    root = Path(manifest_root)

    matches_dir = root / "matches"
    players_dir = root / "players"

    matches_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    players_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    compiled_at = datetime.now().isoformat(
        timespec="seconds"
    )

    match_players = []
    updated_count = 0

    for player_name, output_path in results.items():
        player_id = _slugify(player_name)

        match_players.append(
            {
                "player_id": player_id,
                "player_name": player_name,
                "compilation_file": output_path,
            }
        )

        player_file = (
            players_dir / f"{player_id}.json"
        )

        lock_path = str(player_file) + ".lock"

        try:
            with FileLock(
                lock_path,
                timeout=LOCK_TIMEOUT,
            ):
                if player_file.exists():
                    with open(
                        player_file,
                        encoding="utf-8",
                    ) as file:
                        data = json.load(file)
                else:
                    data = {
                        "player_id": player_id,
                        "player_name": player_name,
                        "matches": [],
                    }

                # Re-running the same match replaces its existing entry.
                data["matches"] = [
                    match
                    for match in data["matches"]
                    if match["match_id"] != match_id
                ]

                data["matches"].append(
                    {
                        "match_id": match_id,
                        "date": compiled_at,
                        "compilation_file": output_path,
                    }
                )

                with open(
                    player_file,
                    "w",
                    encoding="utf-8",
                ) as file:
                    json.dump(
                        data,
                        file,
                        indent=2,
                    )

            updated_count += 1

        except Timeout:
            print(
                "[csv_prep] WARNING: another compile is "
                f"holding the lock on {player_file.name} "
                f"after {LOCK_TIMEOUT}s. Skipped updating "
                "this player's manifest — the video itself "
                "compiled fine, only the index entry is "
                "missing. Re-run write_manifests for this "
                "match later to fix it."
            )

    match_file = (
        matches_dir / f"{match_id}.json"
    )

    match_lock_path = (
        str(match_file) + ".lock"
    )

    try:
        with FileLock(
            match_lock_path,
            timeout=LOCK_TIMEOUT,
        ):
            with open(
                match_file,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    {
                        "match_id": match_id,
                        "match_url": match_url,
                        "compiled_at": compiled_at,
                        "players": match_players,
                    },
                    file,
                    indent=2,
                )

    except Timeout:
        print(
            "[csv_prep] WARNING: could not write "
            f"{match_file.name}, lock held after "
            f"{LOCK_TIMEOUT}s."
        )

    print(
        f"[csv_prep] Wrote {match_file} and updated "
        f"{updated_count}/{len(results)} player file(s)"
    )
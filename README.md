# Video Tool

A command-line video toolkit for downloading, trimming, and compiling
YouTube and local video content. Several product versions share one
implementation layer, so a fix or improvement in shared code benefits every
version at once.

## Features

- **Basic / Pro / Unified downloaders** — YouTube and Google Drive sources,
  with quality control, trimming, and (Pro) authenticated downloads.
- **Auto-Compiler** — turns a raw sports-tagging CSV export into one merged
  highlight compilation per player, automatically:
  - detects raw tagger exports vs. already-compiler-ready CSVs
  - infers whether tagged timestamps are seconds, milliseconds, or
    microseconds, and corrects them
  - merges a player's overlapping or back-to-back tagged moments into a
    single clip instead of several near-duplicate ones
  - remembers each match's YouTube URL after asking once
  - writes a JSON manifest per match and per player for downstream apps
- **AI Finder** — Gemini-assisted clip discovery from long-form video.
- Shared local cache, so repeated compiles against the same match never
  re-download the source video.

## Project Layout

src/video_tool/
├── core.py                 # FFmpeg, time, cache, config, CSV, trim, merge
├── csv_prep.py             # Raw tagger CSV -> compiler-ready CSV, + manifests
├── version_check.py        # Daily yt-dlp/ffmpeg version check
├── youtube.py               # Shared yt-dlp download behaviour
├── gdrive/                  # Google Drive parsing and trimming
└── versions/
    ├── video_tool_basic.py    # Basic downloader
    ├── video_tool_pro.py      # Pro downloader
    ├── video_tool_unified.py  # YouTube + Google Drive API
    ├── auto_compiler.py       # CSV-driven per-player compilation
    ├── ai_finder.py           # AI-assisted clip discovery
    └── merge_tool.py          # CLI merge utility

Each product version is a runnable script and console entry point. Shared
behaviour lives in `video_tool.core`, `video_tool.csv_prep`,
`video_tool.youtube`, and `video_tool.gdrive` — a bug or behaviour change
there should be fixed once, not independently in Basic, Pro, and
Auto-Compiler.

## Requirements

- Python 3.12+
- FFmpeg and FFprobe on `PATH` (required for trimming, merging, and video
  metadata)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

For AI clip discovery, add the `ai` extra:

```bash
pip install -e ".[dev,ai]"
```

Or, without an editable install:

```bash
pip install -r requirements.txt
```

## Usage

```bash
vt-basic "https://www.youtube.com/watch?v=..."
vt-pro "https://www.youtube.com/watch?v=..." --start 00:30 --end 01:00
vt-auto-compiler path/to/raw_tagger_export.csv
```

The module form is equivalent:

```bash
python -m video_tool.versions.video_tool_basic "https://www.youtube.com/watch?v=..."
python -m video_tool.versions.video_tool_pro "https://www.youtube.com/watch?v=..."
python -m video_tool.versions.auto_compiler path/to/raw_tagger_export.csv
```

## Auto-Compiler Pipeline

`vt-auto-compiler` accepts either a raw export straight from the tagging
software or an already compiler-ready CSV — it detects which one it got and
handles both without any manual conversion step.

**On a raw export**, before anything else runs:

1. **Header detection** — a `player` column plus a single time column marks
   it as raw; `start_time`/`end_time` marks it as already compiler-ready and
   it passes through untouched.
2. **Time unit inference** — the largest raw timestamp in the file is
   checked against a 4-hour ceiling (`MAX_MATCH_HOURS` in `csv_prep.py`) to
   work out whether values are seconds, milliseconds, or microseconds, and
   scales the whole column accordingly. This runs once per file, not per
   row, since the unit is a property of the export, not of any one tag.
3. **URL resolution** — the match's YouTube URL is looked up by filename in
   a registry at `~/.cache/video-tool/matches.csv`. On a miss, it's asked
   for once, interactively, and saved — every later run of that same file
   skips the prompt.
4. **Padding and merging** — each tag becomes a `[t - 0s, t + 2.5s]` window
   (`PAD_BEFORE` / `PAD_AFTER` in `csv_prep.py`). Windows for the same
   player that overlap are merged into one clip rather than producing
   several near-duplicate ones, with their action labels joined (`Pass +
   Progress Pass`).
5. **Compiler-ready CSV** — written alongside the original as
   `<name>_converted.csv`, in the same format Auto-Compiler has always read.

**After compiling**, one manifest per match and one per player are written
next to wherever the finished compilations landed
(`<output-folder>/../manifests/`, so a custom `-o` is respected):

```text
manifests/
├── matches/
│   └── <video_id>.json     # written once per match, immutable after that
└── players/
    └── <player-slug>.json  # appended to on every match that player appears in
```

An app or website only needs to read `players/<slug>.json` to show one
player everything they've ever been tagged in. Writes to a player's file are
protected by a per-file lock, so two matches compiling at the same time and
sharing a player can't silently overwrite each other.

## Dependency Version Checks

Every `AutoCompiler` run checks, at most once per day, whether `yt-dlp` and
`ffmpeg` are current:

- **yt-dlp** auto-upgrades via `pip install --upgrade yt-dlp` — it's a
  normal package in the project's own venv, so upgrading it is standard and
  reversible.
- **ffmpeg** is only reported, never auto-replaced — it's a system binary
  outside pip's control, so updating it is left to you.

Any network failure during this check (offline, GitHub rate limit) is
logged and skipped; it never blocks a compile. State is tracked in
`~/.cache/video-tool/version_check.json`.

## YouTube Authentication

For non-interactive downloads, place an exported Netscape-format cookies
file at:

```text
<project-root>/youtube_cookies.txt
```

Basic, Pro, Unified, and Auto-Compiler all use this file automatically — no
browser login or `--cookie-browser` flag is required for normal use. The
file is ignored by Git; keep it private. `--cookie-file` overrides the
default path, and `--cookie-browser` is available if you'd rather extract
cookies from a live browser session instead.

## Local Cache and Downloads

Local and cached sources are checked before any network request:

1. The shared cache (`~/.cache/video-tool/`)
2. Conventional local directories (`~/Downloads/video-tool`,
   `~/Downloads/Video-Tool`)

A local file is only accepted as a match when its filename contains the
requested YouTube video ID — the downloader never guesses that an unrelated
file is the one requested. Auto-Compiler downloads a missing YouTube source
automatically once those checks come up empty.

Auto-Compiler produces one merged compilation per player. Intermediate
trimmed clips live under `.temp/` inside the output folder and are removed
after a successful run unless `--keep-temp` is supplied.

## Development

Confirmed working as of this project's current state:

```bash
# Every module imports cleanly
python -c "from video_tool.versions.auto_compiler import AutoCompiler"

# Full test suite — 7 passed
pytest

# Lint — auto-fixes what it can, reports the rest
ruff check --fix .
```

`csv_prep.py` and `version_check.py` are lint-clean under the project's
`ruff` config (`line-length = 88`, `select = ["E", "F", "I", "UP"]`).

Nothing local — cookies, `.env` files, generated downloads, logs, caches, or
virtual environments — belongs in Git.

# YFetch

YFetch is a command-line video toolkit with multiple product versions that
share one implementation layer.

## Layout

```text
src/yfetch/
├── core.py                 # FFmpeg, time, cache, config, CSV, trim, merge
├── youtube.py              # Shared yt-dlp download behaviour
├── gdrive/                 # Google Drive parsing and trimming
└── versions/
    ├── yfetch_basic.py     # Basic downloader
    ├── yfetch_pro.py       # Pro downloader
    ├── yfetch_unified.py   # YouTube + Google Drive API
    ├── auto_compiler.py    # CSV-driven player compilation
    ├── ai_finder.py        # AI-assisted clip discovery
    └── merge_tool.py       # CLI merge utility
```

The old Tkinter presentation layer has been removed. Each product version is
now a runnable script/console entry point and imports shared behaviour from
`yfetch.core`, `yfetch.youtube`, and `yfetch.gdrive`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

For AI clip discovery:

```bash
python -m pip install -e ".[dev,ai]"
```

FFmpeg and FFprobe must also be installed and available on `PATH` for
trimming, merging, and video metadata operations.

## Run

```bash
yfetch-basic "https://www.youtube.com/watch?v=..."
yfetch-pro "https://www.youtube.com/watch?v=..." --start 00:30 --end 01:00
yfetch-auto-compiler tests/cape_verde.csv
```

The module form is equivalent:

```bash
python -m yfetch.versions.yfetch_basic "https://www.youtube.com/watch?v=..."
python -m yfetch.versions.yfetch_pro "https://www.youtube.com/watch?v=..."
python -m yfetch.versions.auto_compiler tests/cape_verde.csv
```

## Architecture

Product versions are thin entry points. Shared operations belong in shared
modules.

```text
versions/*
     │
     ├── youtube.py
     ├── core.py
     └── gdrive/*
```

A bug or behaviour change in a shared operation should therefore be fixed once,
not independently in Basic, Pro, and Auto-Compiler.

## Development

```bash
pytest
ruff check .
```

No local cookies, `.env` files, generated downloads, logs, caches, or virtual
environments belong in Git.


## Local cache and YouTube authentication

YFetch checks the shared cache and configured local download directories before
making a network request. Auto-Compiler automatically downloads a missing
YouTube source after those local/cache checks.

For non-interactive YouTube authentication, place an exported Netscape-format
cookie file at:

    ./youtube_cookies.txt

The file is ignored by Git. `--cookie-file` can be used to override it.
Browser cookies are optional and are only used when explicitly requested.

Basic and Pro ask for MP4 or MP3 when `--format` is omitted. Use
`--format MP4` or `--format MP3` for scripts and automation.

Auto-Compiler creates one merged compilation per player. Intermediate trimmed
clips are stored under `.temp/` and are removed after a successful run unless
`--keep-temp` is supplied.


## YouTube authentication

For normal operation, place your exported Netscape-format cookies file at:

```text
<project-root>/youtube_cookies.txt
```

YFetch automatically uses this file for Basic, Pro, Unified, and Auto-Compiler downloads. No browser login or `--cookie-browser` option is required.

The real cookie file is ignored by Git. Keep it private.

Local/cache resolution happens before any YouTube request. YFetch checks its cache and the conventional `~/Downloads/yfetch` and `~/Downloads/YFetch` directories before downloading a missing source. A local file is only accepted as a match when the filename contains the requested YouTube video ID; the downloader never guesses that an unrelated file is the requested video.

Auto-Compiler downloads missing YouTube sources automatically. There is no `--download-missing` switch in the normal workflow.
# tisini-video-tool

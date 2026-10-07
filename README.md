# Tisini Video Tool

A modular Python toolkit for downloading, trimming, merging, and compiling video and audio from YouTube, Google Drive, and local files.

**Version:** 1.0.0

---

## Features

* YouTube video and audio downloads
* Basic and advanced download workflows
* YouTube range downloads and trimming
* Google Drive video support
* Local video processing
* Lossless-first video trimming
* Automatic re-encoding fallback when required
* Video merging with compatibility fallback
* Automatic compilation from CSV event data
* Captions and watermarks
* Download caching
* Download counter
* Optional graphical interface
* Experimental AI-assisted video finder
* Shared FFmpeg processing utilities
* Automated test suite

---

# Graphical Interface

The project includes a lightweight CustomTkinter GUI for downloading YouTube videos.

The GUI provides:

* YouTube URL input
* MP3/MP4 selection
* Quality selection
* Save-location selection
* Download status
* Download history
* Help information
* Persistent download-count tracking

Install the GUI dependencies:

```bash
pip install -e ".[gui]"
```

Launch the GUI:

```bash
python -m video_tool.gui.app
```

When running directly from the source tree without installing the package:

```bash
PYTHONPATH=src python -m video_tool.gui.app
```

The GUI is intentionally thin. Download and processing logic remains in the shared project modules rather than being duplicated inside the interface.

---

# Basic Downloader

The Basic Downloader provides a simple command-line interface for complete YouTube downloads.

The installed commands are:

```text
vt
vt-basic
```

Both commands use the same Basic Downloader implementation.

Example:

```bash
vt-basic "https://youtu.be/VIDEO_ID"
```

Specify an output directory:

```bash
vt-basic "https://youtu.be/VIDEO_ID" -o ~/Downloads/video-tool
```

Select a format:

```bash
vt-basic "https://youtu.be/VIDEO_ID" -f MP4
```

The Basic Downloader is intended for straightforward full-video or audio downloads without the additional range-processing controls available in Pro.

---

# Pro Downloader

The Pro Downloader provides more advanced download and range-processing controls.

Command:

```bash
vt-pro
```

Example:

```bash
vt-pro "https://youtu.be/VIDEO_ID" \
    --start 1:40 \
    --end 2:03
```

The Pro workflow supports:

* MP3 and MP4 downloads
* Quality selection
* Start/end ranges
* Browser cookie authentication
* Cookie-file authentication
* Download caching
* Range processing
* Persistent download counting

For example, a range can be specified using timestamps:

```text
1:40
2:03
```

The downloader converts the requested range into the appropriate media-processing operation.

Range outputs are treated separately from the full-source cache identity. Reusing a cached full source therefore does not require downloading the original YouTube media again.

---

# Video Processing

Shared video-processing functionality is implemented in:

```text
src/video_tool/core.py
```

The core module contains the project's common FFmpeg operations.

These include:

* Video trimming
* Remote trimming
* Remote downloading
* Video merging
* Duration validation
* Metadata inspection
* FFmpeg/FFprobe discovery
* Encoder selection
* Captions
* Watermarks
* Cache management
* Configuration management
* Download counting
* Time conversion utilities

Application-level modules should use these shared functions instead of implementing their own FFmpeg commands.

---

# Video Trimming

The trimming pipeline uses a **copy-first strategy** whenever possible.

Conceptually:

```text
Requested clip
      │
      ▼
Can stream-copy?
   ┌──┴──┐
  Yes    No
   │      │
   ▼      ▼
Copy    Re-encode
   │      │
   └──┬───┘
      ▼
Final clip
```

When the media allows it, FFmpeg performs a stream copy without re-encoding.

When accurate frame boundaries, captions, incompatible streams, or other conditions require re-encoding, the pipeline automatically falls back to a high-quality encode.

This keeps the normal path fast while still supporting accurate output when stream copying is not sufficient.

The fallback is handled internally by the shared processing layer.

---

# Remote Video Processing

Remote sources can be processed without first downloading the entire source file.

The core remote pipeline supports HTTP/HTTPS media sources:

```text
Remote URL
    │
    ▼
FFmpeg
    │
    ├── requested range
    │
    ▼
Output clip
```

This is used by source adapters such as the YouTube and Google Drive workflows.

The goal is to avoid unnecessary full-source downloads when only a section of a video is required.

---

# Video Merging

Video merging is also handled by the shared core.

Command:

```bash
vt-merge
```

Example:

```bash
vt-merge \
    -o output.mp4 \
    tests/fixtures/media/goals.mp4 \
    tests/fixtures/media/saves.mp4
```

The merge pipeline follows the same compatibility-first approach:

```text
Input clips
    │
    ▼
Compatible for stream copy?
    │
 ┌──┴──┐
Yes    No
 │      │
 ▼      ▼
Copy   Re-encode
 │      │
 └──┬───┘
     ▼
Merged video
```

The normal path uses FFmpeg's concat functionality with stream copying.

If the input streams cannot be merged safely using stream copy, the implementation falls back to re-encoding.

The fallback is handled internally rather than requiring the user to manually select a different processing mode.

---

# Auto-Compiler

The Auto-Compiler builds a final video from CSV event data.

Command:

```bash
vt-auto-compiler
```

Example:

```bash
vt-auto-compiler matches.csv
```

Dry-run processing can be used when you want to inspect the planned operations before processing media:

```bash
vt-auto-compiler matches.csv --dry-run
```

The Auto-Compiler can work with:

* YouTube sources
* Google Drive sources
* Local files
* Cached sources
* Full-source downloads
* Requested video ranges
* Chronological clip ordering
* Captions
* Video merging
* Watermarks

The Auto-Compiler is an orchestration layer. It coordinates the source and processing modules rather than maintaining separate implementations of downloading, trimming, or merging.

Conceptually:

```text
CSV
 │
 ▼
Auto-Compiler
 │
 ├── Source detection
 │
 ├── Source fetching
 │
 ├── Range processing
 │
 ├── Clip ordering
 │
 ├── Caption handling
 │
 └── Merge
       │
       ▼
   Final video
```

---

# CSV Preparation

Raw tagging exports can be converted into the CSV format expected by the Auto-Compiler.

The shared implementation is:

```text
src/video_tool/csv_prep.py
```

Its responsibilities include:

* Detecting raw tagger exports
* Detecting compiler-ready CSV files
* Normalizing time values
* Supporting clock-style timestamps
* Supporting numeric timestamps
* Detecting milliseconds/microseconds where appropriate
* Applying clip padding
* Merging overlapping events
* Maintaining the match URL registry
* Creating player and match manifests

The main public function is:

```python
get_compiler_csv(...)
```

The generated compiler CSV uses:

```text
player,match_id,action,start_time,end_time
```

Example:

```csv
player,match_id,action,start_time,end_time
Ada,https://www.youtube.com/watch?v=VIDEO_ID,Goal (Header),00:00:12,00:00:16
Brian,https://www.youtube.com/watch?v=VIDEO_ID,Save (Diving),00:00:35,00:00:39
```

---

# Google Drive

Google Drive support is separated into parsing and media-processing responsibilities.

```text
src/video_tool/gdrive/parser.py
src/video_tool/gdrive/trimmer.py
```

## `gdrive/parser.py`

Responsible for:

* Recognizing Google Drive URLs
* Extracting file IDs
* Retrieving Google Drive metadata
* Resolving downloadable/stream URLs
* Handling Google Drive confirmation requirements

## `gdrive/trimmer.py`

Acts as a thin adapter between Google Drive sources and the shared media-processing layer.

It delegates trimming and downloading to `core.py`.

Conceptually:

```text
Google Drive URL
       │
       ▼
gdrive/parser.py
       │
       ▼
Direct media URL
       │
       ▼
gdrive/trimmer.py
       │
       ▼
core.py
       │
       ▼
FFmpeg
```

This keeps Google Drive-specific handling separate from the general media-processing implementation.

---

# Source Architecture

Source detection and source fetching are intentionally separate.

The current architecture is:

```text
                         Input
                           │
                           ▼
                      sources.py
                           │
                    Source detection
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
       YouTube          Google Drive       Local
          │                │                │
          ▼                ▼                ▼
   clip_source.py     gdrive/parser.py   Existing
          │                │              file
          ▼                ▼
     youtube.py      gdrive/trimmer.py
          │                │
          └────────┬───────┘
                   ▼
                core.py
                   │
          ┌────────┼─────────┐
          ▼        ▼         ▼
        Trim     Merge    Validate
          │        │         │
          └────────┼─────────┘
                   ▼
              Final output
```

## `sources.py`

Source detection only.

It identifies whether an input is:

* YouTube
* Google Drive
* Local
* Cache-key based
* Unknown

It does not download, cache, probe, trim, or merge media.

## `clip_source.py`

Provides the common source-fetching layer for YouTube-oriented workflows.

It handles:

* Source fetching
* Cached-source reuse
* Full downloads
* Range downloads
* Local trimming of fetched media
* Fetch error reporting

## `youtube.py`

Contains YouTube/yt-dlp-specific operations.

Responsibilities include:

* yt-dlp configuration
* YouTube downloads
* Format selection
* Quality selection
* Cookie authentication
* Metadata retrieval
* Filename handling
* YouTube range processing
* YouTube-specific errors

## `core.py`

Contains shared media-processing functionality.

## `auto_compiler.py`

Coordinates the different layers and does not own source-specific download or FFmpeg implementations.

---

# Cache

The project maintains a shared cache so that the same full source does not need to be downloaded repeatedly.

Cache files are stored under:

```text
~/.cache/video-tool/
```

Configuration is stored under:

```text
~/.config/video-tool/
```

The cache uses a source identity based on the media ID and format.

For example:

```text
VIDEO_ID_MP4
VIDEO_ID_MP3
```

These values represent **cache identities/registry keys**. They are not necessarily the literal filenames stored on disk.

The cache allows different workflows to reuse the same downloaded source.

For example:

```text
YouTube URL
     │
     ▼
Check cache
     │
 ┌───┴────┐
Found    Missing
 │          │
 ▼          ▼
Reuse    Download
 │          │
 └────┬─────┘
      ▼
Process requested range
```

Older cached files using historical filename conventions can still be recognized where the cache-resolution logic supports them.

---

# File Naming

User-facing downloaded filenames are based on the media title rather than exposing the internal cache identity.

For example:

```text
Example Match.mp4
```

Range outputs can include their requested range where appropriate:

```text
Example Match_00-01-40_to_00-02-03.mp4
```

The cache identity remains separate from the display filename.

This allows the project to maintain reliable cache lookup without forcing internal IDs into user-facing filenames.

---

# Download Counter

The project maintains a persistent download counter.

The counter is stored at:

```text
~/.cache/video-tool/download_count.json
```

The counter represents actual source downloads.

Reusing an existing cached source does **not** increment the download counter.

This means repeated processing of clips from the same cached source does not artificially increase the download count.

---

# FFmpeg Processing

The project relies on FFmpeg and FFprobe for media processing.

The shared core automatically discovers the available FFmpeg tools and can inspect available video encoders.

Supported encoder choices include:

```text
libx264
libx265
h264_nvenc
h264_amf
h264_videotoolbox
```

The processing layer selects an appropriate encoder when re-encoding is required.

The normal processing preference is to avoid re-encoding when safe.

---

# Captions and Watermarks

The shared processing layer supports:

* Caption overlays
* Watermarks
* Re-encoding when filters are required

FFmpeg filters require the media to be processed rather than simply copied.

Therefore, operations such as captions and watermarks automatically use the appropriate re-encoding path.

---

# Experimental AI Finder

The AI Finder is an experimental component and is intentionally separate from the stable CLI commands.

Location:

```text
src/video_tool/experiments/ai_finder/
```

It is not registered as a `vt-ai-finder` command.

Install the optional AI dependencies:

```bash
pip install -e ".[ai]"
```

Run its help:

```bash
PYTHONPATH=src python -m video_tool.experiments.ai_finder.ai_finder --help
```

The experimental implementation uses the shared source and processing architecture rather than maintaining its own legacy download implementation.

Because it is experimental, its interface may change independently of the stable applications.

---

# Installation

The project requires Python 3.12 or newer.

Create and activate a virtual environment if needed:

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Windows:

```powershell
.venv\Scripts\Activate.ps1
```

Install the core project:

```bash
pip install -e .
```

Install the GUI:

```bash
pip install -e ".[gui]"
```

Install the AI Finder dependencies:

```bash
pip install -e ".[ai]"
```

Install development dependencies:

```bash
pip install -e ".[dev]"
```

Install everything:

```bash
pip install -e ".[gui,ai,dev]"
```

---

# External Dependencies

The project uses:

* Python 3.12+
* FFmpeg
* FFprobe
* yt-dlp

Some current YouTube extraction scenarios may also require a supported JavaScript runtime, depending on the extraction requirements of the installed yt-dlp version. Deno is one supported option.

The exact external dependency requirements can change as YouTube and yt-dlp evolve.

---

# Command Reference

## Basic Downloader

```bash
vt
```

```bash
vt-basic
```

## Pro Downloader

```bash
vt-pro
```

## Auto-Compiler

```bash
vt-auto-compiler
```

## Merge Tool

```bash
vt-merge
```

The installed command set is intentionally small. Experimental functionality is kept outside the stable command-line entry points.

---

# Project Structure

# Project Structure

tisini-video-tool/
│
├── assets/
│   └── logo.png
│
├── .github/
│   └── workflows/
│       └── build-gui.yml
│
├── src/
│   └── video_tool/
│       ├── clip_source.py
│       ├── core.py
│       ├── csv_prep.py
│       ├── errors.py
│       ├── notify.py
│       ├── sources.py
│       ├── version_check.py
│       ├── youtube.py
│       │
│       ├── experiments/
│       │   └── ai_finder/
│       │       ├── __init__.py
│       │       └── ai_finder.py
│       │
│       ├── gdrive/
│       │   ├── __init__.py
│       │   ├── parser.py
│       │   └── trimmer.py
│       │
│       ├── gui/
│       │   ├── __init__.py
│       │   ├── app.py
│       │   └── bundled_deps.py
│       │
│       └── versions/
│           ├── __init__.py
│           ├── auto_compiler.py
│           ├── common_cli.py
│           ├── merge_tool.py
│           ├── video_tool_basic.py
│           └── video_tool_pro.py
│
├── tests/
│   ├── fixtures/
│   │   ├── csv/
│   │   └── media/
│   │
│   ├── test_auto_compiler.py
│   ├── test_core.py
│   ├── test_merge.py
│   ├── test_pro.py
│   └── test_youtube.py
│
├── tools/
│   └── gui_entry.py
│
├── pyproject.toml
└── README.md

The project structure above shows the active application, test, packaging, and CI components. Historical and manual development files that are no longer part of the active architecture have been removed.


---

# Test Fixtures

Reusable test fixtures are stored under:

```text
tests/fixtures/
```

CSV fixtures cover cases such as:

* Basic raw tagger exports
* Overlapping events
* Clock-style timestamps
* Millisecond timestamps
* Compiler-ready CSVs

Media fixtures include compatible sample videos used for merge and media-processing tests.

Keeping fixtures separate from test code prevents temporary test outputs and real-world media files from accumulating in the test directory.

---

# Development

The project uses a `src/` layout and editable installation during development.

Install the development dependencies:

```bash
pip install -e ".[dev]"
```

Run the test suite:

```bash
pytest -q
```

Run Python syntax/bytecode compilation:

```bash
python -m compileall -q src
```

Check individual modules when needed:

```bash
python -m py_compile src/video_tool/core.py
```

Check command-line help:

```bash
PYTHONPATH=src python -m video_tool.versions.video_tool_basic --help
```

```bash
PYTHONPATH=src python -m video_tool.versions.video_tool_pro --help
```

```bash
PYTHONPATH=src python -m video_tool.versions.auto_compiler --help
```

```bash
PYTHONPATH=src python -m video_tool.versions.merge_tool --help
```

For the experimental AI Finder:

```bash
PYTHONPATH=src python -m video_tool.experiments.ai_finder.ai_finder --help
```

---

# Testing Philosophy

Tests focus on the shared architecture rather than only individual command-line interfaces.

Important areas include:

* Source detection
* YouTube configuration
* Cache behavior
* Download counting
* Range handling
* Duration validation
* FFmpeg command construction
* Merge behavior
* Merge fallback
* Auto-Compiler orchestration
* CSV conversion
* Google Drive parsing
* Error handling

The normal development workflow is:

```text
Edit
  │
  ▼
Syntax check
  │
  ▼
Focused test
  │
  ▼
Full pytest suite
  │
  ▼
Manual CLI test when appropriate
```

---

# Error Handling

Shared exceptions are defined in:

```text
src/video_tool/errors.py
```

The project uses canonical error types so that command-line applications, the GUI, and future interfaces can interpret failures consistently.

Important exception types include:

```text
VideoToolError
MissingToolError
ClipLengthError
```

Download and processing failures can also be classified into user-facing error information containing:

* Severity
* Title
* Message
* Optional hint

This provides a foundation for informative CLI messages and GUI notifications without duplicating error-classification logic in every application.

---

# Notifications

Notification helpers are located in:

```text
src/video_tool/notify.py
```

The notification layer supports a callback-based interface while retaining a command-line/stdout fallback.

This allows the same processing code to be used by:

* CLI applications
* GUI applications
* Future interfaces

without coupling the shared processing layer to a specific UI framework.

---

# Versioning

The reorganized architecture starts at:

```text
1.0.0
```

The version is defined consistently in:

```text
src/video_tool/__init__.py
pyproject.toml
```

The `1.0.0` version represents the first release of the reorganized modular architecture rather than the historical numbering of earlier single-file implementations.

---

# Design Principles

The project follows several core principles.

## Shared Logic

Common processing belongs in shared modules.

Applications should orchestrate shared functionality rather than duplicate it.

## Source Separation

Source detection, source fetching, and media processing are separate responsibilities.

## Copy First

Media operations should avoid unnecessary re-encoding whenever stream copying is safe.

## Safe Fallbacks

When stream copying cannot satisfy the requested operation, the processing layer should fall back to re-encoding automatically where possible.

## Cache Reuse

Previously downloaded full sources should be reused instead of downloading the same source repeatedly.

## Thin Interfaces

CLI and GUI layers should remain thin wrappers around the shared processing architecture.

## Stable Public APIs

Refactoring should preserve existing public function signatures where practical so that individual applications do not need unnecessary rewrites.

## Testable Components

Core processing and source-handling logic should remain independently testable.

---

# Current Applications

| Component        | Status       | Purpose                                 |
| ---------------- | ------------ | --------------------------------------- |
| GUI              | Active       | Graphical YouTube downloader            |
| Basic Downloader | Active       | Simple complete downloads               |
| Pro Downloader   | Active       | Advanced downloads and range processing |
| Auto-Compiler    | Active       | CSV-driven video compilation            |
| Merge Tool       | Active       | Command-line video merging              |
| AI Finder        | Experimental | AI-assisted video/event discovery       |

---

# Project Direction

The project is moving toward a shared media-processing architecture in which different applications use the same source, caching, FFmpeg, validation, and error-handling infrastructure.

The intended flow is:

```text
                    User/Application
                           │
                           ▼
                    Source detection
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
       YouTube          Google Drive       Local
          │                │                │
          ▼                ▼                │
     Source adapter    Source adapter       │
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                    Shared media core
                           │
                 ┌─────────┼─────────┐
                 ▼         ▼         ▼
               Trim      Merge    Validate
                 │         │         │
                 └─────────┼─────────┘
                           ▼
                       Output media
```

This architecture allows future interfaces and processing workflows to reuse the same underlying functionality without creating separate implementations for each application.

---

# License

Copyright (C) Tisini 2026.

See the repository for the applicable license information.

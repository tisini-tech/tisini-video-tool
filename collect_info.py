#!/usr/bin/env python3
"""Collect facts about the video-tool release failure into one text file.

Run it from the project root, inside the venv you build from:

    python collect_info.py
    python collect_info.py --url https://youtu.be/SA_tJ3f7tnY
    python collect_info.py --release ~/Downloads/video-tool-gui-linux-1
    python collect_info.py --no-network      # skip the yt-dlp test runs

It writes vt-diagnostics.txt next to where you run it. It uses only the
standard library. It never prints cookie contents, .env values, API keys
or tokens, but read the file before you share it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_URL = "https://youtu.be/SA_tJ3f7tnY"

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    "dist", "build", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".idea", ".vscode",
}

# Files printed in full (cut at MAX_FILE characters).
FULL_NAMES = {
    "pyproject.toml", "version_check.py", "core.py", "errors.py",
    "clip_source.py",
}
FULL_GLOBS = ["requirements*.txt", "*.spec", "*.bat", "*.sh"]
MAX_FILE = 80_000

# For every other .py file, print only lines near these words.
KEYWORDS = re.compile(
    r"remote_components|js_runtimes|deno|yt_dlp_ejs|\bejs\b|_MEIPASS|"
    r"sys\.frozen|getattr\(sys, ?[\"']frozen|version_check|find_ffmpeg|"
    r"ffmpeg_location|self_update|pip install|importlib\.metadata|"
    r"yt_dlp\.version|DownloadOptions\(|cache_dir|platformdirs|appdirs|"
    r"XDG|LOCALAPPDATA|user_data|data_dir|subprocess",
    re.I,
)

REDACT_RULES = [
    (re.compile(
        r"""(?i)((?:api[_-]?key|secret|token|passw\w*)\w*["']?\s*[=:]\s*["'])"""
        r"""[^"'\n]{8,}(["'])"""), r"\1<redacted>\2"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "<redacted>"),
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,}"),
     "<redacted>"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
    (re.compile(r"https?://\S*googlevideo\.com/videoplayback\S*"),
     "<googlevideo url removed>"),
]

lines: list[str] = []


def redact(text: str) -> str:
    for pattern, repl in REDACT_RULES:
        text = pattern.sub(repl, text)
    return text


def add(text: object = "") -> None:
    lines.append(redact(str(text)))


def section(title: str) -> None:
    add()
    add("=" * 72)
    add(title)
    add("=" * 72)
    print(f"  {title}")


def run(cmd: list[str], timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            errors="replace",
        )
        return (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return f"<not found: {cmd[0]}>"
    except subprocess.TimeoutExpired as exc:
        part = exc.stdout or ""
        if isinstance(part, bytes):
            part = part.decode("utf-8", "replace")
        return f"<timed out after {timeout}s>\n{part}"
    except Exception as exc:  # noqa: BLE001
        return f"<error: {exc!r}>"


def clip(text: str, head: int = 45, tail: int = 45) -> str:
    rows = text.splitlines()
    if len(rows) <= head + tail:
        return text
    return "\n".join(
        rows[:head] + [f"   ... {len(rows) - head - tail} lines cut ..."]
        + rows[-tail:]
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def system_info() -> None:
    section("1. System")
    add(f"date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    add(f"platform: {platform.platform()}")
    add(f"python: {sys.version}")
    add(f"executable: {sys.executable}")
    add(f"in a venv: {sys.prefix != sys.base_prefix}")
    add(f"cwd: {Path.cwd()}")
    add(f"PATH: {os.environ.get('PATH', '')}")
    if not Path("pyproject.toml").is_file():
        add("WARNING: no pyproject.toml here. Run this from the project root.")


def tools_info() -> None:
    section("2. Tools on PATH")
    for name, arg in [
        ("deno", "--version"), ("node", "--version"), ("bun", "--version"),
        ("ffmpeg", "-version"), ("yt-dlp", "--version"),
        ("pyinstaller", "--version"),
    ]:
        path = shutil.which(name)
        if not path:
            add(f"{name}: not on PATH")
            continue
        first = run([path, arg]).splitlines()[:2]
        add(f"{name}: {path}  ->  {' | '.join(first)}")


def package_info() -> None:
    section("3. Python packages (this interpreter)")
    for name in ("yt-dlp", "yt-dlp-ejs", "pyinstaller", "customtkinter",
                 "requests", "filelock", "video-tool"):
        try:
            add(f"{name}: {md.version(name)}")
        except md.PackageNotFoundError:
            add(f"{name}: NOT INSTALLED")
    spec = importlib.util.find_spec("yt_dlp_ejs")
    add(f"yt_dlp_ejs importable: {bool(spec)} "
        f"{spec.origin if spec and spec.origin else ''}")
    add()
    add("pip list:")
    add(run([sys.executable, "-m", "pip", "list", "--format=freeze"]))


def cache_info() -> None:
    section("4. yt-dlp cache and config")
    candidates: list[Path] = []
    if os.environ.get("XDG_CACHE_HOME"):
        candidates.append(Path(os.environ["XDG_CACHE_HOME"]) / "yt-dlp")
    candidates.append(Path.home() / ".cache" / "yt-dlp")
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "yt-dlp")
    seen: set[Path] = set()
    for cache in candidates:
        if cache in seen:
            continue
        seen.add(cache)
        add(f"{cache}: {'exists' if cache.is_dir() else 'missing'}")
        if cache.is_dir():
            files = sorted(p for p in cache.rglob("*") if p.is_file())
            for path in files[:200]:
                stat = path.stat()
                day = time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime))
                add(f"  {path.relative_to(cache)}  {stat.st_size} bytes  {day}")
    for conf in (Path.home() / ".config" / "yt-dlp" / "config",
                 Path("yt-dlp.conf"), Path("/etc/yt-dlp.conf")):
        if conf.is_file():
            add(f"\nyt-dlp config {conf}:")
            add(conf.read_text(errors="replace")[:3000])


def list_files() -> list[Path]:
    root = Path.cwd()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            found.append((Path(dirpath) / name).relative_to(root))
    return sorted(found)


def is_full(rel: Path) -> bool:
    name = rel.name
    return (
        name in FULL_NAMES
        or any(fnmatch(name, pat) for pat in FULL_GLOBS)
        or rel.parts[:2] == (".github", "workflows")
        or rel.parts[0] == "tools"
    )


def read(rel: Path) -> str:
    try:
        data = rel.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<unreadable: {exc}>"
    if len(data) > MAX_FILE:
        data = data[:MAX_FILE] + f"\n<... cut at {MAX_FILE} characters>"
    return data


def keyword_lines(text: str, context: int = 5, limit: int = 30_000) -> str:
    rows = text.splitlines()
    hits = [i for i, row in enumerate(rows) if KEYWORDS.search(row)]
    keep: set[int] = set()
    for i in hits:
        keep.update(range(max(0, i - context), min(len(rows), i + context + 1)))
    out: list[str] = []
    prev = -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("   ...")
        out.append(f"{i + 1:5}| {rows[i]}")
        prev = i
    return "\n".join(out)[:limit]


def project_info(files: list[Path]) -> None:
    section("5. Project file list")
    shown = [f for f in files if f.suffix in
             {".py", ".toml", ".spec", ".bat", ".sh", ".yml", ".yaml",
              ".json", ".txt", ".md", ".cfg", ".ini"}]
    for rel in shown[:400]:
        add(f"  {rel}")

    section("6. Build and release files (full text)")
    for rel in files:
        if is_full(rel):
            add(f"\n----- {rel} -----")
            add(read(rel))

    section("7. Other Python files (only lines near update/ffmpeg/deno/freeze)")
    for rel in files:
        if rel.suffix == ".py" and not is_full(rel) and "tests" not in rel.parts:
            text = keyword_lines(read(rel))
            if text:
                add(f"\n----- {rel} -----")
                add(text)


def config_info(files: list[Path]) -> None:
    section("8. Config (secret values hidden)")
    for rel in files:
        if rel.parts[0] == "config" and rel.suffix == ".json":
            add(f"\n----- {rel} -----")
            add(read(rel))
    for rel in files:
        if rel.name.startswith(".env"):
            names = []
            for row in read(rel).splitlines():
                row = row.strip()
                if row and not row.startswith("#") and "=" in row:
                    names.append(row.split("=", 1)[0].strip())
            add(f"{rel}: variable names only -> {', '.join(names) or 'none'}")
    cookies = Path("youtube_cookies.txt")
    if cookies.is_file():
        first = ""
        with cookies.open(errors="replace") as fh:
            first = next((r.strip() for r in fh if r.strip()), "")
        add(f"youtube_cookies.txt: {cookies.stat().st_size} bytes, "
            f"first line: {first[:60]!r} (contents not printed)")
    else:
        add("youtube_cookies.txt: not present")


def git_info() -> None:
    section("9. Git")
    if not shutil.which("git"):
        add("git not on PATH")
        return
    add("last commits:")
    add(run(["git", "log", "--oneline", "-8"]))
    add("\nstatus:")
    add("\n".join(run(["git", "status", "--short"]).splitlines()[:40]))
    add("\nremotes:")
    add(run(["git", "remote", "-v"]))
    add("\nrecent tags:")
    add("\n".join(run(["git", "tag", "--sort=-creatordate"]).splitlines()[:8]))


MARKERS = [b"yt_dlp_ejs", b"yt-dlp-ejs", b".min.js"]


def scan_binary(path: Path) -> tuple[str, dict[bytes, int]]:
    counts = {m: 0 for m in MARKERS}
    sha = hashlib.sha256()
    tail = b""
    keep = max(len(m) for m in MARKERS) - 1
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 22):
            sha.update(chunk)
            buf = tail + chunk
            for m in MARKERS:
                counts[m] += buf.count(m)
            tail = buf[-keep:]
    return sha.hexdigest(), counts


def release_info(paths: list[str]) -> None:
    section("10. Release binaries (what is packed inside)")
    targets: list[Path] = [Path(p).expanduser() for p in paths]
    if not targets:
        for folder in (Path.home() / "Downloads", Path("dist")):
            if folder.is_dir():
                targets += sorted(folder.glob("video-tool-gui*"))
    if not targets:
        add("no release files found. Pass them with --release PATH")
        return
    for path in targets:
        if not path.is_file():
            add(f"{path}: missing")
            continue
        stat = path.stat()
        day = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        sha, counts = scan_binary(path)
        add(f"\n{path}")
        add(f"  size: {stat.st_size} bytes   modified: {day}")
        add(f"  sha256: {sha}")
        for marker, count in counts.items():
            add(f"  contains {marker.decode()!r}: {count} time(s)")


def yt_dlp_tests(url: str) -> None:
    section("11. yt-dlp test runs (network, up to 7 minutes)")
    base = [sys.executable, "-m", "yt_dlp", "-v", "-s", "--no-playlist"]
    with tempfile.TemporaryDirectory() as tmp:
        cases = [
            ("A. your normal cache", []),
            ("B. empty cache (like a new user)",
             ["--cache-dir", f"{tmp}/b"]),
            ("C. empty cache + --remote-components ejs:github",
             ["--cache-dir", f"{tmp}/c", "--remote-components", "ejs:github"]),
        ]
        for label, extra in cases:
            print(f"    running {label} ...")
            out = run(base + extra + [url], timeout=150)
            failed = (
                re.search(r"^ERROR", out, re.M) is not None
                or "timed out" in out
            )
            solver = "n challenge solving failed" in out
            add(f"\n----- {label} -----")
            add(f"result: {'FAILED' if failed else 'OK'}   "
                f"n-challenge failed: {solver}")
            add(clip(out))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--release", nargs="*", default=[],
                        help="release binaries to inspect")
    parser.add_argument("--no-network", action="store_true",
                        help="skip the yt-dlp test runs")
    parser.add_argument("--out", default="vt-diagnostics.txt")
    args = parser.parse_args()

    print("Collecting:")
    files = list_files()
    steps = [
        system_info, tools_info, package_info, cache_info,
        lambda: project_info(files), lambda: config_info(files), git_info,
        lambda: release_info(args.release),
    ]
    if not args.no_network:
        steps.append(lambda: yt_dlp_tests(args.url))

    for step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            add(f"<step failed: {exc!r}>")

    out = Path(args.out)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {out} ({out.stat().st_size // 1024} KB).")
    print("Read it, remove anything private, then send it to Claude.")


if __name__ == "__main__":
    main()

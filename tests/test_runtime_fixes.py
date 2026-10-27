"""Tests for the Windows runtime fixes.

They cover: the right version flag per tool (Deno was rejected because the
FFmpeg flag was used for it), not re-running files that already passed,
hidden helper windows, live progress, short error text, and the fallback
when the standalone yt-dlp will not start.
"""

from __future__ import annotations

import http.server
import shutil
import stat
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from video_tool import procutil, youtube
from video_tool.gui import bundled_deps

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="uses small shell scripts as fake tools"
)


# ---------------------------------------------------------------------------
# Version flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("ffmpeg", "-version"),
        ("ffmpeg.exe", "-version"),
        ("ffmpeg.exe.new", "-version"),
        ("ffprobe.exe", "-version"),
        ("deno", "--version"),
        ("deno.exe", "--version"),
        ("deno.exe.new", "--version"),
        ("yt-dlp.exe", "--version"),
        ("yt-dlp.exe.new", "--version"),
    ],
)
def test_version_flag_matches_the_tool(name, expected):
    assert bundled_deps._version_arg(name) == expected


def _fake_tool(directory: Path, name: str, accepts: str, output: str) -> Path:
    """Make a script that prints ``output`` for ``accepts`` and fails otherwise.

    Real Deno behaves this way: ``deno -version`` is an error.
    """
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "{accepts}" ]; then echo "{output}"; exit 0; fi\n'
        'echo "error: unexpected argument" >&2\n'
        "exit 1\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@posix_only
def test_deno_archive_installs(tmp_path, monkeypatch):
    """Regression: a Deno that only knows --version must pass the check."""
    monkeypatch.setattr(bundled_deps, "STATE_PATH", tmp_path / "state.json")

    source = tmp_path / "src"
    source.mkdir()
    fake = _fake_tool(source, "deno", "--version", "deno 9.9.9")

    archive = tmp_path / "deno.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(fake, "deno")

    destination = tmp_path / "bin" / "deno"

    assert bundled_deps._extract_archive_binary(archive, destination, "deno")
    assert destination.exists()


@posix_only
def test_ffmpeg_archive_still_installs(tmp_path, monkeypatch):
    """The FFmpeg fix must keep working: these tools only know -version."""
    monkeypatch.setattr(bundled_deps, "STATE_PATH", tmp_path / "state.json")

    source = tmp_path / "src"
    source.mkdir()
    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in ("ffmpeg", "ffprobe"):
            fake = _fake_tool(source, name, "-version", f"{name} version 1")
            zf.write(fake, f"bin/{name}")

    binaries = {
        "ffmpeg": tmp_path / "bin" / "ffmpeg",
        "ffprobe": tmp_path / "bin" / "ffprobe",
    }

    assert bundled_deps._extract_archive_binaries(archive, binaries)
    assert all(path.exists() for path in binaries.values())


# ---------------------------------------------------------------------------
# Not re-running files that already passed
# ---------------------------------------------------------------------------


def test_verified_file_is_not_run_again(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled_deps, "STATE_PATH", tmp_path / "state.json")
    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"one")

    calls = []
    monkeypatch.setattr(
        bundled_deps, "_works", lambda path, *a: calls.append(path) or True
    )

    assert bundled_deps._usable(tool)
    assert bundled_deps._usable(tool)
    assert len(calls) == 1  # the second answer came from the saved record

    tool.write_bytes(b"a different file")  # changed on disk: check again
    assert bundled_deps._usable(tool)
    assert len(calls) == 2


def test_usable_tries_again_before_giving_up(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled_deps, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(bundled_deps.time, "sleep", lambda s: None)
    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"x")

    results = iter([False, True])  # first start fails, second works
    monkeypatch.setattr(bundled_deps, "_works", lambda *a: next(results))

    assert bundled_deps._usable(tool)


def test_failed_check_keeps_the_file(tmp_path, monkeypatch):
    """One bad check must not delete a file that may be fine."""
    monkeypatch.setattr(bundled_deps, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(bundled_deps, "BUNDLE_DIR", tmp_path / "bin")
    monkeypatch.setattr(bundled_deps.time, "sleep", lambda s: None)
    monkeypatch.setattr(bundled_deps, "_works", lambda *a: False)
    monkeypatch.setattr(bundled_deps, "_install_binary", lambda *a: False)

    (tmp_path / "bin").mkdir()
    existing = tmp_path / "bin" / bundled_deps._binary_name("yt-dlp")
    existing.write_bytes(b"maybe fine")

    result = bundled_deps._update_binary(
        "yt-dlp", "http://example.invalid/x", "ytdlp_checked_at", None
    )

    assert result is None  # not offered for use
    assert existing.exists()  # but not deleted


# ---------------------------------------------------------------------------
# Deno update check
# ---------------------------------------------------------------------------


class _Head:
    def __init__(self, location):
        self.headers = {"Location": location}


def test_latest_deno_version_is_read_from_the_redirect(monkeypatch):
    monkeypatch.setattr(
        bundled_deps.requests,
        "head",
        lambda *a, **k: _Head(
            "https://github.com/denoland/deno/releases/tag/v2.9.8"
        ),
    )
    assert bundled_deps._latest_deno_version() == "2.9.8"


def test_latest_deno_version_survives_a_bad_answer(monkeypatch):
    monkeypatch.setattr(
        bundled_deps.requests, "head", lambda *a, **k: _Head("")
    )
    assert bundled_deps._latest_deno_version() is None


def test_current_deno_is_not_downloaded_again(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled_deps, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(bundled_deps, "BUNDLE_DIR", tmp_path / "bin")
    monkeypatch.setattr(bundled_deps, "SYSTEM", "Linux")
    monkeypatch.setattr(bundled_deps, "MACHINE", "x86_64")
    monkeypatch.setattr(bundled_deps, "_usable", lambda path, *a: True)
    monkeypatch.setattr(bundled_deps, "_latest_deno_version", lambda: "2.0.0")
    monkeypatch.setattr(
        bundled_deps, "_installed_deno_version", lambda path: "2.0.0"
    )

    def must_not_download(*args, **kwargs):
        raise AssertionError("Deno was downloaded although it is current")

    monkeypatch.setattr(bundled_deps, "_download", must_not_download)

    result = bundled_deps.ensure_deno()

    assert result == tmp_path / "bin" / "deno"
    # The check is recorded, so the next start does not even look.
    assert not bundled_deps._due_for_check(
        bundled_deps._load_state(), "deno_checked_at"
    )


# ---------------------------------------------------------------------------
# procutil
# ---------------------------------------------------------------------------


def test_helpers_do_nothing_on_a_normal_linux_run(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(procutil, "IS_WINDOWS", False)

    assert procutil.child_process_kwargs() == {}


def test_frozen_child_env_drops_pyinstaller_variables(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("_MEIPASS2", "C:\\Temp\\_MEI123")
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", "C:\\app.exe")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib")

    env = procutil.child_env()

    assert env is not None
    assert "_MEIPASS2" not in env
    assert "_PYI_ARCHIVE_FILE" not in env
    assert env["LD_LIBRARY_PATH"] == "/usr/lib"


def test_popen_gets_no_window_flag_on_windows(monkeypatch):
    class FakePopen:
        seen: dict = {}

        def __init__(self, *args, **kwargs):
            FakePopen.seen = kwargs

    monkeypatch.setattr(procutil, "IS_WINDOWS", True)
    monkeypatch.setattr(procutil.subprocess, "Popen", FakePopen)

    procutil.install_no_window_default()
    procutil.install_no_window_default()  # twice is harmless

    FakePopen(["tool"])
    assert FakePopen.seen["creationflags"] == procutil.CREATE_NO_WINDOW

    # A caller that chose its own console behaviour is left alone.
    FakePopen(["tool"], creationflags=procutil.DETACHED_PROCESS)
    assert FakePopen.seen["creationflags"] == procutil.DETACHED_PROCESS


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


def _report(done, total, streams=2):
    return {
        "status": "downloading",
        "downloaded_bytes": done,
        "total_bytes": total,
        "info_dict": {"requested_formats": [{}] * streams},
    }


def test_progress_runs_forward_across_video_and_audio():
    tracker = youtube.ProgressTracker()
    values = [
        tracker.fraction(_report(0, 1000)),
        tracker.fraction(_report(500, 1000)),
        tracker.fraction(_report(1000, 1000)),  # video stream done
        tracker.fraction(_report(0, 100)),      # audio stream starts
        tracker.fraction(_report(100, 100)),
    ]

    assert values[0] == 0.0
    assert values[2] == pytest.approx(0.5)
    assert values[-1] == pytest.approx(1.0)
    assert values == sorted(values)  # never backwards


def test_progress_never_goes_backwards_when_stream_count_is_unknown():
    tracker = youtube.ProgressTracker()
    plain = lambda done, total: {  # noqa: E731
        "status": "downloading",
        "downloaded_bytes": done,
        "total_bytes": total,
    }
    values = [
        tracker.fraction(plain(1000, 1000)),
        tracker.fraction(plain(0, 100)),
        tracker.fraction(plain(100, 100)),
    ]
    assert values == sorted(values)
    assert max(values) <= 1.0


def test_progress_uses_fragments_when_size_is_unknown():
    tracker = youtube.ProgressTracker()
    value = tracker.fraction(
        {
            "status": "downloading",
            "downloaded_bytes": 10,
            "fragment_index": 3,
            "fragment_count": 6,
        }
    )
    assert value == pytest.approx(0.5)


def test_progress_ignores_reports_without_numbers():
    tracker = youtube.ProgressTracker()
    assert tracker.fraction({"status": "finished"}) is None
    assert tracker.fraction({"status": "downloading"}) is None
    assert (
        tracker.fraction({"status": "downloading", "downloaded_bytes": "NA"})
        is None
    )


# ---------------------------------------------------------------------------
# Error text
# ---------------------------------------------------------------------------

BOT_CHECK_OUTPUT = """\
WARNING: [youtube] TkPhc7HHJuU: Unable to download webpage: HTTP Error 429: Too Many Requests (caused by <HTTPError 429: Too Many Requests>)
WARNING: [youtube] No supported JavaScript runtime could be found. Only deno is enabled by default.
WARNING: [youtube] No title found in player responses; falling back to title from initial data.
ERROR: [youtube] TkPhc7HHJuU: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication.
"""


def test_error_text_keeps_the_error_and_drops_the_warnings():
    text = youtube._clean_ytdlp_output(BOT_CHECK_OUTPUT, 1)

    assert text.startswith("ERROR: [youtube] TkPhc7HHJuU: Sign in to confirm")
    assert "WARNING" not in text
    assert "JavaScript" not in text


def test_error_text_keeps_the_unpack_failure_lines():
    output = (
        "[PYI-21604:ERROR] Failed to extract python310.dll: "
        "decompression resulted in return code -1!\n"
        "[PYI-21604:ERROR] Failed to extract entry: python310.dll.\n"
    )
    text = youtube._clean_ytdlp_output(output, 1)

    assert "python310.dll" in text
    assert len(text.splitlines()) == 2


def test_silent_failure_names_the_exit_code():
    text = youtube._clean_ytdlp_output("", 3221225477)
    assert "3221225477" in text


def test_launch_failure_detection():
    pyi = "[PYI-21604:ERROR] Failed to extract python310.dll"

    assert youtube._is_launch_failure(pyi, [], 1)
    assert youtube._is_launch_failure("", [], 1)  # killed, nothing printed
    assert not youtube._is_launch_failure("", [], 0)
    assert not youtube._is_launch_failure("ERROR: Video unavailable", [], 1)


# ---------------------------------------------------------------------------
# Whole download against a slow local server (real yt-dlp, no internet)
# ---------------------------------------------------------------------------


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    payload = b"x" * (3 * 1024 * 1024)

    def _head(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()

    def do_HEAD(self):
        self._head()

    def do_GET(self):
        self._head()
        step = 128 * 1024
        for start in range(0, len(self.payload), step):
            self.wfile.write(self.payload[start:start + step])
            self.wfile.flush()
            time.sleep(0.04)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_video_url(monkeypatch):
    # Never send this traffic through a proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/sample.mp4"
    finally:
        server.shutdown()


@posix_only
def test_standalone_download_reports_progress(tmp_path, local_video_url):
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        pytest.skip("yt-dlp command not installed")

    reports = []
    options = youtube.DownloadOptions(
        output_dir=str(tmp_path / "out"),
        yt_dlp_executable=ytdlp,
        progress_hook=reports.append,
    )

    path = youtube.download_url(local_video_url, options)

    assert path and Path(path).is_file()
    assert Path(path).stat().st_size == len(_SlowHandler.payload)

    tracker = youtube.ProgressTracker()
    fractions = [tracker.fraction(r) for r in reports]
    fractions = [f for f in fractions if f is not None]

    assert len(fractions) >= 3, "the bar should move more than once"
    assert fractions == sorted(fractions)
    assert fractions[-1] == pytest.approx(1.0, abs=0.01)


@posix_only
def test_broken_standalone_falls_back_to_built_in(
    tmp_path, local_video_url, monkeypatch
):
    """yt-dlp.exe that cannot unpack itself must not fail the download."""
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)

    broken = tmp_path / "yt-dlp-broken"
    broken.write_text(
        "#!/bin/sh\n"
        'echo "[PYI-21604:ERROR] Failed to extract python310.dll: '
        'decompression resulted in return code -1!" >&2\n'
        "exit 1\n"
    )
    broken.chmod(broken.stat().st_mode | stat.S_IXUSR)

    options = youtube.DownloadOptions(
        output_dir=str(tmp_path / "out"),
        yt_dlp_executable=str(broken),
    )

    path = youtube.download_url(local_video_url, options)

    assert path and Path(path).is_file()


@posix_only
def test_broken_standalone_raises_a_launch_error_on_its_own(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube, "_LAST_LOG_PATH", tmp_path / "last.log")

    silent = tmp_path / "yt-dlp-silent"
    silent.write_text("#!/bin/sh\nexit 7\n")
    silent.chmod(silent.stat().st_mode | stat.S_IXUSR)

    options = youtube.DownloadOptions(
        output_dir=str(tmp_path / "out"),
        yt_dlp_executable=str(silent),
    )

    with pytest.raises(youtube.ExecutableLaunchError) as caught:
        youtube._download_url_with_executable("http://example.invalid/", options)

    assert "exit code 7" in str(caught.value)
    assert (tmp_path / "last.log").exists()  # full output kept for support

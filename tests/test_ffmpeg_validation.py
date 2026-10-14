import io
import tarfile

from video_tool.gui import bundled_deps as b

FAKE = """#!/bin/sh
if [ "$1" = "-version" ]; then
  echo "NAME version test"
  exit 0
fi
echo "Unrecognized option '$1'" >&2
exit 8
"""


def make_archive(path):
    with tarfile.open(path, "w:xz") as tf:
        for name in ("ffmpeg", "ffprobe"):
            data = FAKE.replace("NAME", name).encode()
            info = tarfile.TarInfo(f"pkg/bin/{name}")
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))


def test_works_uses_single_dash_for_ffmpeg_tools(tmp_path):
    make_archive(tmp_path / "a.tar.xz")
    dest = {"ffmpeg": tmp_path / "bin" / "ffmpeg",
            "ffprobe": tmp_path / "bin" / "ffprobe"}
    assert b._extract_archive_binaries(tmp_path / "a.tar.xz", dest)
    assert b._works(dest["ffmpeg"])
    assert b._works(dest["ffprobe"])


def test_run_check_reports_exit_code(tmp_path):
    make_archive(tmp_path / "a.tar.xz")
    dest = {"ffmpeg": tmp_path / "bin" / "ffmpeg",
            "ffprobe": tmp_path / "bin" / "ffprobe"}
    b._extract_archive_binaries(tmp_path / "a.tar.xz", dest)
    ok, detail = b._run_check(dest["ffmpeg"], "--version")
    assert not ok
    assert "exit code 8" in detail

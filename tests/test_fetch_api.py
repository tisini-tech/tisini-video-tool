"""Guards for the shared fetch API.

The first test reads every call to fetch_clip() and fetch_source() in
the source tree and checks it against the real definition. It needs no
network. It exists because a change to the signature once broke five
callers that no other test touched.
"""

import ast
import inspect
from pathlib import Path

import pytest

from video_tool import clip_source

SRC = Path(clip_source.__file__).parent
NAMES = {"fetch_clip", "fetch_source"}


def _calls():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            name = getattr(func, "id", getattr(func, "attr", None))

            if name in NAMES:
                yield path, node, name


def test_every_call_matches_its_definition():
    checked = 0

    for path, node, name in _calls():
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue

        if any(k.arg is None for k in node.keywords):
            continue

        signature = inspect.signature(getattr(clip_source, name))

        try:
            signature.bind(
                *[None] * len(node.args),
                **{k.arg: None for k in node.keywords},
            )
        except TypeError as exc:
            pytest.fail(
                f"{path.relative_to(SRC)}:{node.lineno} "
                f"calls {name}() wrongly: {exc}"
            )

        checked += 1

    # Basic, Pro, merge, GUI, auto_compiler (2), ai_finder, and
    # fetch_source's own call to fetch_clip.
    assert checked >= 8


def test_both_functions_take_the_same_arguments():
    clip = inspect.signature(clip_source.fetch_clip).parameters
    source = inspect.signature(clip_source.fetch_source).parameters

    assert list(clip)[1:] == list(source)[1:]


def test_missing_output_path_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        clip_source, "download_url", lambda url, options: None
    )

    options = clip_source.DownloadOptions(output_dir=str(tmp_path))

    with pytest.raises(FileNotFoundError, match="did not produce"):
        clip_source.fetch_clip(
            "https://youtu.be/abcdefghijk",
            tmp_path, None, None, options,
            full_download=True,
        )

    with pytest.raises(FileNotFoundError, match="did not produce"):
        clip_source.download_range(
            "https://youtu.be/abcdefghijk",
            options, 0, 5, attempts=1,
        )


def _make_clip(path: Path, seconds: int) -> Path:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=25",
            "-f", "lavfi", "-i", f"sine=duration={seconds}:sample_rate=48000",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(path),
        ],
        check=True,
    )
    return path


@pytest.mark.skipif(
    not __import__("shutil").which("ffmpeg"), reason="ffmpeg is needed"
)
def test_full_download_does_not_delete_a_file_that_was_already_there(
    tmp_path, monkeypatch
):
    """yt-dlp reuses a finished download. That file belongs to the user."""
    existing = _make_clip(tmp_path / "My video.mp4", 4)

    # yt-dlp says "already been downloaded" and returns the old file.
    monkeypatch.setattr(
        clip_source, "download_url", lambda url, options: str(existing)
    )

    options = clip_source.DownloadOptions(output_dir=str(tmp_path))
    clip, downloaded = clip_source.fetch_clip(
        "https://youtu.be/abcdefghijk",
        tmp_path, 0, 2, options,
        no_cache=True, full_download=True, keep_original=False,
    )

    assert clip.is_file()
    assert existing.is_file(), "the user's own full video was deleted"


@pytest.mark.skipif(
    not __import__("shutil").which("ffmpeg"), reason="ffmpeg is needed"
)
def test_full_download_still_deletes_its_own_temporary_file(
    tmp_path, monkeypatch
):
    """A file this run downloaded is removed after trimming."""
    source = tmp_path / "template.mp4"
    _make_clip(source, 4)
    work = tmp_path / "work"
    work.mkdir()

    def fake_download(url, options):
        target = work / "Fresh video.mp4"
        target.write_bytes(source.read_bytes())
        return str(target)

    monkeypatch.setattr(clip_source, "download_url", fake_download)

    options = clip_source.DownloadOptions(output_dir=str(work))
    clip, downloaded = clip_source.fetch_clip(
        "https://youtu.be/abcdefghijk",
        work, 0, 2, options,
        no_cache=True, full_download=True, keep_original=False,
    )

    assert downloaded is True
    assert clip.is_file()
    assert not (work / "Fresh video.mp4").exists()

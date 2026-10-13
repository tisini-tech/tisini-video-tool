"""The vt-api-try command, run against the API in memory."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from video_tool.api.app import create_app  # noqa: E402
from video_tool.api.security import Settings  # noqa: E402
from video_tool.api.try_it import run  # noqa: E402
from video_tool.errors import DownloadError  # noqa: E402

LINK = "https://youtu.be/abcdefghijk"


def make_client(tmp_path, runner):
    settings = Settings(api_key="k", work_dir=tmp_path / "w")
    app = create_app(settings, runners={"basic": runner}, janitor=False)

    return TestClient(app, headers={"X-API-Key": "k", "X-User-Id": "ann"})


def test_the_command_saves_the_file(tmp_path, capsys):
    def good(params, folder, progress):
        out = folder / "clip.mp4"
        out.write_bytes(b"video")
        return out

    client = make_client(tmp_path, good)
    code = run(LINK, client, tmp_path / "out", pause=0.02)

    saved = list((tmp_path / "out").glob("*.mp4"))

    assert code == 0
    assert saved and saved[0].read_bytes() == b"video"
    assert "Saved:" in capsys.readouterr().out


def test_the_command_prints_a_plain_reason_when_a_job_fails(tmp_path, capsys):
    def bad(params, folder, progress):
        raise DownloadError("ERROR: [youtube] x: This video is unavailable")

    client = make_client(tmp_path, bad)
    code = run(LINK, client, tmp_path / "out", pause=0.02)
    shown = capsys.readouterr().out

    assert code == 1
    assert "Video not available" in shown
    assert "yt-dlp" not in shown


def test_the_command_keeps_the_name_the_server_chose(tmp_path):
    def titled(params, folder, progress):
        out = folder / "Me at the zoo.mp4"
        out.write_bytes(b"video")
        return out

    client = make_client(tmp_path, titled)
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0

    assert (tmp_path / "out" / "Me at the zoo.mp4").read_bytes() == b"video"


def test_the_command_never_overwrites_an_earlier_file(tmp_path):
    def titled(params, folder, progress):
        out = folder / "Me at the zoo.mp4"
        out.write_bytes(b"video")
        return out

    client = make_client(tmp_path, titled)
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0

    names = sorted(p.name for p in (tmp_path / "out").glob("*.mp4"))

    assert len(names) == 2
    assert "Me at the zoo.mp4" in names


def test_the_command_keeps_the_name_the_server_chose(tmp_path):
    def titled(params, folder, progress):
        out = folder / "Me at the zoo.mp4"
        out.write_bytes(b"video")
        return out

    client = make_client(tmp_path, titled)
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0

    assert (tmp_path / "out" / "Me at the zoo.mp4").read_bytes() == b"video"


def test_the_command_never_overwrites_an_earlier_file(tmp_path):
    def titled(params, folder, progress):
        out = folder / "Me at the zoo.mp4"
        out.write_bytes(b"video")
        return out

    client = make_client(tmp_path, titled)
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0

    names = sorted(p.name for p in (tmp_path / "out").glob("*.mp4"))

    assert len(names) == 2
    assert "Me at the zoo.mp4" in names


def test_the_command_keeps_the_name_the_server_chose(tmp_path):
    def titled(params, folder, progress):
        out = folder / "Me at the zoo.mp4"
        out.write_bytes(b"video")
        return out

    client = make_client(tmp_path, titled)
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0

    assert (tmp_path / "out" / "Me at the zoo.mp4").read_bytes() == b"video"


def test_the_command_never_overwrites_an_earlier_file(tmp_path):
    def titled(params, folder, progress):
        out = folder / "Me at the zoo.mp4"
        out.write_bytes(b"video")
        return out

    client = make_client(tmp_path, titled)
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0

    names = sorted(p.name for p in (tmp_path / "out").glob("*.mp4"))

    assert len(names) == 2
    assert "Me at the zoo.mp4" in names

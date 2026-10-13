"""Naming of saved files in vt-api-try."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from video_tool.api.app import create_app  # noqa: E402
from video_tool.api.security import Settings  # noqa: E402
from video_tool.api.try_it import run  # noqa: E402

LINK = "https://youtu.be/abcdefghijk"


def titled(params, folder, progress):
    out = folder / "Me at the zoo.mp4"
    out.write_bytes(b"video")
    return out


def make_client(tmp_path):
    settings = Settings(api_key="k", work_dir=tmp_path / "w")
    app = create_app(settings, runners={"basic": titled}, janitor=False)

    return TestClient(app, headers={"X-API-Key": "k", "X-User-Id": "ann"})


def test_the_command_keeps_the_name_the_server_chose(tmp_path):
    client = make_client(tmp_path)

    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0
    assert (tmp_path / "out" / "Me at the zoo.mp4").read_bytes() == b"video"


def test_the_command_never_overwrites_an_earlier_file(tmp_path):
    client = make_client(tmp_path)

    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0
    assert run(LINK, client, tmp_path / "out", pause=0.02) == 0

    names = sorted(p.name for p in (tmp_path / "out").glob("*.mp4"))

    assert len(names) == 2
    assert "Me at the zoo.mp4" in names

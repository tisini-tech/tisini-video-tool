"""The optional test page."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from video_tool.api.app import create_app  # noqa: E402
from video_tool.api.security import Settings  # noqa: E402


def quick(params, folder, progress):
    out = folder / "clip.mp4"
    out.write_bytes(b"video")
    return out


def make_client(tmp_path, **options):
    settings = Settings(api_key="secret-key", work_dir=tmp_path / "w")
    app = create_app(
        settings, runners={"basic": quick}, janitor=False, **options
    )

    return TestClient(app)


def test_the_page_is_off_by_default(tmp_path):
    assert make_client(tmp_path).get("/test").status_code == 404


def test_the_page_is_served_when_asked(tmp_path):
    reply = make_client(tmp_path, test_page=True).get("/test")

    assert reply.status_code == 200
    assert "text/html" in reply.headers["content-type"]
    assert "Video Tool API" in reply.text


def test_the_page_never_contains_the_key(tmp_path):
    reply = make_client(tmp_path, test_page=True).get("/test")

    assert "secret-key" not in reply.text

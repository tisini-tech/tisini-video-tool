"""API tests. They use fake runners, so they need no network."""

import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from video_tool.api import runners  # noqa: E402
from video_tool.api.app import create_app  # noqa: E402
from video_tool.api.jobs import JobStore  # noqa: E402
from video_tool.api.security import Settings  # noqa: E402
from video_tool.errors import DownloadError  # noqa: E402

KEY = "test-key"
LINK = "https://youtu.be/abcdefghijk"


def headers(user="ann"):
    return {"X-API-Key": KEY, "X-User-Id": user}


def body():
    return {"tool": "basic", "url": LINK}


def quick(params, folder, progress):
    progress(0.5)
    out = folder / "clip.mp4"
    out.write_bytes(b"video")
    return out


def make_client(tmp_path, runner):
    settings = Settings(api_key=KEY, work_dir=tmp_path / "work")
    return TestClient(create_app(settings, runners={"basic": runner}))


def wait_for(client, job_id, user="ann"):
    data = {}
    for _ in range(100):
        data = client.get(f"/v1/jobs/{job_id}", headers=headers(user)).json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(data)


def test_a_job_runs_and_the_file_downloads(tmp_path):
    client = make_client(tmp_path, quick)
    start = client.post("/v1/jobs", json=body(), headers=headers())
    assert start.status_code == 202

    done = wait_for(client, start.json()["id"])
    assert done["status"] == "done" and done["progress"] == 1.0

    got = client.get(done["file_url"], headers=headers())
    assert got.status_code == 200 and got.content == b"video"


def test_a_missing_or_wrong_key_is_refused(tmp_path):
    client = make_client(tmp_path, quick)
    assert client.post("/v1/jobs", json=body()).status_code == 401
    bad = {"X-API-Key": "nope", "X-User-Id": "ann"}
    assert client.post("/v1/jobs", json=body(), headers=bad).status_code == 401


def test_a_bad_link_is_refused_before_a_job_starts(tmp_path):
    client = make_client(tmp_path, quick)
    reply = client.post("/v1/jobs", json={"tool": "basic", "url": "https://example.com/x"}, headers=headers())
    assert reply.status_code == 422


def test_one_running_job_per_user(tmp_path):
    gate = threading.Event()

    def slow(params, folder, progress):
        gate.wait(5)
        return quick(params, folder, progress)

    client = make_client(tmp_path, slow)
    first = client.post("/v1/jobs", json=body(), headers=headers())
    assert first.status_code == 202

    second = client.post("/v1/jobs", json=body(), headers=headers())
    assert second.status_code == 429
    assert second.json()["detail"]["title"]

    other = client.post("/v1/jobs", json=body(), headers=headers("bob"))
    assert other.status_code == 202

    gate.set()
    wait_for(client, first.json()["id"])
    wait_for(client, other.json()["id"], "bob")

    third = client.post("/v1/jobs", json=body(), headers=headers())
    assert third.status_code == 202
    wait_for(client, third.json()["id"])


def test_users_cannot_see_each_others_jobs(tmp_path):
    client = make_client(tmp_path, quick)
    job = client.post("/v1/jobs", json=body(), headers=headers()).json()
    wait_for(client, job["id"])

    assert client.get(f"/v1/jobs/{job['id']}", headers=headers("bob")).status_code == 404
    assert client.get(f"/v1/jobs/{job['id']}/file", headers=headers("bob")).status_code == 404
    assert client.delete(f"/v1/jobs/{job['id']}", headers=headers("bob")).status_code == 404


def test_a_failed_job_returns_plain_text_only(tmp_path):
    def broken(params, folder, progress):
        raise DownloadError("ERROR: [youtube] x: This video is unavailable")

    client = make_client(tmp_path, broken)
    job = client.post("/v1/jobs", json=body(), headers=headers()).json()
    done = wait_for(client, job["id"])

    assert done["status"] == "failed"
    assert done["error"]["code"] == "unavailable"
    assert set(done["error"]) == {"level", "title", "message", "hint", "code"}
    assert "yt-dlp" not in str(done["error"])


def test_deleting_a_finished_job_removes_its_folder(tmp_path):
    client = make_client(tmp_path, quick)
    job = client.post("/v1/jobs", json=body(), headers=headers()).json()
    wait_for(client, job["id"])
    folder = tmp_path / "work" / job["id"]
    assert folder.exists()

    assert client.delete(f"/v1/jobs/{job['id']}", headers=headers()).status_code == 200
    assert not folder.exists()
    assert client.get(f"/v1/jobs/{job['id']}", headers=headers()).status_code == 404


def test_old_finished_jobs_expire(tmp_path):
    now = [1000.0]
    store = JobStore(tmp_path / "w", {"basic": quick}, ttl=60, clock=lambda: now[0])
    job = store.submit("ann", "basic", {})

    for _ in range(100):
        if store.get("ann", job.id).status == "done":
            break
        time.sleep(0.05)

    assert job.folder.exists()
    now[0] += 61
    assert store.get("ann", job.id) is None
    assert not job.folder.exists()
    store.shutdown()


def test_basic_runner_never_uses_the_shared_cache(tmp_path, monkeypatch):
    seen = {}

    def fake_fetch(url, output_dir, start, end, options, **kw):
        seen.update(kw=kw, options=options)
        out = Path(output_dir) / "a.mp4"
        out.write_bytes(b"x")
        return out, True

    monkeypatch.setattr(runners, "fetch_clip", fake_fetch)
    settings = Settings(api_key=KEY, work_dir=tmp_path, cookie_file="/srv/c.txt")
    run = runners.make_runners(settings)["basic"]

    path = run({"url": LINK, "format": "MP4", "quality": "Best"}, tmp_path, lambda v: None)

    assert seen["kw"]["no_cache"] is True
    assert seen["options"].cookie_file == "/srv/c.txt"
    assert path.exists()

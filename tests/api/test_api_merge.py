"""Merge tool tests. Fake runners and a fake clock, no network.

The request-shape, runner and job-store tests need only pydantic. The
tests that go through the web routes skip themselves without fastapi.
"""

import threading
import time
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from video_tool.api import runners
from video_tool.api.jobs import JobStore
from video_tool.api.schemas import MAX_MERGE_CLIPS, JobRequest
from video_tool.api.security import Limits, Settings
from video_tool.errors import DownloadError

KEY = "test-key"
A = "https://youtu.be/abcdefghijk"
B = "https://www.youtube.com/watch?v=lmnopqrstuv"
A_FIXED = "https://www.youtube.com/watch?v=abcdefghijk"
B_FIXED = "https://www.youtube.com/watch?v=lmnopqrstuv"

request = TypeAdapter(JobRequest)


def headers(user="ann"):
    return {"X-API-Key": KEY, "X-User-Id": user}


def wait_for(store, user, job_id, wanted=("done", "failed")):
    for _ in range(200):
        job = store.get(user, job_id)
        if job and job.status in wanted:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not reach " + "/".join(wanted))


# ------------------------------------------------------------ request shape


def test_a_merge_request_is_cleaned_and_keeps_its_order():
    body = request.validate_python({"tool": "merge", "urls": [B, A]})

    assert body.tool == "merge"
    assert body.params() == {
        "sources": [{"url": B_FIXED}, {"url": A_FIXED}],
        "quality": "Best",
    }


def test_a_basic_request_still_works():
    body = request.validate_python({"tool": "basic", "url": A})

    assert body.params() == {"url": A_FIXED, "format": "MP4", "quality": "Best"}


def test_the_same_link_may_be_merged_twice():
    body = request.validate_python({"tool": "merge", "urls": [A, A]})

    assert body.params()["sources"] == [{"url": A_FIXED}, {"url": A_FIXED}]


@pytest.mark.parametrize(
    "body",
    [
        {"tool": "merge", "urls": [A]},
        {"tool": "merge", "urls": [A] * (MAX_MERGE_CLIPS + 1)},
        {"tool": "merge", "urls": []},
        {"tool": "merge", "urls": [A, "https://example.com/video"]},
        {"tool": "merge", "urls": [A, "not a link"]},
        {"tool": "merge", "urls": [A, B], "format": "MP3"},
        {"tool": "merge", "urls": [A, B], "quality": "4K"},
        {"tool": "merge", "url": A},
        {"tool": "merge", "urls": A},
        {"tool": "merge", "urls": [A, B], "path": "/etc/passwd"},
        {"tool": "other", "urls": [A, B]},
        {"urls": [A, B]},
        {"tool": "merge"},
        {"tool": "merge", "urls": [A, B], "sources": [{"url": A}, {"url": B}]},
    ],
)
def test_bad_merge_requests_are_refused(body):
    with pytest.raises(ValidationError):
        request.validate_python(body)


# ------------------------------------------------------------ the runner


@pytest.fixture
def fake_tools(monkeypatch):
    seen = {"fetch": [], "options": [], "merge": []}

    def fetch(url, folder, start, end, options, **kwargs):
        seen["fetch"].append((url, Path(folder).name))
        seen["options"].append(options)
        options.progress_hook(
            {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100}
        )
        out = Path(folder) / "clip.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")

        return out, False

    def merge(ffmpeg_path, clip_paths, output_path, **kwargs):
        seen["merge"].append((ffmpeg_path, list(clip_paths), output_path, kwargs))
        Path(output_path).write_bytes(b"merged")

        return output_path

    monkeypatch.setattr(runners, "fetch_clip", fetch)
    monkeypatch.setattr(runners, "merge_clips", merge)
    monkeypatch.setattr(runners, "find_ffmpeg", lambda: "/fake/ffmpeg")

    return seen


def test_the_runner_fetches_in_order_then_joins(tmp_path, fake_tools):
    run = runners.make_runners(Settings(api_key="k"))["merge"]
    steps = []

    result = run(
        {"sources": [{"url": A_FIXED}, {"url": B_FIXED}], "quality": "720p"},
        tmp_path,
        steps.append,
    )

    assert fake_tools["fetch"] == [(A_FIXED, "clip1"), (B_FIXED, "clip2")]
    assert [o.quality for o in fake_tools["options"]] == ["720p", "720p"]
    assert [o.format_type for o in fake_tools["options"]] == ["MP4", "MP4"]

    ffmpeg, clips, output, kwargs = fake_tools["merge"][0]
    assert ffmpeg == "/fake/ffmpeg"
    assert clips == [
        str(tmp_path / "clip1" / "clip.mp4"),
        str(tmp_path / "clip2" / "clip.mp4"),
    ]
    assert output == str(tmp_path / "merged.mp4")
    assert kwargs["re_encode"] is False

    assert result == tmp_path / "merged.mp4"
    assert result.read_bytes() == b"merged"

    # Only the finished file stays on disk.
    assert not (tmp_path / "clip1").exists()
    assert not (tmp_path / "clip2").exists()

    # Progress never goes backwards and leaves the last slice for the join.
    assert steps == sorted(steps)
    assert 0 < steps[0]
    assert steps[-1] == pytest.approx(runners.DOWNLOAD_SHARE)


def test_a_clip_that_fails_stops_the_merge_and_is_numbered(
    tmp_path, monkeypatch, fake_tools
):
    first = runners.fetch_clip

    def flaky(url, *args, **kwargs):
        if url == B_FIXED:
            raise DownloadError("ERROR: [youtube] x: This video is unavailable")

        return first(url, *args, **kwargs)

    monkeypatch.setattr(runners, "fetch_clip", flaky)

    store = JobStore(
        tmp_path / "w", runners.make_runners(Settings(api_key="k")), limits=Limits()
    )

    try:
        job = store.submit(
            "ann",
            "merge",
            {"sources": [{"url": A_FIXED}, {"url": B_FIXED}], "quality": "Best"},
        )
        job = wait_for(store, "ann", job.id)
    finally:
        store.shutdown()

    assert job.status == "failed"
    assert job.error["code"] == "unavailable"
    assert job.error["clip"] == 2
    assert fake_tools["merge"] == []


def test_a_basic_job_error_has_no_clip_number(tmp_path):
    def unavailable(params, folder, progress):
        raise DownloadError("ERROR: [youtube] x: This video is unavailable")

    store = JobStore(tmp_path / "w", {"basic": unavailable}, limits=Limits())

    try:
        job = store.submit("ann", "basic", {"url": A_FIXED})
        job = wait_for(store, "ann", job.id)
    finally:
        store.shutdown()

    assert job.status == "failed"
    assert "clip" not in job.error


# ------------------------------------------------------------ time limit


def test_a_merge_may_run_longer_than_a_basic_job(tmp_path):
    now = [1000.0]
    release = threading.Event()

    def slow(params, folder, progress):
        release.wait(5)
        out = folder / "out.mp4"
        out.write_bytes(b"x")

        return out

    store = JobStore(
        tmp_path / "w",
        {"basic": slow, "merge": slow},
        limits=Limits(job_timeout=100, merge_timeout=1000),
        clock=lambda: now[0],
    )

    try:
        basic = store.submit("ann", "basic", {"url": A_FIXED})
        merge = store.submit(
            "bob", "merge", {"sources": [{"url": A_FIXED}, {"url": B_FIXED}]}
        )
        wait_for(store, "ann", basic.id, wanted=("running",))
        wait_for(store, "bob", merge.id, wanted=("running",))

        now[0] += 500
        store.sweep()

        assert store.get("ann", basic.id).status == "failed"
        assert store.get("ann", basic.id).error["code"] == "timeout"
        assert store.get("bob", merge.id).status == "running"

        now[0] += 600
        store.sweep()

        assert store.get("bob", merge.id).status == "failed"
        assert store.get("bob", merge.id).error["code"] == "timeout"
    finally:
        release.set()
        store.shutdown()


def test_the_merge_limit_can_be_set_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("VT_API_KEY", "k" * 40)
    monkeypatch.setenv("VT_API_MERGE_TIMEOUT", "42")
    monkeypatch.chdir(tmp_path)

    assert Settings.from_env().limits.merge_timeout == 42


# ------------------------------------------------------------ web routes


def quick(params, folder, progress):
    progress(0.5)
    out = folder / "merged.mp4"
    out.write_bytes(b"joined")

    return out


def make_client(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from fastapi.testclient import TestClient

    from video_tool.api.app import create_app

    settings = Settings(api_key=KEY, work_dir=tmp_path / "work")

    return TestClient(create_app(settings, runners={"basic": quick, "merge": quick}))


def test_the_api_accepts_a_merge_and_serves_the_file(tmp_path):
    client = make_client(tmp_path)

    start = client.post(
        "/v1/jobs", json={"tool": "merge", "urls": [A, B]}, headers=headers()
    )
    assert start.status_code == 202
    assert start.json()["tool"] == "merge"

    job_id = start.json()["id"]
    data = {}

    for _ in range(100):
        data = client.get(f"/v1/jobs/{job_id}", headers=headers()).json()

        if data["status"] in ("done", "failed"):
            break

        time.sleep(0.05)

    assert data["status"] == "done"

    got = client.get(data["file_url"], headers=headers())
    assert got.status_code == 200 and got.content == b"joined"
    assert "merged.mp4" in got.headers["content-disposition"]


@pytest.mark.parametrize(
    "body",
    [
        {"tool": "merge", "urls": [A]},
        {"tool": "merge", "urls": [A] * (MAX_MERGE_CLIPS + 1)},
        {"tool": "merge", "urls": [A, "https://example.com/x"]},
        {"tool": "merge", "url": A},
        {"tool": "nope"},
    ],
)
def test_the_api_refuses_bad_merge_bodies_before_a_job_starts(tmp_path, body):
    client = make_client(tmp_path)

    reply = client.post("/v1/jobs", json=body, headers=headers())

    assert reply.status_code == 422


def test_the_openapi_description_lists_both_tools(tmp_path):
    client = make_client(tmp_path)

    text = client.get("/openapi.json").text

    assert "BasicJob" in text and "MergeJob" in text

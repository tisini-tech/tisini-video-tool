"""Limits and failure rules. A fake clock and fake runners, no network."""

import logging
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from video_tool.api.app import create_app  # noqa: E402
from video_tool.api.jobs import (  # noqa: E402
    JobStopped,
    JobStore,
    TooSoonError,
)
from video_tool.api.security import Limits, Settings  # noqa: E402
from video_tool.errors import DownloadError  # noqa: E402


def quick(params, folder, progress):
    out = folder / "clip.mp4"
    out.write_bytes(b"video")
    return out


def unavailable(params, folder, progress):
    (folder / "half.part").write_bytes(b"x")
    raise DownloadError("ERROR: [youtube] x: This video is unavailable")


def odd(params, folder, progress):
    raise ValueError("odd input")


def make(tmp_path, runner, **limits):
    now = [1000.0]
    store = JobStore(
        tmp_path / "w",
        {"basic": runner},
        limits=Limits(**limits),
        clock=lambda: now[0],
    )
    return store, now


def wait_done(store, user, job_id):
    for _ in range(200):
        job = store.get(user, job_id)
        if job and job.status in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not end")


def run_job(store, user="ann", **params):
    job = store.submit(user, "basic", params)
    return wait_done(store, user, job.id)


def test_a_failed_job_keeps_its_error_but_loses_its_folder(tmp_path):
    store, _ = make(tmp_path, unavailable)
    job = run_job(store, n=1)

    assert job.status == "failed"
    assert job.error["code"] == "unavailable"

    for _ in range(100):
        if not job.folder.exists():
            break
        time.sleep(0.02)

    assert not job.folder.exists()
    store.shutdown()


def test_start_up_removes_old_job_folders_and_nothing_else(tmp_path):
    work = tmp_path / "w"
    old = work / ("a" * 22)
    other = work / "keep-me"
    old.mkdir(parents=True)
    other.mkdir()

    JobStore(work, {"basic": quick}).shutdown()

    assert not old.exists()
    assert other.exists()


def test_hourly_limit_counts_every_attempt(tmp_path):
    store, now = make(tmp_path, quick, per_hour=2)
    run_job(store, n=1)
    run_job(store, n=2)

    with pytest.raises(TooSoonError) as caught:
        store.submit("ann", "basic", {"n": 3})

    assert caught.value.title == "Hourly limit reached"
    assert caught.value.retry_after > 0

    store.submit("bob", "basic", {"n": 3})  # another user is not blocked

    now[0] += 3601
    run_job(store, n=4)
    store.shutdown()


def test_three_failures_in_a_row_cause_a_pause(tmp_path):
    store, now = make(tmp_path, odd, fail_streak=3, fail_pause=300)

    for n in range(3):
        assert run_job(store, n=n).status == "failed"

    with pytest.raises(TooSoonError) as caught:
        store.submit("ann", "basic", {"n": 9})

    assert caught.value.title == "Too many failed jobs"
    assert "5 minutes" in caught.value.message

    now[0] += 301
    store.submit("ann", "basic", {"n": 10})
    store.shutdown()


def test_a_success_resets_the_failure_count(tmp_path):
    calls = []

    def sometimes(params, folder, progress):
        calls.append(1)
        if params["ok"]:
            return quick(params, folder, progress)
        raise ValueError("odd")

    store, _ = make(tmp_path, sometimes, fail_streak=3)

    for n, ok in enumerate([False, False, True, False, False]):
        assert run_job(store, n=n, ok=ok)

    store.submit("ann", "basic", {"n": 99, "ok": True})  # no pause
    store.shutdown()


def test_a_link_that_failed_for_a_fixed_reason_is_remembered(tmp_path):
    calls = []

    def counted(params, folder, progress):
        calls.append(1)
        return unavailable(params, folder, progress)

    store, now = make(tmp_path, counted)
    run_job(store, url="x")
    assert len(calls) == 1

    again = store.submit("ann", "basic", {"url": "x"})
    assert again.status == "failed"
    assert again.error["code"] == "unavailable"
    assert len(calls) == 1  # no second call

    now[0] += 301
    run_job(store, url="x")
    assert len(calls) == 2
    store.shutdown()


def test_a_job_over_its_time_limit_fails_and_frees_the_user(tmp_path):
    gate = threading.Event()
    stopped = []

    def slow(params, folder, progress):
        gate.wait(5)
        try:
            progress(0.5)
        except JobStopped:
            stopped.append(True)
            raise
        return quick(params, folder, progress)

    store, now = make(tmp_path, slow, job_timeout=100)
    job = store.submit("ann", "basic", {"n": 1})

    for _ in range(100):
        if job.status == "running":
            break
        time.sleep(0.02)

    now[0] += 101
    store.sweep()

    assert job.status == "failed"
    assert job.error["code"] == "timeout"

    store.submit("ann", "basic", {"n": 2})  # the user is free again

    gate.set()

    for _ in range(100):
        if stopped and not job.folder.exists():
            break
        time.sleep(0.02)

    assert stopped
    assert not job.folder.exists()
    store.shutdown()


def test_known_errors_log_one_line_and_unknown_ones_log_a_traceback(
    tmp_path, caplog
):
    def boom(params, folder, progress):
        raise RuntimeError("boom")

    caplog.set_level(logging.INFO, logger="video_tool.api.jobs")

    store, _ = make(tmp_path, unavailable)
    run_job(store, n=1)
    known = [r for r in caplog.records if "failed" in r.getMessage()]
    assert known and all(r.exc_info is None for r in known)
    store.shutdown()

    caplog.clear()

    store, _ = make(tmp_path, boom)
    run_job(store, n=2)
    unknown = [r for r in caplog.records if "failed" in r.getMessage()]
    assert any(r.exc_info for r in unknown)
    store.shutdown()


def test_the_api_answers_a_limit_with_429_and_retry_after(tmp_path):
    settings = Settings(
        api_key="k", work_dir=tmp_path / "w", limits=Limits(per_hour=1)
    )
    client = TestClient(
        create_app(settings, runners={"basic": quick}, janitor=False)
    )
    head = {"X-API-Key": "k", "X-User-Id": "ann"}
    body = {"tool": "basic", "url": "https://youtu.be/abcdefghijk"}

    first = client.post("/v1/jobs", json=body, headers=head).json()

    for _ in range(100):
        status = client.get(f"/v1/jobs/{first['id']}", headers=head).json()
        if status["status"] == "done":
            break
        time.sleep(0.02)

    second = client.post("/v1/jobs", json=body, headers=head)

    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0
    assert second.json()["detail"]["title"] == "Hourly limit reached"

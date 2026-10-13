"""Security tests: link check, key lockout, call limits, hardening."""

from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from video_tool.api.app import create_app
from video_tool.api.linkcheck import canonical_url
from video_tool.api.ratelimit import CallLimiter, KeyGuard
from video_tool.api.security import Limits, Settings, valid_user_id

KEY = "k" * 40
OTHER_KEY = "n" * 40
ID = "jNQXAC9IVRw"
CANON = f"https://www.youtube.com/watch?v={ID}"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def auth(user: str = "u1", key: str = KEY) -> dict:
    return {"X-API-Key": key, "X-User-Id": user}


def _quick_runner(params, folder, progress):
    target = folder / "video.mp4"
    target.write_bytes(b"x")

    return target


@contextmanager
def running(tmp_path, *, limits=None, next_key="", runners=None, **options):
    clock = Clock()
    settings = Settings(
        api_key=KEY,
        work_dir=tmp_path / "work",
        limits=limits or Limits(),
        api_key_next=next_key,
    )
    app = create_app(
        settings,
        runners or {"basic": _quick_runner},
        janitor=False,
        clock=clock,
        **options,
    )

    with TestClient(app) as client:
        yield client, clock


# ---------------------------------------------------------------- links


@pytest.mark.parametrize(
    "link",
    [
        f"https://youtu.be/{ID}",
        f"https://youtu.be/{ID}?si=abc",
        f"https://youtu.be/{ID}/",
        f"youtu.be/{ID}",
        f"https://www.youtube.com/watch?v={ID}",
        f"https://www.youtube.com/watch?v={ID}&list=PLabc&t=5s",
        f"https://m.youtube.com/watch?v={ID}",
        f"http://youtube.com/watch?v={ID}",
        f"https://www.youtube.com:443/watch?v={ID}",
        f"https://www.youtube.com/shorts/{ID}",
        f"https://www.youtube.com/live/{ID}?feature=share",
        f"https://www.youtube.com/embed/{ID}",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={ID}",
        f'"https://youtu.be/{ID}"',
        f"  https://youtu.be/{ID}  ",
    ],
)
def test_good_links_become_one_standard_link(link):
    assert canonical_url(link) == CANON


@pytest.mark.parametrize(
    "link",
    [
        "",
        "   ",
        f"https://evil.com/watch?v={ID}",
        f"https://youtube.com.evil.com/watch?v={ID}",
        f"https://evil.com/youtube.com/watch?v={ID}",
        f"https://youtube.com@evil.com/watch?v={ID}",
        f"https://user:pw@www.youtube.com/watch?v={ID}",
        f"https://www.youtube.com:8443/watch?v={ID}",
        f"https://music.youtube.com/watch?v={ID}",
        f"ftp://youtu.be/{ID}",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://www.youtube.com/watch?v=short",
        f"https://www.youtube.com/watch?v={ID}1",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/playlist?list=PLabc",
        "https://www.youtube.com/@channel",
        "https://youtu.be/",
        f"https://youtu.be/{ID}/extra",
        f"https://www.youtube.com/watch?v={ID}\nhttps://evil.com",
        "https://youtu.be/" + "a" * 3000,
    ],
)
def test_bad_links_are_refused(link):
    with pytest.raises(ValueError):
        canonical_url(link)


def test_user_id_rules():
    assert valid_user_id("abc.DEF_1@x-y")
    assert not valid_user_id("")
    assert not valid_user_id("a" * 65)
    assert not valid_user_id("abc\n")
    assert not valid_user_id("a b")


# ------------------------------------------------------------ the limits


def test_call_limiter_blocks_then_recovers():
    clock = Clock()
    limiter = CallLimiter(3, 10, clock=clock)

    assert [limiter.check("a") for _ in range(3)] == [0, 0, 0]
    assert limiter.check("a") == 10
    assert limiter.check("b") == 0  # another key is not affected

    clock.advance(4)
    assert limiter.check("a") == 6

    clock.advance(6)
    assert limiter.check("a") == 0


def test_call_limiter_forgets_old_keys():
    clock = Clock()
    limiter = CallLimiter(1, 10, clock=clock, max_keys=5)

    for n in range(50):
        limiter.check(f"user{n}")

    assert len(limiter._hits) <= 5


def test_key_guard_blocks_after_repeated_failures():
    clock = Clock()
    guard = KeyGuard(3, 60, 100, clock=clock)

    guard.fail("1.2.3.4")
    guard.fail("1.2.3.4")
    assert guard.blocked_for("1.2.3.4") == 0

    guard.fail("1.2.3.4")
    assert guard.blocked_for("1.2.3.4") == 100
    assert guard.blocked_for("5.6.7.8") == 0

    clock.advance(40)
    assert guard.blocked_for("1.2.3.4") == 60

    clock.advance(61)
    assert guard.blocked_for("1.2.3.4") == 0


def test_key_guard_ignores_old_failures():
    clock = Clock()
    guard = KeyGuard(3, 60, 100, clock=clock)

    guard.fail("a")
    clock.advance(61)
    guard.fail("a")
    guard.fail("a")

    assert guard.blocked_for("a") == 0


def test_key_guard_stays_small():
    clock = Clock()
    guard = KeyGuard(5, 60, 100, clock=clock, max_keys=20)

    for n in range(200):
        guard.fail(f"10.0.0.{n}")

    assert len(guard._fails) + len(guard._blocked) <= 20


# ------------------------------------------------------------ the routes


def test_five_wrong_keys_block_the_address(tmp_path):
    with running(tmp_path) as (client, clock):
        wrong = auth(key="w" * 40)

        for _ in range(5):
            assert client.get("/v1/jobs/abc", headers=wrong).status_code == 401

        blocked = client.get("/v1/jobs/abc", headers=auth())

        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0

        clock.advance(901)

        assert client.get("/v1/jobs/abc", headers=auth()).status_code == 404


def test_next_key_works_beside_the_current_key(tmp_path):
    with running(tmp_path, next_key=OTHER_KEY) as (client, _):
        assert client.get("/v1/jobs/abc", headers=auth()).status_code == 404
        assert client.get("/v1/jobs/abc", headers=auth(key=OTHER_KEY)).status_code == 404
        assert client.get("/v1/jobs/abc", headers=auth(key="x" * 40)).status_code == 401


def test_missing_or_bad_user_id(tmp_path):
    with running(tmp_path) as (client, _):
        assert client.get("/v1/jobs/abc", headers={"X-API-Key": KEY}).status_code == 400
        assert client.get("/v1/jobs/abc", headers=auth(user="a b")).status_code == 400


def test_user_call_limit(tmp_path):
    with running(tmp_path, limits=Limits(calls_per_min=3)) as (client, clock):
        codes = [
            client.get("/v1/jobs/abc", headers=auth()).status_code
            for _ in range(4)
        ]

        assert codes == [404, 404, 404, 429]

        limited = client.get("/v1/jobs/abc", headers=auth())
        assert int(limited.headers["Retry-After"]) > 0

        # another user is not affected
        assert client.get("/v1/jobs/abc", headers=auth(user="u2")).status_code == 404

        clock.advance(61)

        assert client.get("/v1/jobs/abc", headers=auth()).status_code == 404


def test_health_hides_details_without_the_key(tmp_path):
    with running(tmp_path) as (client, _):
        assert client.get("/v1/health").json() == {"status": "ok"}

        keyed = client.get("/v1/health", headers={"X-API-Key": KEY}).json()

        assert keyed["status"] == "ok"
        assert "ffmpeg" in keyed


def test_replies_carry_safe_headers(tmp_path):
    with running(tmp_path) as (client, _):
        reply = client.get("/v1/health")

        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["x-content-type-options"] == "nosniff"
        assert reply.headers["referrer-policy"] == "no-referrer"


def test_big_request_body_is_refused(tmp_path):
    with running(tmp_path) as (client, _):
        reply = client.post(
            "/v1/jobs",
            content=b"x" * 5000,
            headers={**auth(), "Content-Type": "application/json"},
        )

        assert reply.status_code == 413


def test_docs_can_be_switched_off(tmp_path):
    with running(tmp_path, docs=False) as (client, _):
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/", follow_redirects=False).status_code == 404


def test_docs_can_be_switched_on(tmp_path):
    with running(tmp_path, docs=True) as (client, _):
        assert client.get("/docs").status_code == 200
        assert client.get("/", follow_redirects=False).status_code in (302, 307)


# ------------------------------------------------------- the job request


def test_job_runs_with_the_standard_link(tmp_path):
    seen = []
    done = threading.Event()

    def runner(params, folder, progress):
        seen.append(params)
        done.set()

        return _quick_runner(params, folder, progress)

    with running(tmp_path, runners={"basic": runner}) as (client, _):
        reply = client.post(
            "/v1/jobs",
            json={"tool": "basic", "url": f"https://youtu.be/{ID}?si=x", "quality": "720p"},
            headers=auth(),
        )

        assert reply.status_code == 202
        assert done.wait(5)

    assert seen == [{"url": CANON, "format": "MP4", "quality": "720p"}]


@pytest.mark.parametrize(
    "body",
    [
        {"tool": "basic", "url": "https://evil.com/watch?v=" + ID},
        {"tool": "basic", "url": f"https://youtu.be/{ID}", "quality": "9999p"},
        {"tool": "basic", "url": f"https://youtu.be/{ID}", "quality": "Best; rm -rf"},
        {"tool": "basic", "url": f"https://youtu.be/{ID}", "path": "/etc/passwd"},
        {"tool": "basic", "url": f"https://youtu.be/{ID}", "format": "AVI"},
        {"tool": "pro", "url": f"https://youtu.be/{ID}"},
    ],
)
def test_bad_job_requests_are_refused(tmp_path, body):
    with running(tmp_path) as (client, _):
        reply = client.post("/v1/jobs", json=body, headers=auth(user="u9"))

        assert reply.status_code == 422

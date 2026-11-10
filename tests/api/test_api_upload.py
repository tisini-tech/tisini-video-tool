"""Upload and local-file merge tests. No network.

Needs only pydantic and starlette, except the group at the end that goes
through the web routes (it skips itself without fastapi). The tests that
make real video files skip themselves without ffmpeg and ffprobe.
"""

import asyncio
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from video_tool.api import runners, uploads
from video_tool.api.hardening import Hardening
from video_tool.api.jobs import (
    JobStore,
    UnknownUploadError,
    UploadLimitError,
)
from video_tool.api.schemas import MAX_MERGE_CLIPS, JobRequest
from video_tool.api.security import Limits, Settings
from video_tool.api.uploads import (
    UploadRejected,
    check_video,
    looks_like_video,
    save_stream,
)

KEY = "test-key"
A = "https://youtu.be/abcdefghijk"
A_FIXED = "https://www.youtube.com/watch?v=abcdefghijk"
UP1 = "A" * 22
UP2 = "B" * 22

request = TypeAdapter(JobRequest)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg and ffprobe are needed"
)


def headers(user="ann"):
    return {"X-API-Key": KEY, "X-User-Id": user}


# ------------------------------------------------------------ real videos


def make_video(path, *, size="160x120", seconds=1, audio=True):
    command = [
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}",
    ]

    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]

    command += ["-c:v", "mpeg4"]
    command += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    subprocess.run(command + [str(path)], check=True, timeout=60)

    return path


def duration(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()

    return float(out)


@pytest.fixture(scope="session")
def videos(tmp_path_factory):
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg and ffprobe are needed")

    base = tmp_path_factory.mktemp("videos")
    sound = base / "sound.m4a"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=1", "-c:a", "aac", str(sound)],
        check=True, timeout=60,
    )

    return {
        "mp4": make_video(base / "one.mp4"),
        "mkv": make_video(base / "two.mkv"),
        "big": make_video(base / "big.mp4", size="320x240"),
        "sound": sound,
    }


# ------------------------------------------------------------ request shape


def test_sources_may_mix_links_and_uploads_and_keep_their_order():
    body = request.validate_python(
        {"tool": "merge", "sources": [{"upload": UP1}, {"url": A}, {"upload": UP2}]}
    )

    assert body.params() == {
        "sources": [{"upload": UP1}, {"url": A_FIXED}, {"upload": UP2}],
        "quality": "Best",
    }


def test_two_uploads_alone_make_a_merge():
    body = request.validate_python(
        {"tool": "merge", "sources": [{"upload": UP1}, {"upload": UP2}]}
    )

    assert [s.upload for s in body.sources] == [UP1, UP2]


@pytest.mark.parametrize(
    "sources",
    [
        [{"upload": UP1}],
        [{"upload": UP1}] * (MAX_MERGE_CLIPS + 1),
        [],
        [{"upload": UP1}, {"upload": "short"}],
        [{"upload": UP1}, {"upload": "../" * 8 + "etc/passwd"}],
        [{"upload": UP1}, {"upload": "a/b" + "c" * 19}],
        [{"upload": UP1}, {"upload": UP2, "path": "/etc/passwd"}],
        [{"upload": UP1}, {"upload": UP2, "url": A}],
        [{"upload": UP1}, {"path": "/etc/passwd"}],
        [{"upload": UP1}, {}],
        [{"upload": UP1}, {"url": "https://example.com/x"}],
        [{"upload": UP1}, "not an object"],
        {"upload": UP1},
    ],
)
def test_bad_sources_are_refused(sources):
    with pytest.raises(ValidationError):
        request.validate_python({"tool": "merge", "sources": sources})


# ------------------------------------------------------------ file checks


def test_known_containers_are_recognised():
    ts = bytearray(600)
    for spot in (0, 188, 376):
        ts[spot] = 0x47

    m2ts = bytearray(600)
    for spot in (4, 196, 388):
        m2ts[spot] = 0x47

    for head in (
        b"\x00\x00\x00\x18ftypmp42" + bytes(100),
        b"\x00\x00\x00\x14ftypqt  " + bytes(100),
        b"\x00\x00\x00\x08wide" + bytes(100),
        b"\x1a\x45\xdf\xa3" + bytes(100),
        b"RIFF\x00\x00\x00\x00AVI LIST" + bytes(100),
        b"FLV\x01\x05" + bytes(100),
        b"\x00\x00\x01\xba" + bytes(100),
        bytes(ts),
        bytes(m2ts),
    ):
        assert looks_like_video(head), head[:12]


def test_playlists_scripts_and_other_files_are_not_video():
    for head in (
        b"",
        b"short",
        b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:1,\nfile:///etc/passwd\n",
        b"ffconcat version 1.0\nfile '/etc/passwd'\n" + b" " * 100,
        b"\x89PNG\r\n\x1a\n" + bytes(100),
        b"PK\x03\x04" + bytes(100),
        b"<?xml version='1.0'?><MPD></MPD>" + b" " * 50,
        b"\x47" + bytes(300),  # one sync byte is not a stream
    ):
        assert not looks_like_video(head), head[:20]


@needs_ffmpeg
def test_real_videos_pass_the_check(videos):
    check_video(videos["mp4"])
    check_video(videos["mkv"])


@needs_ffmpeg
def test_a_file_with_a_video_header_but_no_video_in_it_is_refused(tmp_path, videos):
    fake = tmp_path / "fake.bin"
    fake.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"junk" * 200)

    with pytest.raises(UploadRejected) as caught:
        check_video(fake)

    assert caught.value.status == 415


@needs_ffmpeg
def test_sound_only_is_refused(videos):
    with pytest.raises(UploadRejected) as caught:
        check_video(videos["sound"])

    assert caught.value.status == 415


def test_a_playlist_is_refused_before_ffprobe_runs(tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("ffprobe must not open an unchecked file")

    monkeypatch.setattr(uploads.subprocess, "run", never)
    playlist = tmp_path / "list.bin"
    playlist.write_bytes(b"#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n")

    with pytest.raises(UploadRejected) as caught:
        check_video(playlist)

    assert caught.value.status == 415


# ------------------------------------------------------------ streaming


async def pieces(*parts):
    for part in parts:
        yield part


def test_a_stream_is_written_to_disk(tmp_path):
    out = tmp_path / "x" / "file.bin"
    size = asyncio.run(save_stream(pieces(b"ab", b"", b"cde"), out, 100))

    assert size == 5 and out.read_bytes() == b"abcde"


def test_a_stream_over_the_limit_stops_early(tmp_path):
    taken = []

    async def endless():
        for _ in range(1000):
            taken.append(1)
            yield b"x" * 10

    with pytest.raises(UploadRejected) as caught:
        asyncio.run(save_stream(endless(), tmp_path / "f.bin", 35))

    assert caught.value.status == 413
    assert len(taken) <= 5  # it did not read the rest


def test_an_empty_stream_is_refused(tmp_path):
    with pytest.raises(UploadRejected) as caught:
        asyncio.run(save_stream(pieces(), tmp_path / "f.bin", 100))

    assert caught.value.status == 422


# ------------------------------------------------------------ body limits


def call_hardening(path, length, bigger=None):
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "path": path,
        "headers": [(b"content-length", str(length).encode())],
    }
    asyncio.run(Hardening(inner, bigger=bigger)(scope, receive, send))

    return sent[0]


def test_only_the_upload_route_may_take_a_big_body():
    bigger = {"/v1/uploads": 1000}

    assert call_hardening("/v1/jobs", 4096, bigger)["status"] == 200
    assert call_hardening("/v1/jobs", 4097, bigger)["status"] == 413
    assert call_hardening("/v1/uploads", 1000, bigger)["status"] == 200
    assert call_hardening("/v1/uploads", 1001, bigger)["status"] == 413
    assert call_hardening("/v1/uploads/other", 5000, bigger)["status"] == 413
    assert call_hardening("/v1/uploads", 5000)["status"] == 413


def test_replies_keep_their_safety_headers():
    start = call_hardening("/v1/uploads", 10, {"/v1/uploads": 1000})
    names = {name for name, _ in start["headers"]}

    assert {b"cache-control", b"x-content-type-options"} <= names


# ------------------------------------------------------------ job store


def make_store(tmp_path, runner=None, **limits):
    now = [1000.0]
    seen = []

    def default(params, folder, progress):
        seen.append(params)
        out = folder / "merged.mp4"
        out.write_bytes(b"merged")

        return out

    store = JobStore(
        tmp_path / "w",
        {"merge": runner or default},
        limits=Limits(**limits),
        clock=lambda: now[0],
    )

    return store, now, seen


def add_upload(store, user="ann", data=b"v" * 10):
    upload = store.start_upload(user)
    upload.file.write_bytes(data)
    store.finish_upload(upload, len(data))

    return upload


def wait_for(store, user, job_id, wanted=("done", "failed")):
    for _ in range(300):
        job = store.get(user, job_id)

        if job and job.status in wanted:
            return job

        time.sleep(0.02)

    raise AssertionError("job did not reach " + "/".join(wanted))


def gone(folder):
    for _ in range(100):
        if not folder.exists():
            return True

        time.sleep(0.02)

    return False


def test_a_user_may_hold_only_so_many_uploads(tmp_path):
    store, _, _ = make_store(tmp_path, max_uploads=2)

    try:
        add_upload(store)
        second = add_upload(store)

        with pytest.raises(UploadLimitError):
            store.start_upload("ann")

        # Another user is not affected, and a freed place can be used again.
        add_upload(store, "bob")
        store.abort_upload(second)
        add_upload(store)
    finally:
        store.shutdown()


def test_upload_space_is_reserved_while_a_file_arrives(tmp_path):
    store, _, _ = make_store(tmp_path, max_upload_mb=1, upload_quota_mb=1)

    try:
        arriving = store.start_upload("ann")
        assert arriving.limit == 1024**2

        with pytest.raises(UploadLimitError):
            store.start_upload("ann")  # all of the space is promised

        arriving.file.write_bytes(b"x" * 100)
        store.finish_upload(arriving, 100)

        again = store.start_upload("ann")
        assert 0 < again.limit <= 1024**2 - 100
    finally:
        store.shutdown()


def test_a_job_gets_the_path_of_an_upload_and_the_upload_goes_after(tmp_path):
    store, _, seen = make_store(tmp_path)

    try:
        upload = add_upload(store)
        job = store.submit(
            "ann", "merge",
            {"sources": [{"upload": upload.id}, {"url": A_FIXED}], "quality": "Best"},
        )
        job = wait_for(store, "ann", job.id)

        assert job.status == "done"
        assert seen[0]["sources"] == [{"path": str(upload.file)}, {"url": A_FIXED}]
        assert seen[0]["quality"] == "Best"
        assert gone(upload.folder)
        assert store.drop_upload("ann", upload.id) == "missing"
        assert job.file.read_bytes() == b"merged"
    finally:
        store.shutdown()


def test_the_upload_goes_when_the_job_fails_too(tmp_path):
    def boom(params, folder, progress):
        raise ValueError("odd input")

    store, _, _ = make_store(tmp_path, boom)

    try:
        upload = add_upload(store)
        job = store.submit("ann", "merge", {"sources": [{"upload": upload.id}]})
        job = wait_for(store, "ann", job.id)

        assert job.status == "failed"
        assert gone(upload.folder)
    finally:
        store.shutdown()


@pytest.mark.parametrize("who", ["ann", "bob"])
def test_only_the_owner_may_use_a_ready_upload(tmp_path, who):
    store, _, seen = make_store(tmp_path)

    try:
        upload = add_upload(store, "ann")
        sources = [{"url": A_FIXED}, {"upload": upload.id}]

        if who == "ann":
            job = store.submit(who, "merge", {"sources": sources})
            assert wait_for(store, who, job.id).status == "done"
        else:
            with pytest.raises(UnknownUploadError) as caught:
                store.submit(who, "merge", {"sources": sources})

            assert caught.value.position == 2
            assert (upload.folder / "upload.bin").exists()  # left alone
    finally:
        store.shutdown()


def test_unknown_and_unfinished_uploads_are_refused(tmp_path):
    store, _, _ = make_store(tmp_path)

    try:
        arriving = store.start_upload("ann")

        for upload_id in (UP1, arriving.id):
            with pytest.raises(UnknownUploadError) as caught:
                store.submit("ann", "merge", {"sources": [{"upload": upload_id}]})

            assert caught.value.position == 1
    finally:
        store.shutdown()


def test_a_refused_upload_costs_no_attempt(tmp_path):
    store, _, _ = make_store(tmp_path, per_hour=1)

    try:
        with pytest.raises(UnknownUploadError):
            store.submit("ann", "merge", {"sources": [{"upload": UP1}]})

        upload = add_upload(store)
        job = store.submit("ann", "merge", {"sources": [{"upload": upload.id}]})

        assert wait_for(store, "ann", job.id).status == "done"
    finally:
        store.shutdown()


def test_an_upload_can_be_deleted_until_a_job_takes_it(tmp_path):
    release = threading.Event()

    def held(params, folder, progress):
        release.wait(5)
        out = folder / "merged.mp4"
        out.write_bytes(b"m")

        return out

    store, _, _ = make_store(tmp_path, held)

    try:
        arriving = store.start_upload("ann")
        assert store.drop_upload("ann", arriving.id) == "busy"
        store.abort_upload(arriving)

        mine = add_upload(store, "ann")
        assert store.drop_upload("bob", mine.id) == "missing"
        assert store.drop_upload("ann", mine.id) == "deleted"
        assert gone(mine.folder)

        used = add_upload(store, "ann")
        job = store.submit("ann", "merge", {"sources": [{"upload": used.id}]})
        wait_for(store, "ann", job.id, wanted=("running",))
        assert store.drop_upload("ann", used.id) == "busy"
    finally:
        release.set()
        store.shutdown()


def test_an_unused_upload_expires_but_one_in_use_does_not(tmp_path):
    release = threading.Event()

    def held(params, folder, progress):
        release.wait(5)
        out = folder / "merged.mp4"
        out.write_bytes(b"m")

        return out

    store, now, _ = make_store(tmp_path, held, merge_timeout=100000)

    try:
        idle = add_upload(store, "ann")
        used = add_upload(store, "bob")
        job = store.submit("bob", "merge", {"sources": [{"upload": used.id}]})
        wait_for(store, "bob", job.id, wanted=("running",))

        now[0] += 3601
        store.sweep()

        assert gone(idle.folder)
        assert store.drop_upload("ann", idle.id) == "missing"
        assert used.folder.exists()
    finally:
        release.set()
        store.shutdown()


def test_a_job_that_runs_too_long_frees_its_uploads(tmp_path):
    release = threading.Event()

    def held(params, folder, progress):
        release.wait(5)
        out = folder / "merged.mp4"
        out.write_bytes(b"m")

        return out

    store, now, _ = make_store(tmp_path, held, merge_timeout=100)

    try:
        upload = add_upload(store)
        job = store.submit("ann", "merge", {"sources": [{"upload": upload.id}]})
        wait_for(store, "ann", job.id, wanted=("running",))

        now[0] += 500
        store.sweep()

        assert store.get("ann", job.id).error["code"] == "timeout"
        assert gone(upload.folder)
    finally:
        release.set()
        store.shutdown()


def test_old_uploads_are_removed_when_the_server_starts(tmp_path):
    leftover = tmp_path / "w" / "uploads" / ("Z" * 22)
    leftover.mkdir(parents=True)
    (leftover / "upload.bin").write_bytes(b"old")

    store, _, _ = make_store(tmp_path)

    try:
        assert not leftover.exists()
    finally:
        store.shutdown()


# ------------------------------------------------------------ real merges


def run_merge(tmp_path, sources, monkeypatch=None):
    run = runners.make_runners(Settings(api_key="k"))["merge"]
    steps = []
    out = run({"sources": sources, "quality": "Best"}, tmp_path / "job", steps.append)

    return out, steps


@needs_ffmpeg
def test_two_local_files_with_the_same_settings_join_without_re_encoding(
    tmp_path, videos
):
    first = shutil.copy(videos["mp4"], tmp_path / "a.bin")
    second = shutil.copy(videos["mp4"], tmp_path / "b.bin")

    out, steps = run_merge(tmp_path, [{"path": first}, {"path": second}])

    assert out.name == "merged.mp4" and out.is_file()
    assert duration(out) == pytest.approx(2 * duration(videos["mp4"]), abs=0.3)
    assert steps == sorted(steps)
    assert steps[-1] == pytest.approx(runners.DOWNLOAD_SHARE)
    assert Path(first).is_file() and Path(second).is_file()  # the store deletes them


@needs_ffmpeg
def test_local_files_of_different_kinds_and_sizes_are_joined(tmp_path, videos):
    out, _ = run_merge(
        tmp_path, [{"path": str(videos["mp4"])}, {"path": str(videos["big"])},
                   {"path": str(videos["mkv"])}]
    )
    expected = sum(duration(videos[k]) for k in ("mp4", "big", "mkv"))

    assert duration(out) == pytest.approx(expected, abs=0.5)


@needs_ffmpeg
def test_an_upload_and_a_link_can_be_merged_in_that_order(tmp_path, videos, monkeypatch):
    fetched = []

    def fetch(url, folder, start, end, options, **kwargs):
        fetched.append(url)
        target = Path(folder) / "clip.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(videos["mp4"], target)

        return target, False

    monkeypatch.setattr(runners, "fetch_clip", fetch)

    out, _ = run_merge(
        tmp_path, [{"path": str(videos["mkv"])}, {"url": A_FIXED}]
    )

    assert fetched == [A_FIXED]
    assert duration(out) == pytest.approx(
        duration(videos["mkv"]) + duration(videos["mp4"]), abs=0.5
    )
    assert not (tmp_path / "job" / "clip2").exists()  # downloads are cleaned up


# ------------------------------------------------------------ web routes


def quick(params, folder, progress):
    progress(0.5)
    out = folder / "merged.mp4"
    out.write_bytes(b"joined")

    return out


def make_client(tmp_path, **limits):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from fastapi.testclient import TestClient

    from video_tool.api.app import create_app

    settings = Settings(
        api_key=KEY, work_dir=tmp_path / "work", limits=Limits(**limits)
    )

    return TestClient(create_app(settings, runners={"basic": quick, "merge": quick}))


def send(client, data, user="ann", **extra):
    return client.post(
        "/v1/uploads",
        content=data,
        headers={**headers(user), "Content-Type": "application/octet-stream", **extra},
    )


def video_bytes(videos):
    return Path(videos["mp4"]).read_bytes()


def test_an_uploaded_video_can_be_merged_and_is_then_gone(tmp_path, videos):
    client = make_client(tmp_path)

    first = send(client, video_bytes(videos))
    assert first.status_code == 201
    assert first.json()["size"] == len(video_bytes(videos))
    assert first.json()["expires_in"] == 3600
    assert set(first.json()) == {"id", "size", "expires_in"}

    second = send(client, video_bytes(videos))
    ids = [first.json()["id"], second.json()["id"]]

    start = client.post(
        "/v1/jobs",
        json={"tool": "merge", "sources": [{"upload": i} for i in ids]},
        headers=headers(),
    )
    assert start.status_code == 202

    data = {}

    for _ in range(100):
        data = client.get(f"/v1/jobs/{start.json()['id']}", headers=headers()).json()

        if data["status"] in ("done", "failed"):
            break

        time.sleep(0.05)

    assert data["status"] == "done"
    assert client.get(data["file_url"], headers=headers()).content == b"joined"
    assert client.delete(f"/v1/uploads/{ids[0]}", headers=headers()).status_code == 404


def test_things_that_are_not_videos_are_refused(tmp_path):
    client = make_client(tmp_path)

    text = send(client, b"#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n")
    empty = send(client, b"")

    assert text.status_code == 415 and text.json()["detail"]["title"]
    assert empty.status_code == 422

    # A refused upload leaves nothing behind and takes no place.
    folder = tmp_path / "work" / "uploads"
    assert not folder.exists() or not any(folder.iterdir())


def test_a_file_over_the_limit_is_refused_by_its_declared_size(tmp_path):
    client = make_client(tmp_path, max_upload_mb=1)

    reply = send(client, b"\x00" * (2 * 1024**2))

    assert reply.status_code == 413


def test_the_other_routes_keep_the_small_body_limit(tmp_path):
    client = make_client(tmp_path, max_upload_mb=1)

    reply = client.post(
        "/v1/jobs", content=b"x" * 5000,
        headers={**headers(), "Content-Type": "application/json"},
    )

    assert reply.status_code == 413


def test_uploads_need_the_key_and_a_user(tmp_path):
    client = make_client(tmp_path)

    assert client.post("/v1/uploads", content=b"x").status_code == 401
    no_user = client.post(
        "/v1/uploads", content=b"x", headers={"X-API-Key": KEY}
    )
    assert no_user.status_code == 400


def test_a_job_naming_a_missing_or_foreign_upload_is_refused(tmp_path, videos):
    client = make_client(tmp_path)
    mine = send(client, video_bytes(videos)).json()["id"]

    for user, upload_id in (("ann", UP1), ("bob", mine)):
        reply = client.post(
            "/v1/jobs",
            json={"tool": "merge", "sources": [{"url": A}, {"upload": upload_id}]},
            headers=headers(user),
        )

        assert reply.status_code == 404
        assert reply.json()["detail"]["source"] == 2

    bad = client.post(
        "/v1/jobs",
        json={"tool": "merge", "sources": [{"url": A}, {"upload": "short"}]},
        headers=headers(),
    )
    assert bad.status_code == 422


def test_an_upload_can_be_deleted_by_its_owner_only(tmp_path, videos):
    client = make_client(tmp_path)
    mine = send(client, video_bytes(videos)).json()["id"]

    assert client.delete(f"/v1/uploads/{mine}", headers=headers("bob")).status_code == 404
    assert client.delete(f"/v1/uploads/{mine}", headers=headers()).status_code == 200
    assert client.delete(f"/v1/uploads/{mine}", headers=headers()).status_code == 404


def test_too_many_uploads_are_refused(tmp_path, videos):
    client = make_client(tmp_path, max_uploads=1)

    assert send(client, video_bytes(videos)).status_code == 201
    assert send(client, video_bytes(videos)).status_code == 429


def test_the_docs_show_a_file_picker_for_uploads(tmp_path):
    client = make_client(tmp_path)

    spec = client.get("/openapi.json").json()
    body = spec["paths"]["/v1/uploads"]["post"]["requestBody"]
    schema = body["content"]["application/octet-stream"]["schema"]

    assert schema == {"type": "string", "format": "binary"}

#!/usr/bin/env python3
"""A pretend video API, for trying the interface without ffmpeg or YouTube.

It answers the same calls as the real API (uploads, jobs, the finished
file) with the same replies and error shapes, but joins nothing. Use it to
work on the page and to see the slow and failing cases on purpose.

    python fake_api.py                      # listens on 127.0.0.1:8000
    VT_API_URL=http://127.0.0.1:8000 python server.py

Things you can trigger:
  - a YouTube link containing the word "private" makes that source fail
  - a file that is not a video (for example a .txt file) is refused
  - FAKE_JOIN_SECONDS, FAKE_FETCH_SECONDS and FAKE_UPLOAD_MS_PER_MB set
    how long each part takes (defaults: 8, 3 per link, 20)

The key is the one in FAKE_KEY (default below). Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = os.environ.get("FAKE_KEY", "demo-key-demo-key-demo-key-demo-key")
JOIN_SECONDS = float(os.environ.get("FAKE_JOIN_SECONDS", "8"))
FETCH_SECONDS = float(os.environ.get("FAKE_FETCH_SECONDS", "3"))
UPLOAD_MS_PER_MB = float(os.environ.get("FAKE_UPLOAD_MS_PER_MB", "20"))
MAX_UPLOAD = 5 * 1024**3
MAX_UPLOADS = 6
FILE_SIZE = 3 * 1024 * 1024

USER = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
ID = re.compile(r"[A-Za-z0-9_-]{22}")

lock = threading.Lock()
uploads: dict[str, dict] = {}
jobs: dict[str, dict] = {}


def sniff(head: bytes) -> bool:
    return len(head) >= 12 and (
        head[4:8] in (b"ftyp", b"moov", b"mdat")
        or head[:4] == b"\x1a\x45\xdf\xa3"
    )


def run_basic(job: dict, link: str) -> None:
    time.sleep(0.6)
    job["status"] = "running"
    steps = 20

    for step in range(steps):
        time.sleep(FETCH_SECONDS * 2 / steps)
        job["progress"] = round((step + 1) / steps * 0.95, 3)

    if "private" in link:
        job["error"] = {
            "level": "error",
            "title": "This video is private",
            "message": "YouTube does not let anyone watch this video "
            "without signing in.",
            "hint": "Ask the owner to make it public or unlisted, then try again.",
            "code": "private",
        }
        job["status"] = "failed"
        return

    job["progress"] = 1.0
    job["status"] = "done"
    job["finished"] = time.time()


def run_job(job: dict, sources: list[dict]) -> None:
    def set_progress(value: float) -> None:
        job["progress"] = round(value, 3)

    time.sleep(0.6)
    job["status"] = "running"
    links = [i for i, s in enumerate(sources) if "url" in s]
    count = len(sources)

    for position, source in enumerate(sources):
        if "url" in source:
            steps = 10

            for step in range(steps):
                time.sleep(FETCH_SECONDS / steps)
                set_progress(0.9 * (position + (step + 1) / steps) / count)

            if "private" in source["url"]:
                job["error"] = {
                    "level": "error",
                    "title": "This video is private",
                    "message": "YouTube does not let anyone watch this video "
                    "without signing in.",
                    "hint": "Ask the owner to make it public or unlisted, "
                    "then try again.",
                    "code": "private",
                    "clip": position + 1,
                }
                job["status"] = "failed"
                return
        else:
            set_progress(0.9 * (position + 1) / count)

    del links
    set_progress(0.9)
    time.sleep(JOIN_SECONDS)
    job["progress"] = 1.0
    job["status"] = "done"
    job["finished"] = time.time()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeApi"

    def log_message(self, fmt, *args):
        sys.stderr.write("[fake api] " + (fmt % args) + "\n")

    # ------------------------------------------------------------ helpers

    def send_json(self, status, data, close=False):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(raw)

    def fail(self, status, title, message, close=False, **extra):
        self.send_json(
            status, {"detail": {"title": title, "message": message, **extra}}, close
        )

    def who(self):
        if self.headers.get("X-API-Key", "") != KEY:
            self.fail(401, "Not allowed", "The API key is missing or wrong.", True)
            return None

        user = self.headers.get("X-User-Id", "")

        if not USER.fullmatch(user):
            self.fail(400, "Bad user ID", "Send X-User-Id with 1 to 64 letters, digits or . _ @ -", True)
            return None

        return user

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)

        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    # ------------------------------------------------------------ routes

    def do_GET(self):
        if self.path == "/v1/health":
            return self.send_json(200, {"status": "ok", "fake": True})

        user = self.who()

        if user is None:
            return

        match = re.fullmatch(r"/v1/jobs/([A-Za-z0-9_-]+)(/file)?", self.path)

        if not match:
            return self.fail(404, "Not found", "No such page.")

        job = jobs.get(match.group(1))

        if job is None or job["user"] != user:
            return self.fail(404, "Job not found", "There is no such job, or it has expired.")

        if match.group(2):
            if job["status"] != "done":
                return self.fail(409, "No file yet", "The job has not produced a file.")

            if job["tool"] == "basic":
                audio = job["format"] == "MP3"
                kind = "audio/mpeg" if audio else "video/mp4"
                name = "Demo video.mp3" if audio else "Demo video.mp4"
            else:
                kind, name = "video/mp4", "merged.mp4"

            data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * (FILE_SIZE - 12)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            self.wfile.write(data)
            return

        view = {
            "id": job["id"],
            "tool": job["tool"],
            "status": job["status"],
            "progress": job["progress"],
        }

        if job.get("error"):
            view["error"] = job["error"]

        if job["status"] == "done":
            view["file_url"] = f"/v1/jobs/{job['id']}/file"

        self.send_json(200, view)

    def do_DELETE(self):
        user = self.who()

        if user is None:
            return

        match = re.fullmatch(r"/v1/uploads/([A-Za-z0-9_-]+)", self.path)
        found = match and uploads.get(match.group(1))

        if not found or found["user"] != user:
            return self.fail(404, "Upload not found", "There is no such upload, or it has expired.")

        del uploads[match.group(1)]
        self.send_json(200, {"deleted": match.group(1)})

    def do_POST(self):
        user = self.who()

        if user is None:
            return

        if self.path == "/v1/uploads":
            return self.upload(user)

        if self.path == "/v1/jobs":
            return self.create_job(user)

        self.fail(404, "Not found", "No such page.", True)

    def upload(self, user):
        length = int(self.headers.get("Content-Length") or 0)

        if length > MAX_UPLOAD:
            return self.fail(413, "Request too large", "The request body is too big.", True)

        with lock:
            mine = [u for u in uploads.values() if u["user"] == user]

            if len(mine) >= MAX_UPLOADS:
                return self.fail(
                    429, "Too many uploads",
                    f"You can hold {MAX_UPLOADS} uploaded files at once. "
                    "Use them in a merge or delete some first.", True,
                )

        head = b""
        left = length

        while left:
            chunk = self.rfile.read(min(1024 * 1024, left))

            if not chunk:
                return

            head = head or chunk[:64]
            left -= len(chunk)
            time.sleep(UPLOAD_MS_PER_MB / 1000 * len(chunk) / (1024 * 1024))

        if length == 0:
            return self.fail(422, "Empty file", "The upload had no data in it.")

        if not sniff(head):
            return self.fail(
                415, "Not a video file",
                "This file is not a video the server can read. Send an MP4, "
                "MOV, MKV, WebM, AVI, FLV or TS file.",
            )

        upload_id = secrets.token_urlsafe(16)
        uploads[upload_id] = {"user": user, "size": length}
        self.send_json(201, {"id": upload_id, "size": length, "expires_in": 3600})

    def create_job(self, user):
        body = self.body_json()

        if isinstance(body, dict) and body.get("tool") == "basic":
            return self.create_basic(user, body)

        sources = body.get("sources") if isinstance(body, dict) else None

        if (
            not isinstance(body, dict)
            or body.get("tool") != "merge"
            or not isinstance(sources, list)
            or not 2 <= len(sources) <= 6
        ):
            return self.send_json(422, {"detail": [{"type": "value_error", "msg": "Bad request body"}]})

        with lock:
            if any(j["user"] == user and j["status"] in ("queued", "running") for j in jobs.values()):
                return self.fail(
                    429, "A job is already running",
                    "Wait for your current job to finish, then start the next.",
                )

            for position, source in enumerate(sources, 1):
                if "upload" in source:
                    found = uploads.get(source["upload"])

                    if not found or found["user"] != user:
                        return self.fail(
                            404, "Upload not found",
                            f"File number {position} was not found, has expired, "
                            "or is already in use. Upload it again.",
                            source=position,
                        )

                elif "youtu" not in str(source.get("url", "")):
                    return self.send_json(422, {"detail": [{"type": "value_error", "msg": "This is not a YouTube link."}]})

            for source in sources:
                if "upload" in source:
                    del uploads[source["upload"]]

            job_id = secrets.token_urlsafe(16)
            job = {"id": job_id, "user": user, "tool": "merge", "status": "queued", "progress": 0.0}
            jobs[job_id] = job

        threading.Thread(target=run_job, args=(job, sources), daemon=True).start()
        self.send_json(
            202, {"id": job_id, "tool": "merge", "status": "queued", "progress": 0.0}
        )

    def create_basic(self, user, body):
        link = body.get("url")
        fmt = body.get("format", "MP4")

        if not isinstance(link, str) or "youtu" not in link or fmt not in ("MP4", "MP3"):
            return self.send_json(422, {"detail": [{"type": "value_error", "msg": "This is not a YouTube link."}]})

        with lock:
            if any(j["user"] == user and j["status"] in ("queued", "running") for j in jobs.values()):
                return self.fail(
                    429, "A job is already running",
                    "Wait for your current job to finish, then start the next.",
                )

            job_id = secrets.token_urlsafe(16)
            job = {
                "id": job_id, "user": user, "tool": "basic", "format": fmt,
                "status": "queued", "progress": 0.0,
            }
            jobs[job_id] = job

        threading.Thread(target=run_basic, args=(job, link), daemon=True).start()
        self.send_json(
            202, {"id": job_id, "tool": "basic", "status": "queued", "progress": 0.0}
        )


def main() -> None:
    port = int(os.environ.get("FAKE_PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Fake video API on http://127.0.0.1:{port}")
    print(f"Key: {KEY}")
    print("In another window: VT_API_KEY=" + KEY + " python server.py")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

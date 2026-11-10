#!/usr/bin/env python3
"""A small stand-in for the tms backend. It serves the page and carries the
page's calls to the video API.

The page never talks to the video API. It talks to this server, and this
server adds the secret key and the user ID. That is the setup tms needs:
the key stays on the server, and the server decides who the user is.

    VT_API_KEY=... python server.py                 # reads .env if unset
    VT_API_URL=http://127.0.0.1:8000 python server.py
    python server.py --port 8080

Calls the page makes (and what tms's own backend must offer):

    GET    /api/config              limits the page shows and checks
    POST   /api/uploads             the file as the whole body; returns {id}
    DELETE /api/uploads/{id}        drop an upload before it is used
    POST   /api/merges              {"sources": [{"upload": id}|{"url": link}]}
    GET    /api/merges/{id}         {id, status, progress, error?}
    GET    /api/merges/{id}/download  the finished video
    POST   /api/downloads           {"url": link, "format": "MP4"|"MP3", "quality": ...}
    GET    /api/downloads/{id}      {id, status, progress, error?}
    GET    /api/downloads/{id}/download  the finished file, with its own name

Files are passed on as streams, so a multi-GB video never sits in memory.

The user here is a cookie made up on first visit. In tms, use the signed-in
user's ID from your own database instead.

Standard library only. For trying the page on your own computer.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
CHUNK = 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
COOKIE = "tms_demo_user"

API_URL = os.environ.get("VT_API_URL", "http://127.0.0.1:8000").rstrip("/")
MAX_FILE = int(os.environ.get("TMS_MAX_UPLOAD_MB", "5120")) * 1024**2
MAX_SOURCES = 6
MIN_SOURCES = 2
FORMATS = ("MP4", "MP3")
QUALITIES = ("Best", "1080p", "720p", "480p", "360p")


def read_key() -> str:
    key = os.environ.get("VT_API_KEY", "").strip()

    if key:
        return key

    # Fall back to the project's .env, looking a few folders up.
    for folder in (Path.cwd(), *list(HERE.parents)[:3]):
        env = folder / ".env"

        if not env.is_file():
            continue

        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            name, _, value = line.partition("=")

            if name.strip() == "VT_API_KEY":
                return value.strip().strip("'\"")

    return ""


API_KEY = read_key()


class Unreachable(Exception):
    """The video API did not answer."""


def connect() -> http.client.HTTPConnection:
    parts = urlsplit(API_URL)
    secure = parts.scheme == "https"
    cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection

    return cls(parts.hostname, parts.port or (443 if secure else 80), timeout=3600)


def api_headers(user: str, extra: dict | None = None) -> dict:
    return {"X-API-Key": API_KEY, "X-User-Id": user, **(extra or {})}


def call_json(method: str, path: str, user: str, payload=None) -> tuple[int, dict]:
    conn = connect()

    try:
        body = json.dumps(payload).encode() if payload is not None else None
        extra = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=api_headers(user, extra))
        reply = conn.getresponse()
        raw = reply.read()
    except OSError as exc:
        raise Unreachable from exc
    finally:
        conn.close()

    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}

    return reply.status, data if isinstance(data, dict) else {}


def problem(status: int, data: dict) -> dict:
    """Turn the video API's error into text a visitor can read."""

    if status in (401, 403):
        # Our own key is wrong. That is not the visitor's doing, and the
        # visitor must not be told about keys.
        print("The video API refused the key. Check VT_API_KEY.", file=sys.stderr)

        return {
            "title": "Video tools are not available",
            "message": "The video service could not be reached. "
            "Try again later or contact support.",
        }

    detail = data.get("detail")

    if isinstance(detail, dict) and detail.get("title"):
        out = {
            "title": detail["title"],
            "message": detail.get("message") or "Try again in a moment.",
        }

        for name in ("hint", "source", "retry_after"):
            if name in detail:
                out[name] = detail[name]

        return out

    if status == 422:
        return {
            "title": "These videos were not accepted",
            "message": "Check the files and links, then try again.",
        }

    return {
        "title": "Something went wrong",
        "message": "The video service sent an unexpected reply. Try again in a moment.",
    }


def job_view(job: dict, failed_title: str = "The merge failed") -> dict:
    out = {
        "id": job.get("id"),
        "status": job.get("status"),
        "progress": job.get("progress", 0.0),
    }
    error = job.get("error")

    if isinstance(error, dict):
        out["error"] = {
            "title": error.get("title") or failed_title,
            "message": error.get("message") or "Try again in a moment.",
            "hint": error.get("hint"),
            "code": error.get("code"),
        }

        if isinstance(error.get("clip"), int):
            out["error"]["source"] = error["clip"]

    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TmsDemo"

    def log_message(self, fmt, *args):
        sys.stderr.write("[tms] " + (fmt % args) + "\n")

    # ------------------------------------------------------------ plumbing

    def identify(self) -> None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie[COOKIE].value if COOKIE in cookie else ""
        self.fresh = not ID.fullmatch(value)
        self.user = "demo-" + secrets.token_hex(6) if self.fresh else value

    def send_head(self, status: int, headers: dict) -> None:
        self.send_response(status)

        for name, value in headers.items():
            self.send_header(name, value)

        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")

        if self.fresh:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}={self.user}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000",
            )

        self.end_headers()

    def send_json(self, status: int, data: dict, close: bool = False) -> None:
        raw = json.dumps(data).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}

        if close:
            headers["Connection"] = "close"
            self.close_connection = True

        self.send_head(status, headers)
        self.wfile.write(raw)

    def send_error_json(self, status: int, title: str, message: str, close: bool = False) -> None:
        self.send_json(status, {"error": {"title": title, "message": message}}, close)

    def unreachable(self) -> None:
        self.send_error_json(
            502,
            "The video service is not reachable",
            "Try again in a moment.",
            close=True,
        )

    def pass_error(self, status: int, data: dict) -> None:
        # Keep the status the video API gave, so the page can react to it.
        shown = status if status in (404, 409, 413, 415, 422, 429, 507) else 502
        self.send_json(shown, {"error": problem(status, data)})

    # ------------------------------------------------------------ routes

    def do_GET(self) -> None:
        self.identify()

        if self.path in ("/", "/index.html"):
            page = (HERE / "index.html").read_bytes()
            self.send_head(
                200,
                {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(page))},
            )
            self.wfile.write(page)
            return

        if self.path == "/favicon.ico":
            return self.send_head(204, {"Content-Length": "0"})

        if self.path == "/api/config":
            return self.send_json(
                200,
                {
                    "maxFileBytes": MAX_FILE,
                    "maxSources": MAX_SOURCES,
                    "minSources": MIN_SOURCES,
                },
            )

        match = re.fullmatch(r"/api/(merges|downloads)/([A-Za-z0-9_-]{1,64})(/download)?", self.path)

        if not match:
            return self.send_error_json(404, "Not found", "There is nothing at this address.")

        kind, job_id = match.group(1), match.group(2)

        if match.group(3):
            return self.download(job_id, keep_name=kind == "downloads")

        try:
            status, data = call_json("GET", f"/v1/jobs/{job_id}", self.user)
        except Unreachable:
            return self.unreachable()

        # A job belongs to one tool. Do not show a merge through the download
        # address or the other way round.
        if status == 200 and data.get("tool") not in (None, "merge" if kind == "merges" else "basic"):
            return self.send_error_json(404, "Not found", "There is no such job, or it has expired.")

        if status == 200:
            title = "The merge failed" if kind == "merges" else "The download failed"
            return self.send_json(200, job_view(data, title))

        self.pass_error(status, data)

    def do_POST(self) -> None:
        self.identify()

        if self.path == "/api/uploads":
            return self.upload()

        if self.path == "/api/merges":
            return self.start_merge()

        if self.path == "/api/downloads":
            return self.start_download()

        self.send_error_json(404, "Not found", "There is nothing at this address.", True)

    def do_DELETE(self) -> None:
        self.identify()
        match = re.fullmatch(r"/api/uploads/([A-Za-z0-9_-]{1,64})", self.path)

        if not match:
            return self.send_error_json(404, "Not found", "There is nothing at this address.")

        try:
            status, data = call_json("DELETE", f"/v1/uploads/{match.group(1)}", self.user)
        except Unreachable:
            return self.unreachable()

        if status == 200:
            return self.send_json(200, {"deleted": match.group(1)})

        self.pass_error(status, data)

    # ------------------------------------------------------------ actions

    def start_merge(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)

        if length > 8192:
            return self.send_error_json(413, "Request too large", "Send fewer videos.", True)

        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None

        sources = body.get("sources") if isinstance(body, dict) else None

        if not isinstance(sources, list) or not MIN_SOURCES <= len(sources) <= MAX_SOURCES:
            return self.send_error_json(
                422,
                "Add more videos",
                f"A merge needs {MIN_SOURCES} to {MAX_SOURCES} videos.",
            )

        # Only these two shapes go on to the video API.
        clean = []

        for source in sources:
            if isinstance(source, dict) and isinstance(source.get("upload"), str):
                clean.append({"upload": source["upload"]})
            elif isinstance(source, dict) and isinstance(source.get("url"), str):
                clean.append({"url": source["url"]})
            else:
                return self.send_error_json(422, "These videos were not accepted", "Check the files and links, then try again.")

        try:
            status, data = call_json(
                "POST", "/v1/jobs", self.user, {"tool": "merge", "sources": clean}
            )
        except Unreachable:
            return self.unreachable()

        if status == 202:
            return self.send_json(202, job_view(data))

        self.pass_error(status, data)

    def start_download(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)

        if length > 4096:
            return self.send_error_json(413, "Request too large", "Send one link.", True)

        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None

        link = body.get("url") if isinstance(body, dict) else None
        fmt = body.get("format", "MP4") if isinstance(body, dict) else None
        quality = body.get("quality", "Best") if isinstance(body, dict) else None

        if not isinstance(link, str) or not link.strip() or len(link) > 2048:
            return self.send_error_json(422, "Paste a YouTube link", "Add the link of the video you want.")

        if fmt not in FORMATS or quality not in QUALITIES:
            return self.send_error_json(422, "Choose a format", "Pick video (MP4) or audio only (MP3).")

        # Only these fields go on to the video API.
        job = {"tool": "basic", "url": link.strip(), "format": fmt, "quality": quality}

        try:
            status, data = call_json("POST", "/v1/jobs", self.user, job)
        except Unreachable:
            return self.unreachable()

        if status == 202:
            return self.send_json(202, job_view(data, "The download failed"))

        if status == 422:
            return self.send_error_json(
                422,
                "That link was not accepted",
                "Paste the full address of a YouTube video, for example https://youtu.be/…",
            )

        self.pass_error(status, data)

    def upload(self) -> None:
        length = self.headers.get("Content-Length", "")

        if not length.isdigit():
            return self.send_error_json(411, "Upload not accepted", "The file size is missing.", True)

        size = int(length)

        if size == 0:
            return self.send_error_json(422, "Empty file", "The file has no data in it.", True)

        if size > MAX_FILE:
            return self.send_error_json(
                413,
                "File too big",
                f"This file is larger than the {MAX_FILE / 1024**3:.0f} GB allowed.",
                True,
            )

        conn = connect()
        left = size

        try:
            conn.putrequest("POST", "/v1/uploads", skip_accept_encoding=True)

            for name, value in api_headers(
                self.user,
                {"Content-Type": "application/octet-stream", "Content-Length": str(size)},
            ).items():
                conn.putheader(name, value)

            conn.endheaders()

            try:
                while left:
                    chunk = self.rfile.read(min(CHUNK, left))

                    if not chunk:
                        # The visitor closed the page. Hanging up here makes
                        # the video API throw the partial file away.
                        self.close_connection = True
                        return

                    conn.send(chunk)
                    left -= len(chunk)
            except OSError:
                # The video API may have answered early. Read its reply.
                pass

            reply = conn.getresponse()
            raw = reply.read()
        except OSError:
            return self.unreachable()
        finally:
            conn.close()

        if left:
            self.close_connection = True

        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}

        if reply.status == 201 and isinstance(data, dict) and "id" in data:
            return self.send_json(201, {"id": data["id"], "size": data.get("size", size)})

        self.pass_error(reply.status, data if isinstance(data, dict) else {})

    def download(self, job_id: str, keep_name: bool = False) -> None:
        conn = connect()

        try:
            conn.request("GET", f"/v1/jobs/{job_id}/file", headers=api_headers(self.user))
            reply = conn.getresponse()

            if reply.status != 200:
                raw = reply.read()

                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    data = {}

                return self.pass_error(reply.status, data if isinstance(data, dict) else {})

            if keep_name:
                # A single download is named after the video. Pass the API's
                # name through, but always as an attachment.
                disposition = reply.getheader("Content-Disposition") or ""
                disposition = (
                    disposition
                    if disposition.lower().startswith("attachment")
                    else 'attachment; filename="video"'
                )
                headers = {
                    "Content-Type": reply.getheader("Content-Type") or "application/octet-stream",
                    "Content-Disposition": disposition,
                }
            else:
                headers = {
                    "Content-Type": "video/mp4",
                    "Content-Disposition": 'attachment; filename="merged.mp4"',
                }

            length = reply.getheader("Content-Length")

            if length:
                headers["Content-Length"] = length
            else:
                headers["Connection"] = "close"
                self.close_connection = True

            self.send_head(200, headers)

            while True:
                chunk = reply.read(CHUNK)

                if not chunk:
                    break

                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # the visitor stopped the download
        except OSError:
            self.close_connection = True
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the merge page for trying.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if not API_KEY:
        print("Set VT_API_KEY (or put it in .env) so this server can call the video API.")
        return 1

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Open http://{args.host}:{args.port}  (video API: {API_URL})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

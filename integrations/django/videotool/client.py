"""Talks to the video API (vt-api). No Django in this file.

Every call carries the secret key and the ID of the signed-in user. The
views in views.py decide who the user is; this file only sends the calls and
turns the API's errors into words a visitor can read.

Files go through as streams, so a multi-GB video never sits in memory.
"""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

CHUNK = 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
FORMATS = ("MP4", "MP3")
QUALITIES = ("Best", "1080p", "720p", "480p", "360p")


class Unreachable(Exception):
    """The video API did not answer."""


class Disconnected(Exception):
    """The visitor closed the page before the upload finished."""


@dataclass(frozen=True)
class Api:
    url: str
    key: str

    def connect(self) -> http.client.HTTPConnection:
        parts = urlsplit(self.url)
        secure = parts.scheme == "https"
        cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        return cls(parts.hostname, parts.port or (443 if secure else 80), timeout=3600)

    def headers(self, user: str, extra: dict | None = None) -> dict:
        return {"X-API-Key": self.key, "X-User-Id": user, **(extra or {})}


def _decode(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}

    return data if isinstance(data, dict) else {}


def call_json(api: Api, method: str, path: str, user: str, payload=None) -> tuple[int, dict]:
    conn = api.connect()

    try:
        body = json.dumps(payload).encode() if payload is not None else None
        extra = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=api.headers(user, extra))
        reply = conn.getresponse()
        raw = reply.read()
    except OSError as exc:
        raise Unreachable from exc
    finally:
        conn.close()

    return reply.status, _decode(raw)


def send_upload(api: Api, user: str, read, size: int) -> tuple[int, dict]:
    """Pass `size` bytes from read(n) on to the API as one upload.

    `read` is the visitor's request body. If it ends early, the connection to
    the API is dropped, so the API throws the partial file away.
    """

    conn = api.connect()
    left = size

    try:
        conn.putrequest("POST", "/v1/uploads", skip_accept_encoding=True)

        for name, value in api.headers(
            user, {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
        ).items():
            conn.putheader(name, value)

        conn.endheaders()

        try:
            while left:
                chunk = read(min(CHUNK, left))

                if not chunk:
                    raise Disconnected

                conn.send(chunk)
                left -= len(chunk)
        except OSError:
            # The API may have answered early (too big, not a video). Read it.
            pass

        reply = conn.getresponse()
        raw = reply.read()
    except OSError as exc:
        raise Unreachable from exc
    finally:
        conn.close()

    return reply.status, _decode(raw)


def open_file(api: Api, user: str, job_id: str):
    """Start fetching a finished file. Returns (connection, reply).

    The caller reads the reply in chunks and closes the connection.
    """

    conn = api.connect()

    try:
        conn.request("GET", f"/v1/jobs/{job_id}/file", headers=api.headers(user))
        return conn, conn.getresponse()
    except OSError as exc:
        conn.close()
        raise Unreachable from exc


def problem(status: int, data: dict) -> dict:
    """Turn the video API's error into text a visitor can read."""

    if status in (401, 403):
        # Our own key is wrong. That is not the visitor's doing, and the
        # visitor must not be told about keys.
        return {
            "title": "Video tools are not available",
            "message": "The video service could not be reached. Try again later or contact support.",
            "log": "The video API refused the key. Check VIDEO_TOOL_KEY.",
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


def clean_sources(sources) -> list[dict] | None:
    """Keep only the two shapes the API accepts. None if anything else is in it."""

    clean = []

    for source in sources:
        if isinstance(source, dict) and isinstance(source.get("upload"), str):
            clean.append({"upload": source["upload"]})
        elif isinstance(source, dict) and isinstance(source.get("url"), str):
            clean.append({"url": source["url"]})
        else:
            return None

    return clean

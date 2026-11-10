"""The routes the page calls. Each one checks the visitor is signed in, then
passes the call on to the video API with the visitor's ID.

The page never talks to the video API, and never sees the key.
"""

from __future__ import annotations

import json
import logging
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from . import client

log = logging.getLogger(__name__)

MAX_SOURCES = 6
MIN_SOURCES = 2


# ---------------------------------------------------------------- settings


def api() -> client.Api:
    key = getattr(settings, "VIDEO_TOOL_KEY", "")

    if not key:
        raise ImproperlyConfigured("Set VIDEO_TOOL_KEY in settings (the VT_API_KEY of vt-api).")

    return client.Api(getattr(settings, "VIDEO_TOOL_URL", "http://127.0.0.1:8000").rstrip("/"), key)


def max_file_bytes() -> int:
    return int(getattr(settings, "VIDEO_TOOL_MAX_UPLOAD_MB", 5120)) * 1024**2


def user_id(request) -> str:
    """The ID the video API knows this visitor by.

    It must be stable and unique per account, 1 to 64 letters, digits or
    . _ @ -. Set VIDEO_TOOL_USER_ID to a function(request) to change it.
    """

    custom = getattr(settings, "VIDEO_TOOL_USER_ID", None)

    if callable(custom):
        return str(custom(request))

    return f"tms-{request.user.pk}"


# ---------------------------------------------------------------- helpers


def fail(status: int, title: str, message: str) -> JsonResponse:
    return JsonResponse({"error": {"title": title, "message": message}}, status=status)


def pass_error(status: int, data: dict) -> JsonResponse:
    # Keep the status the API gave, so the page can react to it.
    shown = status if status in (404, 409, 413, 415, 422, 429, 507) else 502
    out = client.problem(status, data)
    note = out.pop("log", None)

    if note:
        log.error(note)

    return JsonResponse({"error": out}, status=shown)


def unreachable() -> JsonResponse:
    log.error("The video API did not answer at %s", getattr(settings, "VIDEO_TOOL_URL", "?"))
    return fail(502, "The video service is not reachable", "Try again in a moment.")


def endpoint(*methods: str):
    """Only these methods, only signed-in visitors, never cached."""

    def decorate(view):
        @wraps(view)
        @never_cache
        @require_http_methods(list(methods))
        def inner(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return fail(401, "Sign in to continue", "Your session has ended. Sign in again.")

            return view(request, *args, **kwargs)

        return inner

    return decorate


def json_body(request, limit: int):
    length = request.META.get("CONTENT_LENGTH", "")

    if not length.isdigit() or int(length) > limit:
        return None

    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return None

    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------- the page


@login_required
def index(request):
    base = reverse("videotool:config").rsplit("/", 1)[0]
    return render(request, "videotool/index.html", {"api_base": base})


# ---------------------------------------------------------------- routes


@endpoint("GET")
def config(request):
    return JsonResponse(
        {"maxFileBytes": max_file_bytes(), "maxSources": MAX_SOURCES, "minSources": MIN_SOURCES}
    )


@endpoint("POST")
def upload(request):
    length = request.META.get("CONTENT_LENGTH", "")

    if not length.isdigit():
        return fail(411, "Upload not accepted", "The file size is missing.")

    size = int(length)

    if size == 0:
        return fail(422, "Empty file", "The file has no data in it.")

    if size > max_file_bytes():
        return fail(
            413, "File too big", f"This file is larger than the {max_file_bytes() / 1024**3:.0f} GB allowed."
        )

    # request.read hands over the body as it arrives. Never touch request.body
    # here: that would load the whole file into memory.
    try:
        status, data = client.send_upload(api(), user_id(request), request.read, size)
    except client.Disconnected:
        return HttpResponse(status=400)
    except client.Unreachable:
        return unreachable()

    if status == 201 and "id" in data:
        return JsonResponse({"id": data["id"], "size": data.get("size", size)}, status=201)

    return pass_error(status, data)


@endpoint("DELETE")
def delete_upload(request, upload_id):
    if not client.ID.fullmatch(upload_id):
        return fail(404, "Not found", "There is nothing at this address.")

    try:
        status, data = client.call_json(api(), "DELETE", f"/v1/uploads/{upload_id}", user_id(request))
    except client.Unreachable:
        return unreachable()

    if status == 200:
        return JsonResponse({"deleted": upload_id})

    return pass_error(status, data)


@endpoint("POST")
def start_merge(request):
    body = json_body(request, 8192)
    sources = body.get("sources") if body else None

    if not isinstance(sources, list) or not MIN_SOURCES <= len(sources) <= MAX_SOURCES:
        return fail(422, "Add more videos", f"A merge needs {MIN_SOURCES} to {MAX_SOURCES} videos.")

    clean = client.clean_sources(sources)

    if clean is None:
        return fail(422, "These videos were not accepted", "Check the files and links, then try again.")

    try:
        status, data = client.call_json(
            api(), "POST", "/v1/jobs", user_id(request), {"tool": "merge", "sources": clean}
        )
    except client.Unreachable:
        return unreachable()

    if status == 202:
        return JsonResponse(client.job_view(data), status=202)

    return pass_error(status, data)


@endpoint("POST")
def start_download(request):
    body = json_body(request, 4096)

    if body is None:
        return fail(422, "Paste a YouTube link", "Add the link of the video you want.")

    link = body.get("url")
    fmt = body.get("format", "MP4")
    quality = body.get("quality", "Best")

    if not isinstance(link, str) or not link.strip() or len(link) > 2048:
        return fail(422, "Paste a YouTube link", "Add the link of the video you want.")

    if fmt not in client.FORMATS or quality not in client.QUALITIES:
        return fail(422, "Choose a format", "Pick video (MP4) or audio only (MP3).")

    job = {"tool": "basic", "url": link.strip(), "format": fmt, "quality": quality}

    try:
        status, data = client.call_json(api(), "POST", "/v1/jobs", user_id(request), job)
    except client.Unreachable:
        return unreachable()

    if status == 202:
        return JsonResponse(client.job_view(data, "The download failed"), status=202)

    if status == 422:
        return fail(
            422,
            "That link was not accepted",
            "Paste the full address of a YouTube video, for example https://youtu.be/…",
        )

    return pass_error(status, data)


def job_status(request, job_id, tool, failed_title):
    if not client.ID.fullmatch(job_id):
        return fail(404, "Not found", "There is no such job, or it has expired.")

    try:
        status, data = client.call_json(api(), "GET", f"/v1/jobs/{job_id}", user_id(request))
    except client.Unreachable:
        return unreachable()

    if status != 200:
        return pass_error(status, data)

    # A job belongs to one tool. Do not show a merge through the download
    # address or the other way round.
    if data.get("tool") not in (None, tool):
        return fail(404, "Not found", "There is no such job, or it has expired.")

    return JsonResponse(client.job_view(data, failed_title))


@endpoint("GET")
def merge_status(request, job_id):
    return job_status(request, job_id, "merge", "The merge failed")


@endpoint("GET")
def download_status(request, job_id):
    return job_status(request, job_id, "basic", "The download failed")


def stream_file(request, job_id, keep_name):
    if not client.ID.fullmatch(job_id):
        return fail(404, "Not found", "There is no such job, or it has expired.")

    try:
        conn, reply = client.open_file(api(), user_id(request), job_id)
    except client.Unreachable:
        return unreachable()

    if reply.status != 200:
        try:
            data = client._decode(reply.read())
        finally:
            conn.close()

        return pass_error(reply.status, data)

    if keep_name:
        # A single download is named after the video. Pass the API's name
        # through, but always as an attachment.
        disposition = reply.getheader("Content-Disposition") or ""

        if not disposition.lower().startswith("attachment"):
            disposition = 'attachment; filename="video"'

        content_type = reply.getheader("Content-Type") or "application/octet-stream"
    else:
        disposition = 'attachment; filename="merged.mp4"'
        content_type = "video/mp4"

    def chunks():
        try:
            while True:
                chunk = reply.read(client.CHUNK)

                if not chunk:
                    break

                yield chunk
        finally:
            conn.close()

    response = StreamingHttpResponse(chunks(), content_type=content_type)
    response["Content-Disposition"] = disposition
    response["X-Content-Type-Options"] = "nosniff"
    length = reply.getheader("Content-Length")

    if length:
        response["Content-Length"] = length

    return response


@endpoint("GET")
def merge_file(request, job_id):
    return stream_file(request, job_id, keep_name=False)


@endpoint("GET")
def download_file(request, job_id):
    return stream_file(request, job_id, keep_name=True)

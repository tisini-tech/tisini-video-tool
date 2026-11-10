"""Web routes. All work happens in jobs.py and runners.py."""

from __future__ import annotations

import asyncio
import errno
import logging
import math
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from ..core import find_ffmpeg
from .hardening import Hardening
from .jobs import (
    Job,
    JobStore,
    TooSoonError,
    UnknownUploadError,
    UploadLimitError,
    UserBusyError,
)
from .ratelimit import CallLimiter, KeyGuard
from .runners import make_runners
from .schemas import JobRequest
from .security import Settings, key_ok, valid_user_id
from .uploads import UploadRejected, check_video, save_stream

logger = logging.getLogger(__name__)

NOT_FOUND = {
    "title": "Job not found",
    "message": "There is no such job, or it has expired.",
}

UPLOAD_NOT_FOUND = {
    "title": "Upload not found",
    "message": "There is no such upload, or it has expired.",
}

# Tells the /docs page that the body of an upload is the file itself, so
# it shows a file picker. The route reads the body as a stream.
UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/octet-stream": {
                "schema": {"type": "string", "format": "binary"}
            }
        },
    }
}


def _minutes(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))

    return f"{minutes} minute" + ("" if minutes == 1 else "s")


def _view(job: Job) -> dict:
    data = {
        "id": job.id,
        "tool": job.tool,
        "status": job.status,
        "progress": round(job.progress, 3),
    }

    if job.error:
        data["error"] = job.error

    if job.status == "done":
        data["file_url"] = f"/v1/jobs/{job.id}/file"

    return data


def create_app(
    settings: Settings,
    runners: dict | None = None,
    *,
    janitor: bool = True,
    test_page: bool = False,
    docs: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the API.

    ``docs`` serves /docs and /openapi.json. The vt-api command turns them
    off unless asked, so a real server does not list its own routes.
    ``clock`` times the key lockout and the call limit; tests replace it.
    """

    store = JobStore(
        settings.work_dir,
        runners or make_runners(settings),
        max_workers=settings.max_workers,
        ttl=settings.ttl,
        limits=settings.limits,
        janitor=janitor,
    )
    limits = settings.limits
    guard = KeyGuard(
        limits.key_fails,
        limits.key_fail_window,
        limits.key_block,
        clock=clock,
    )
    calls = CallLimiter(limits.calls_per_min, 60, clock=clock)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        store.shutdown()

    app = FastAPI(
        title="Video Tool API",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.store = store
    # Every route takes a small body, except the one that takes a file.
    app.add_middleware(Hardening, bigger={"/v1/uploads": limits.max_upload})

    def who(
        request: Request,
        x_api_key: str = Header(default=""),
        x_user_id: str = Header(default=""),
    ) -> str:
        address = request.client.host if request.client else "unknown"
        wait = guard.blocked_for(address)

        if wait:
            raise HTTPException(
                429,
                {
                    "title": "Too many wrong keys",
                    "message": "This address is blocked for now. "
                    f"Try again in {_minutes(wait)}.",
                    "retry_after": wait,
                },
                headers={"Retry-After": str(wait)},
            )

        if not key_ok(x_api_key, settings):
            guard.fail(address)

            raise HTTPException(401, {"title": "Not allowed", "message": "The API key is missing or wrong."})

        if not valid_user_id(x_user_id):
            raise HTTPException(400, {"title": "Bad user ID", "message": "Send X-User-Id with 1 to 64 letters, digits or . _ @ -"})

        wait = calls.check(x_user_id)

        if wait:
            raise HTTPException(
                429,
                {
                    "title": "Too many calls",
                    "message": f"Slow down. Try again in {wait} seconds.",
                    "retry_after": wait,
                },
                headers={"Retry-After": str(wait)},
            )

        return x_user_id

    def own(user: str, job_id: str) -> Job:
        job = store.get(user, job_id)

        if job is None:
            raise HTTPException(404, NOT_FOUND)

        return job

    if docs:

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/docs")

    @app.get("/v1/health")
    def health(x_api_key: str = Header(default="")) -> dict:
        # Without the key, the reply says nothing about the server.
        if key_ok(x_api_key, settings):
            return {"status": "ok", "ffmpeg": bool(find_ffmpeg())}

        return {"status": "ok"}

    @app.post("/v1/uploads", status_code=201, openapi_extra=UPLOAD_BODY)
    async def upload_file(request: Request, user: str = Depends(who)) -> dict:
        """Hand over one video file. The body is the file, as it is.

        The reply holds the upload ``id``. Use it in a merge job as
        ``{"upload": "<id>"}``. An upload you do not use is deleted after
        an hour.
        """

        try:
            upload = store.start_upload(user)
        except UploadLimitError as exc:
            raise HTTPException(
                429, {"title": exc.title, "message": exc.message}
            )

        done = False

        try:
            size = await save_stream(
                request.stream(), upload.file, upload.limit
            )
            await asyncio.to_thread(check_video, upload.file)
            store.finish_upload(upload, size)
            done = True
        except UploadRejected as exc:
            raise HTTPException(
                exc.status, {"title": exc.title, "message": exc.message}
            )
        except OSError as exc:
            logger.error("Upload could not be saved", exc_info=exc)
            full = exc.errno == errno.ENOSPC

            raise HTTPException(
                507 if full else 500,
                {
                    "title": "Server disk is full" if full else "Upload failed",
                    "message": "The server has no room for this file."
                    if full
                    else "The server could not save this file.",
                },
            )
        finally:
            # Also runs when the sender hangs up half way.
            if not done:
                store.abort_upload(upload)

        return {"id": upload.id, "size": size, "expires_in": settings.ttl}

    @app.delete("/v1/uploads/{upload_id}")
    def delete_upload(upload_id: str, user: str = Depends(who)) -> dict:
        result = store.drop_upload(user, upload_id)

        if result == "missing":
            raise HTTPException(404, UPLOAD_NOT_FOUND)

        if result == "busy":
            raise HTTPException(
                409,
                {
                    "title": "Upload is in use",
                    "message": "A job is using this file, or it is still "
                    "arriving. It is deleted when the job ends.",
                },
            )

        return {"deleted": upload_id}

    @app.post("/v1/jobs", status_code=202)
    def create_job(body: JobRequest, user: str = Depends(who)) -> dict:
        try:
            job = store.submit(user, body.tool, body.params())
        except UnknownUploadError as exc:
            raise HTTPException(
                404,
                {
                    "title": "Upload not found",
                    "message": f"File number {exc.position} was not found, "
                    "has expired, or is already in use. Upload it again.",
                    "source": exc.position,
                },
            )
        except UserBusyError:
            raise HTTPException(429, {"title": "A job is already running", "message": "Wait for your current job to finish, then start the next."})
        except TooSoonError as exc:
            raise HTTPException(
                429,
                {
                    "title": exc.title,
                    "message": exc.message,
                    "retry_after": exc.retry_after,
                },
                headers={"Retry-After": str(exc.retry_after)},
            )

        return _view(job)

    @app.get("/v1/jobs/{job_id}")
    def job_status(job_id: str, user: str = Depends(who)) -> dict:
        return _view(own(user, job_id))

    @app.get("/v1/jobs/{job_id}/file")
    def job_file(job_id: str, user: str = Depends(who)):
        job = own(user, job_id)

        if job.status != "done" or not job.file or not job.file.is_file():
            raise HTTPException(409, {"title": "No file yet", "message": "The job has not produced a file."})

        return FileResponse(job.file, filename=job.file.name)

    @app.delete("/v1/jobs/{job_id}")
    def delete_job(job_id: str, user: str = Depends(who)) -> dict:
        result = store.delete(user, job_id)

        if result == "missing":
            raise HTTPException(404, NOT_FOUND)

        if result == "busy":
            raise HTTPException(409, {"title": "Job is running", "message": "A running job cannot be stopped yet. Delete it after it ends."})

        return {"deleted": job_id}

    if test_page:
        from .testpage import add_test_page

        add_test_page(app)

    return app

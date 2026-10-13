"""Web routes. All work happens in jobs.py and runners.py."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from ..core import find_ffmpeg
from .hardening import Hardening
from .jobs import Job, JobStore, TooSoonError, UserBusyError
from .ratelimit import CallLimiter, KeyGuard
from .runners import make_runners
from .schemas import BasicJob
from .security import Settings, key_ok, valid_user_id

NOT_FOUND = {
    "title": "Job not found",
    "message": "There is no such job, or it has expired.",
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
    app.add_middleware(Hardening)

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

    @app.post("/v1/jobs", status_code=202)
    def create_job(body: BasicJob, user: str = Depends(who)) -> dict:
        try:
            job = store.submit(user, body.tool, body.params())
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

"""Web routes. All work happens in jobs.py and runners.py."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from ..core import find_ffmpeg
from .jobs import Job, JobStore, TooSoonError, UserBusyError
from .runners import make_runners
from .schemas import BasicJob
from .security import Settings, key_matches, valid_user_id

NOT_FOUND = {
    "title": "Job not found",
    "message": "There is no such job, or it has expired.",
}


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
) -> FastAPI:
    store = JobStore(
        settings.work_dir,
        runners or make_runners(settings),
        max_workers=settings.max_workers,
        ttl=settings.ttl,
        limits=settings.limits,
        janitor=janitor,
    )

    @asynccontextmanager
    async def lifespan(_app):
        yield
        store.shutdown()

    app = FastAPI(title="Video Tool API", lifespan=lifespan)
    app.state.store = store

    def who(
        x_api_key: str = Header(default=""),
        x_user_id: str = Header(default=""),
    ) -> str:
        if not key_matches(x_api_key, settings.api_key):
            raise HTTPException(401, {"title": "Not allowed", "message": "The API key is missing or wrong."})

        if not valid_user_id(x_user_id):
            raise HTTPException(400, {"title": "Bad user ID", "message": "Send X-User-Id with 1 to 64 letters, digits or . _ @ -"})

        return x_user_id

    def own(user: str, job_id: str) -> Job:
        job = store.get(user, job_id)

        if job is None:
            raise HTTPException(404, NOT_FOUND)

        return job

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok", "ffmpeg": bool(find_ffmpeg())}

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

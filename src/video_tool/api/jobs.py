"""Job store and workers. No web code lives here.

Rules kept here:

- one waiting or running job per user
- a limit on jobs per hour, counting every attempt
- a pause after several failures in a row
- a link that failed for a fixed reason is remembered for a short time
- a running job that goes over its time limit is stopped
- a failed job loses its folder at once, a finished one after ``ttl``
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ErrorInfo, explain
from .security import Limits

logger = logging.getLogger(__name__)

ACTIVE = ("queued", "running")

# Job folders are named by secrets.token_urlsafe(16). Only folders with
# such a name are ever deleted at start-up.
FOLDER_NAME = re.compile(r"^[A-Za-z0-9_-]{22}$")

# Uploaded files live in work_dir/uploads/<id>/. The folder is emptied when
# the server starts, because the list of uploads is only kept in memory.
UPLOAD_FOLDER = "uploads"

# Causes that repeat for the same link, so a second try can skip YouTube.
REMEMBER = {
    "private", "unavailable", "bad_link", "invalid_link",
    "unsupported_site", "age_restricted", "region", "copyright",
    "http_404",
}

# Codes that mean Video Tool has no rule for the cause. These keep the
# full traceback in the log.
UNEXPECTED = {"unexpected", "download_failed"}

# A runner gets (params, job folder, progress callback) and returns a file.
Runner = Callable[[dict, Path, Callable[[float], None]], Path]


class UserBusyError(Exception):
    """The user already has a job waiting or running."""


class TooSoonError(Exception):
    """A limit stops this request for now. Try again later."""

    def __init__(self, title: str, message: str, retry_after: float):
        super().__init__(message)
        self.title = title
        self.message = message
        self.retry_after = int(math.ceil(retry_after))


class UnknownToolError(ValueError):
    """No runner exists for this tool."""


class JobStopped(Exception):
    """Raised inside a runner when its job was stopped from outside."""


class UploadLimitError(Exception):
    """The user holds too many uploads, or has no upload space left."""

    def __init__(self, title: str, message: str):
        super().__init__(message)
        self.title = title
        self.message = message


class UnknownUploadError(Exception):
    """A job names an upload that is missing, expired, in use or not theirs.

    ``position`` counts the sources of the request from 1.
    """

    def __init__(self, position: int):
        super().__init__(position)
        self.position = position


@dataclass
class Upload:
    """A file a user handed over, waiting to be used by a job."""

    id: str
    user: str
    folder: Path
    file: Path
    limit: int  # most bytes this upload may take; reserved while it arrives
    created: float
    size: int = 0
    ready: bool = False
    job: str | None = None  # the job that uses it, once there is one


@dataclass
class Job:
    id: str
    user: str
    tool: str
    folder: Path
    status: str = "queued"
    progress: float = 0.0
    created: float = 0.0
    started: float | None = None
    finished: float | None = None
    file: Path | None = None
    error: dict | None = None


@dataclass
class _User:
    attempts: deque = field(default_factory=deque)
    streak: int = 0
    paused_until: float = 0.0


def _error_dict(info: ErrorInfo) -> dict:
    """Plain text only. detail and cli_hint stay in the server log."""

    return {
        "level": info.level,
        "title": info.title,
        "message": info.message,
        "hint": info.hint,
        "code": info.code,
    }


def _wait_text(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))

    return f"{minutes} minute" + ("" if minutes == 1 else "s")


class JobStore:
    def __init__(
        self,
        work_dir: Path,
        runners: Mapping[str, Runner],
        *,
        max_workers: int = 2,
        ttl: int = 3600,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.time,
        janitor: bool = False,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.work_dir / UPLOAD_FOLDER, ignore_errors=True)
        self.ttl = ttl
        self.limits = limits or Limits()
        self._runners = dict(runners)
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._users: dict[str, _User] = {}
        self._failed_links: dict[str, tuple[float, dict]] = {}
        self._uploads: dict[str, Upload] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

        self._remove_old_folders()

        if janitor:
            threading.Thread(target=self._janitor, daemon=True).start()

    # ------------------------------------------------------------ public

    def submit(self, user: str, tool: str, params: dict) -> Job:
        if tool not in self._runners:
            raise UnknownToolError(tool)

        key = json.dumps([tool, params], sort_keys=True)

        with self._lock:
            self._sweep_locked()
            now = self._clock()

            if any(
                j.user == user and j.status in ACTIVE
                for j in self._jobs.values()
            ):
                raise UserBusyError(user)

            state = self._users.setdefault(user, _User())
            self._check_limits(state, now)

            # A bad upload is refused before it costs the user an attempt.
            run_params, upload_ids = self._resolve_uploads_locked(user, params)

            state.attempts.append(now)

            job_id = secrets.token_urlsafe(16)
            folder = self.work_dir / job_id
            remembered = self._failed_links.get(key)

            if remembered and remembered[0] > now:
                job = Job(
                    job_id, user, tool, folder,
                    status="failed", created=now, finished=now,
                    error=remembered[1],
                )
                self._jobs[job_id] = job
                self._count_failure(state, now)

                return job

            folder.mkdir(parents=True, exist_ok=True)
            job = Job(job_id, user, tool, folder, created=now)
            self._jobs[job_id] = job

            for upload_id in upload_ids:
                self._uploads[upload_id].job = job_id

        self._pool.submit(self._work, job_id, key, run_params)

        return job

    def get(self, user: str, job_id: str) -> Job | None:
        with self._lock:
            self._sweep_locked()
            job = self._jobs.get(job_id)

            return job if job and job.user == user else None

    def delete(self, user: str, job_id: str) -> str:
        """Return "deleted", "missing" or "busy"."""

        with self._lock:
            job = self._jobs.get(job_id)

            if job is None or job.user != user:
                return "missing"

            if job.status == "running":
                return "busy"

            del self._jobs[job_id]
            gone = self._release_uploads_locked(job_id)

        shutil.rmtree(job.folder, ignore_errors=True)

        for folder in gone:
            shutil.rmtree(folder, ignore_errors=True)

        return "deleted"

    # ---------------------------------------------------------- uploads

    def start_upload(self, user: str) -> Upload:
        """Reserve room for one upload. The caller then fills ``file``.

        Raises ``UploadLimitError`` when the user holds too many files or
        has no space left. While the file arrives, the most it may take is
        counted against the user's space, so two uploads sent at the same
        time cannot get past the limit together.
        """

        with self._lock:
            self._sweep_locked()
            now = self._clock()
            mine = [u for u in self._uploads.values() if u.user == user]

            if len(mine) >= self.limits.max_uploads:
                raise UploadLimitError(
                    "Too many uploads",
                    f"You can hold {self.limits.max_uploads} uploaded files "
                    "at once. Use them in a merge or delete some first.",
                )

            used = sum(u.size if u.ready else u.limit for u in mine)
            room = min(
                self.limits.max_upload, self.limits.upload_quota - used
            )

            if room <= 0:
                raise UploadLimitError(
                    "Upload space is full",
                    "Your uploaded files take all the space you may use. "
                    "Use them in a merge or delete some first.",
                )

            upload_id = secrets.token_urlsafe(16)
            folder = self.work_dir / UPLOAD_FOLDER / upload_id
            folder.mkdir(parents=True, exist_ok=True)
            upload = Upload(
                upload_id, user, folder, folder / "upload.bin", room, now
            )
            self._uploads[upload_id] = upload

        return upload

    def finish_upload(self, upload: Upload, size: int) -> None:
        """The file arrived and passed its checks. It can be used now."""

        with self._lock:
            upload.size = size
            upload.created = self._clock()
            upload.ready = True

    def abort_upload(self, upload: Upload) -> None:
        """The upload failed or was cut off. Forget it and its file."""

        with self._lock:
            self._uploads.pop(upload.id, None)

        shutil.rmtree(upload.folder, ignore_errors=True)

    def drop_upload(self, user: str, upload_id: str) -> str:
        """Return "deleted", "missing" or "busy" (arriving, or in a job)."""

        with self._lock:
            upload = self._uploads.get(upload_id)

            if upload is None or upload.user != user:
                return "missing"

            if upload.job is not None or not upload.ready:
                return "busy"

            del self._uploads[upload_id]

        shutil.rmtree(upload.folder, ignore_errors=True)

        return "deleted"

    def sweep(self) -> None:
        """Expire old jobs, stop slow ones, forget old failures."""

        with self._lock:
            self._sweep_locked()

    def shutdown(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ----------------------------------------------------------- limits

    def _check_limits(self, state: _User, now: float) -> None:
        cutoff = now - 3600

        while state.attempts and state.attempts[0] <= cutoff:
            state.attempts.popleft()

        if state.paused_until > now:
            wait = state.paused_until - now

            raise TooSoonError(
                "Too many failed jobs",
                "Several jobs in a row failed. "
                f"Try again in {_wait_text(wait)}.",
                wait,
            )

        if len(state.attempts) >= self.limits.per_hour:
            wait = state.attempts[0] + 3600 - now

            raise TooSoonError(
                "Hourly limit reached",
                f"You can start {self.limits.per_hour} jobs an hour. "
                f"Try again in {_wait_text(wait)}.",
                wait,
            )

    def _count_failure(self, state: _User, now: float) -> None:
        state.streak += 1

        if state.streak >= self.limits.fail_streak:
            state.paused_until = now + self.limits.fail_pause
            state.streak = 0

    # ----------------------------------------------------------- worker

    def _work(self, job_id: str, key: str, params: dict) -> None:
        with self._lock:
            job = self._jobs.get(job_id)

            if job is None or job.status != "queued":
                return

            job.status = "running"
            job.started = self._clock()

        def progress(value: float) -> None:
            if job.status != "running":
                raise JobStopped()

            job.progress = max(0.0, min(1.0, float(value)))

        try:
            path = self._runners[job.tool](params, job.folder, progress)
        except Exception as exc:
            self._finish_failed(job, key, exc)
        else:
            self._finish_done(job, path)

    def _finish_failed(self, job: Job, key: str, exc: Exception) -> None:
        if isinstance(exc, JobStopped):
            shutil.rmtree(job.folder, ignore_errors=True)
            self._drop_job_uploads(job.id)
            return

        info = explain(exc)

        if info.code in UNEXPECTED:
            logger.error("Job %s failed", job.id, exc_info=exc)
        else:
            logger.warning(
                "Job %s failed: %s (%s)", job.id, info.title, info.code
            )

        error = _error_dict(info)

        # A merge runner marks the exception with the number of the clip
        # that failed (1 is the first link), so the caller can say which.
        clip = getattr(exc, "clip_number", None)

        if isinstance(clip, int):
            error["clip"] = clip

        with self._lock:
            gone = self._release_uploads_locked(job.id)

            if job.status == "running":
                now = self._clock()
                job.error = error
                job.status = "failed"
                job.finished = now

                if info.code in REMEMBER:
                    self._failed_links[key] = (
                        now + self.limits.fail_memory,
                        error,
                    )

                self._count_failure(
                    self._users.setdefault(job.user, _User()), now
                )

        # A failed job keeps no files. The error is enough.
        shutil.rmtree(job.folder, ignore_errors=True)

        for folder in gone:
            shutil.rmtree(folder, ignore_errors=True)

    def _finish_done(self, job: Job, path: Path) -> None:
        with self._lock:
            # The merged file is all that is kept. The uploads it was
            # made from are no longer needed.
            gone = self._release_uploads_locked(job.id)

            if job.status != "running":  # stopped while it ran
                stopped = True
            else:
                stopped = False
                job.file = Path(path)
                job.progress = 1.0
                job.status = "done"
                job.finished = self._clock()
                self._users.setdefault(job.user, _User()).streak = 0

        for folder in gone:
            shutil.rmtree(folder, ignore_errors=True)

        if stopped:
            shutil.rmtree(job.folder, ignore_errors=True)

    # ---------------------------------------------------------- tidying

    def _resolve_uploads_locked(
        self, user: str, params: dict
    ) -> tuple[dict, list[str]]:
        """Swap each ``{"upload": id}`` source for ``{"path": file}``.

        Returns the parameters the runner gets and the IDs the job will
        use. Nothing is changed yet, so a refusal leaves no mark. A source
        is refused when the upload is missing, expired, not this user's,
        still arriving, or already used by another job.
        """

        sources = params.get("sources")

        if not sources:
            return params, []

        resolved: list[dict] = []
        used: list[str] = []

        for position, source in enumerate(sources, 1):
            upload_id = source.get("upload")

            if upload_id is None:
                resolved.append(source)
                continue

            upload = self._uploads.get(upload_id)

            if (
                upload is None
                or upload.user != user
                or not upload.ready
                or upload.job is not None
            ):
                raise UnknownUploadError(position)

            resolved.append({"path": str(upload.file)})
            used.append(upload_id)

        return {**params, "sources": resolved}, used

    def _release_uploads_locked(self, job_id: str) -> list[Path]:
        """Forget the uploads a job used. Return their folders to delete."""

        folders = []

        for upload_id, upload in list(self._uploads.items()):
            if upload.job == job_id:
                del self._uploads[upload_id]
                folders.append(upload.folder)

        return folders

    def _drop_job_uploads(self, job_id: str) -> None:
        with self._lock:
            folders = self._release_uploads_locked(job_id)

        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)

    def _time_limit(self, tool: str) -> int:
        """A merge downloads several videos, so it may run longer."""

        if tool == "merge":
            return self.limits.merge_timeout

        return self.limits.job_timeout

    def _sweep_locked(self) -> None:
        now = self._clock()

        for job_id, job in list(self._jobs.items()):
            if job.finished is not None and now - job.finished > self.ttl:
                shutil.rmtree(job.folder, ignore_errors=True)
                del self._jobs[job_id]

                for folder in self._release_uploads_locked(job_id):
                    shutil.rmtree(folder, ignore_errors=True)

            elif (
                job.status == "running"
                and job.started is not None
                and now - job.started > self._time_limit(job.tool)
            ):
                info = explain(TimeoutError())
                logger.warning(
                    "Job %s stopped: over %s seconds",
                    job.id, self._time_limit(job.tool),
                )
                job.error = _error_dict(info)
                job.status = "failed"
                job.finished = now
                self._count_failure(
                    self._users.setdefault(job.user, _User()), now
                )

                for folder in self._release_uploads_locked(job_id):
                    shutil.rmtree(folder, ignore_errors=True)

        for key, (until, _) in list(self._failed_links.items()):
            if until <= now:
                del self._failed_links[key]

        # An upload nobody used goes after the same time as a finished job.
        for upload_id, upload in list(self._uploads.items()):
            if (
                upload.ready
                and upload.job is None
                and now - upload.created > self.ttl
            ):
                shutil.rmtree(upload.folder, ignore_errors=True)
                del self._uploads[upload_id]

        for user, state in list(self._users.items()):
            while state.attempts and state.attempts[0] <= now - 3600:
                state.attempts.popleft()

            if (
                not state.attempts
                and state.streak == 0
                and state.paused_until <= now
            ):
                del self._users[user]

    def _remove_old_folders(self) -> None:
        for child in self.work_dir.iterdir():
            if child.is_dir() and FOLDER_NAME.match(child.name):
                shutil.rmtree(child, ignore_errors=True)

    def _janitor(self) -> None:
        while not self._stop.wait(self.limits.sweep_every):
            try:
                self.sweep()
            except Exception:
                logger.exception("Tidy-up failed")

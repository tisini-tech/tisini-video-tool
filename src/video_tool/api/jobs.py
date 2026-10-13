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
        self.ttl = ttl
        self.limits = limits or Limits()
        self._runners = dict(runners)
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._users: dict[str, _User] = {}
        self._failed_links: dict[str, tuple[float, dict]] = {}
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

        self._pool.submit(self._work, job_id, key, params)

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

        shutil.rmtree(job.folder, ignore_errors=True)

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
            return

        info = explain(exc)

        if info.code in UNEXPECTED:
            logger.error("Job %s failed", job.id, exc_info=exc)
        else:
            logger.warning(
                "Job %s failed: %s (%s)", job.id, info.title, info.code
            )

        error = _error_dict(info)

        with self._lock:
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

    def _finish_done(self, job: Job, path: Path) -> None:
        with self._lock:
            if job.status != "running":  # stopped while it ran
                stopped = True
            else:
                stopped = False
                job.file = Path(path)
                job.progress = 1.0
                job.status = "done"
                job.finished = self._clock()
                self._users.setdefault(job.user, _User()).streak = 0

        if stopped:
            shutil.rmtree(job.folder, ignore_errors=True)

    # ---------------------------------------------------------- tidying

    def _sweep_locked(self) -> None:
        now = self._clock()

        for job_id, job in list(self._jobs.items()):
            if job.finished is not None and now - job.finished > self.ttl:
                shutil.rmtree(job.folder, ignore_errors=True)
                del self._jobs[job_id]

            elif (
                job.status == "running"
                and job.started is not None
                and now - job.started > self.limits.job_timeout
            ):
                info = explain(TimeoutError())
                logger.warning(
                    "Job %s stopped: over %s seconds",
                    job.id, self.limits.job_timeout,
                )
                job.error = _error_dict(info)
                job.status = "failed"
                job.finished = now
                self._count_failure(
                    self._users.setdefault(job.user, _User()), now
                )

        for key, (until, _) in list(self._failed_links.items()):
            if until <= now:
                del self._failed_links[key]

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

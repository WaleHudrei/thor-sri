"""
Job queue + lifecycle manager for thor-sri.

Responsibilities:
  • Accept new jobs, enforce concurrency limits + cooldown
  • Dispatch jobs to a worker thread pool
  • Stream per-job log buffers (in-memory, capped)
  • Support cancellation via cooperative flags
  • Sync every state transition to Postgres
"""

import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Queue, Empty
from typing import Callable, Optional

from src import db

log = logging.getLogger("thor-sri.queue")

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
MAX_LOG_LINES_PER_JOB = 2000


# ── Job record ────────────────────────────────────────────────────────────────
@dataclass
class Job:
    job_id: str
    params: dict
    status: str = "queued"
    progress_current: int = 0
    progress_total: int = 0
    result_count: int = 0
    error_count: int = 0
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: _now_iso())
    # In-memory only
    logs: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES_PER_JOB))
    cancel_flag: threading.Event = field(default_factory=threading.Event)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Queue manager ─────────────────────────────────────────────────────────────
class JobQueue:
    """
    Thread-safe job queue with cooldown awareness. DB is the source of truth
    for persistence; the in-memory dict is for fast access to active jobs.
    """

    def __init__(self, worker: Callable[[Job], None]):
        self._worker = worker
        self._jobs: dict[str, Job] = {}
        self._pending: Queue[str] = Queue()
        self._lock = threading.RLock()

        # Cooldown state (service-wide)
        self._cooldown_until: float = 0.0
        self._cooldown_reason: Optional[str] = None

        # Spin up worker threads
        for i in range(MAX_CONCURRENT_JOBS):
            t = threading.Thread(target=self._loop, name=f"sri-worker-{i}",
                                 daemon=True)
            t.start()

    # ── Public API ────────────────────────────────────────────────────────────
    def submit(self, params: dict) -> tuple[Optional[str], Optional[dict]]:
        """Returns (job_id, None) on success OR (None, error_dict) if rejected."""
        now = time.time()
        if now < self._cooldown_until:
            return None, {
                "error": "cooldown_active",
                "reason": self._cooldown_reason,
                "retry_after_seconds": int(self._cooldown_until - now),
            }

        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id=job_id, params=params)

        with self._lock:
            self._jobs[job_id] = job
        if db.is_available():
            try:
                db.create_job(job_id, params)
            except Exception as e:
                log.error("Failed to persist job creation: %s", e)

        self._pending.put(job_id)
        self._log(job, "INFO", f"Job queued (params keys: {list(params.keys())})")
        return job_id, None

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job:
            return job

        # Not in memory — try DB (for completed jobs after restart)
        if db.is_available():
            row = db.get_job(job_id)
            if row:
                return _row_to_job(row)
        return None

    def list_recent(self, limit: int = 50) -> list[dict]:
        if db.is_available():
            try:
                return db.list_jobs(limit=limit)
            except Exception as e:
                log.warning("DB list_jobs failed, falling back to memory: %s", e)
        with self._lock:
            items = sorted(
                self._jobs.values(),
                key=lambda j: j.created_at,
                reverse=True,
            )[:limit]
        return [_job_to_dict(j) for j in items]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        if job.status in ("done", "error", "cancelled"):
            return False
        job.cancel_flag.set()
        if job.status == "queued":
            self._mark_finished(job, "cancelled")
        self._log(job, "WARN", "Cancel requested")
        return True

    def activate_cooldown(self, seconds: int, reason: str) -> None:
        self._cooldown_until = time.time() + seconds
        self._cooldown_reason = reason
        log.warning("Cooldown activated: %s (%ds)", reason, seconds)

    def cooldown_status(self) -> dict:
        now = time.time()
        remaining = max(0, int(self._cooldown_until - now))
        return {
            "active": remaining > 0,
            "reason": self._cooldown_reason if remaining > 0 else None,
            "seconds_remaining": remaining,
        }

    def queue_stats(self) -> dict:
        with self._lock:
            running = sum(1 for j in self._jobs.values() if j.status == "running")
            queued = sum(1 for j in self._jobs.values() if j.status == "queued")
        return {"length": queued, "running": running}

    # ── Internal ──────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while True:
            try:
                job_id = self._pending.get(timeout=1.0)
            except Empty:
                continue
            job = self._jobs.get(job_id)
            if not job:
                continue
            if job.cancel_flag.is_set():
                self._mark_finished(job, "cancelled")
                continue
            self._run(job)

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.started_at = _now_iso()
        self._persist(job)
        self._log(job, "INFO", "Job started")
        try:
            self._worker(job)
            if job.cancel_flag.is_set():
                self._mark_finished(job, "cancelled")
            else:
                self._mark_finished(job, "done")
        except Exception as e:
            log.exception("Worker crashed for %s", job.job_id)
            job.error_message = str(e)
            self._log(job, "ERROR", f"Worker crashed: {e}")
            self._mark_finished(job, "error")

    def _mark_finished(self, job: Job, status: str) -> None:
        job.status = status
        job.finished_at = _now_iso()
        self._persist(job)
        self._log(job, "INFO", f"Job {status}")

    def _persist(self, job: Job) -> None:
        if not db.is_available():
            return
        try:
            db.update_job(
                job.job_id,
                status=job.status,
                progress_current=job.progress_current,
                progress_total=job.progress_total,
                result_count=job.result_count,
                error_count=job.error_count,
                error_message=job.error_message,
                started_at=job.started_at,
                finished_at=job.finished_at,
            )
        except Exception as e:
            log.error("Persist failed for %s: %s", job.job_id, e)

    def _log(self, job: Job, level: str, msg: str) -> None:
        entry = {
            "index": len(job.logs),
            "time": _now_iso(),
            "level": level,
            "msg": msg,
        }
        job.logs.append(entry)
        log.log(getattr(logging, level, logging.INFO),
                "[%s] %s", job.job_id, msg)


# ── Serialization ─────────────────────────────────────────────────────────────
def _job_to_dict(j: Job) -> dict:
    return {
        "job_id": j.job_id,
        "status": j.status,
        "params": j.params,
        "progress_current": j.progress_current,
        "progress_total": j.progress_total,
        "result_count": j.result_count,
        "error_count": j.error_count,
        "error_message": j.error_message,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "created_at": j.created_at,
    }


def _row_to_job(row: dict) -> Job:
    j = Job(job_id=row["job_id"], params=row.get("params") or {})
    j.status = row["status"]
    j.progress_current = row.get("progress_current") or 0
    j.progress_total = row.get("progress_total") or 0
    j.result_count = row.get("result_count") or 0
    j.error_count = row.get("error_count") or 0
    j.error_message = row.get("error_message")
    j.started_at = row["started_at"].isoformat() if row.get("started_at") else None
    j.finished_at = row["finished_at"].isoformat() if row.get("finished_at") else None
    j.created_at = row["created_at"].isoformat() if row.get("created_at") else _now_iso()
    return j

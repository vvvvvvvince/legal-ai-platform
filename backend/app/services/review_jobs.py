"""Persistent review-job storage primitives.

The store deliberately uses only the Python standard library so the local
deployment stays a three-container Compose stack.  Worker lifecycle code is
added on top of this small, transaction-safe store.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


logger = logging.getLogger(__name__)


JOB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}


class IdempotencyConflict(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ReviewJob:
    job_id: str
    tenant_id: str
    job_type: str
    status: str
    request: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str
    attempt_count: int
    workspace_id: str = "shared"
    created_by_user_id: str = "system-legacy"
    created_by_display_name: str = "Legacy"
    idempotency_key: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    cancel_requested_at: str | None = None


class ReviewJobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed')),
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0
                    ,workspace_id TEXT NOT NULL DEFAULT 'shared'
                    ,created_by_user_id TEXT NOT NULL DEFAULT 'system-legacy'
                    ,created_by_display_name TEXT NOT NULL DEFAULT 'Legacy'
                    ,idempotency_key TEXT
                    ,lease_owner TEXT
                    ,lease_expires_at TEXT
                    ,heartbeat_at TEXT
                    ,cancel_requested_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_review_jobs_queue
                    ON review_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_review_jobs_tenant
                    ON review_jobs(tenant_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_review_jobs_idempotency
                    ON review_jobs(tenant_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(review_jobs)")}
            additions = {
                "workspace_id": "TEXT NOT NULL DEFAULT 'shared'",
                "created_by_user_id": "TEXT NOT NULL DEFAULT 'system-legacy'",
                "created_by_display_name": "TEXT NOT NULL DEFAULT 'Legacy'",
                "idempotency_key": "TEXT",
                "lease_owner": "TEXT",
                "lease_expires_at": "TEXT",
                "heartbeat_at": "TEXT",
                "cancel_requested_at": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE review_jobs ADD COLUMN {name} {definition}")

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> ReviewJob | None:
        if row is None:
            return None
        return ReviewJob(
            job_id=row["job_id"],
            tenant_id=row["tenant_id"],
            job_type=row["job_type"],
            status=row["status"],
            request=json.loads(row["request_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            updated_at=row["updated_at"],
            attempt_count=int(row["attempt_count"]),
            workspace_id=row["workspace_id"] if "workspace_id" in row.keys() else row["tenant_id"],
            created_by_user_id=row["created_by_user_id"] if "created_by_user_id" in row.keys() else "system-legacy",
            created_by_display_name=row["created_by_display_name"] if "created_by_display_name" in row.keys() else "Legacy",
            idempotency_key=row["idempotency_key"] if "idempotency_key" in row.keys() else None,
            lease_owner=row["lease_owner"] if "lease_owner" in row.keys() else None,
            lease_expires_at=row["lease_expires_at"] if "lease_expires_at" in row.keys() else None,
            heartbeat_at=row["heartbeat_at"] if "heartbeat_at" in row.keys() else None,
            cancel_requested_at=row["cancel_requested_at"] if "cancel_requested_at" in row.keys() else None,
        )

    def create_job(
        self,
        *,
        tenant_id: str,
        job_type: str,
        request: dict[str, Any],
        created_by_user_id: str = "system-legacy",
        created_by_display_name: str = "Legacy",
        idempotency_key: str | None = None,
    ) -> ReviewJob:
        now = _utc_now()
        job_id = str(uuid4())
        with self._connect() as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM review_jobs WHERE tenant_id = ? AND idempotency_key = ?",
                    (tenant_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["request_json"] != json.dumps(request, ensure_ascii=False):
                        raise IdempotencyConflict("Idempotency key was already used with a different request.")
                    return self._from_row(existing)  # type: ignore[return-value]
            connection.execute(
                """
                INSERT INTO review_jobs(
                    job_id, tenant_id, job_type, status, request_json,
                    created_at, updated_at, workspace_id, created_by_user_id,
                    created_by_display_name, idempotency_key
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, tenant_id, job_type, json.dumps(request, ensure_ascii=False), now, now,
                 tenant_id, created_by_user_id, created_by_display_name, idempotency_key),
            )
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row)  # type: ignore[return-value]

    def get_job(self, job_id: str, tenant_id: str) -> ReviewJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE job_id = ? AND tenant_id = ?",
                (job_id, tenant_id),
            ).fetchone()
        return self._from_row(row)

    def claim_next_job(self, worker_id: str = "worker", lease_seconds: int = 120) -> ReviewJob | None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expires = (now_dt + timedelta(seconds=max(10, lease_seconds))).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM review_jobs
                WHERE (status = 'queued' AND cancel_requested_at IS NULL)
                   OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                ORDER BY created_at ASC
                LIMIT 1
                """, (now,)).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE review_jobs
                SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?,
                    attempt_count = attempt_count + 1, lease_owner = ?,
                    lease_expires_at = ?, heartbeat_at = ?
                WHERE job_id = ? AND ((status = 'queued' AND cancel_requested_at IS NULL)
                    OR (status = 'running' AND lease_expires_at < ?))
                """,
                (now, now, worker_id, lease_expires, now, row["job_id"], now),
            )
            claimed = connection.execute(
                "SELECT * FROM review_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            connection.commit()
        return self._from_row(claimed)

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool:
        now_dt = datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE review_jobs SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'running' AND lease_owner = ?""",
                ((now_dt + timedelta(seconds=max(10, lease_seconds))).isoformat(), now_dt.isoformat(), now_dt.isoformat(), job_id, worker_id),
            )
        return cursor.rowcount == 1

    def complete_job(self, job_id: str, result: dict[str, Any], worker_id: str = "worker") -> None:
        self._finish_job(job_id, "succeeded", result=result, worker_id=worker_id)

    def fail_job(self, job_id: str, error_message: str, worker_id: str = "worker") -> None:
        self._finish_job(job_id, "failed", error_message=error_message, worker_id=worker_id)

    def _finish_job(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        worker_id: str = "worker",
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError(f"Unsupported terminal status: {status}")
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE review_jobs
                SET status = ?, result_json = ?, error_message = ?,
                    finished_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_message,
                    now,
                    now,
                    job_id, worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Review job {job_id} is not running or does not exist")

    def recover_running_jobs(self) -> int:
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE review_jobs
                SET status = 'queued', started_at = NULL, updated_at = ?
                WHERE status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                """,
                (now, now),
            )
            return cursor.rowcount

    def request_cancel(self, job_id: str, tenant_id: str) -> ReviewJob | None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """UPDATE review_jobs SET cancel_requested_at = ?, updated_at = ?,
                   status = CASE WHEN status = 'queued' THEN 'failed' ELSE status END,
                   error_message = CASE WHEN status = 'queued' THEN '任务已取消。' ELSE error_message END,
                   finished_at = CASE WHEN status = 'queued' THEN ? ELSE finished_at END
                   WHERE job_id = ? AND tenant_id = ? AND status IN ('queued', 'running')""",
                (now, now, now, job_id, tenant_id),
            )
            row = connection.execute("SELECT * FROM review_jobs WHERE job_id = ? AND tenant_id = ?", (job_id, tenant_id)).fetchone()
        return self._from_row(row)

    def is_cancel_requested(self, job_id: str, worker_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested_at FROM review_jobs WHERE job_id = ? AND lease_owner = ?",
                (job_id, worker_id),
            ).fetchone()
        return bool(row and row["cancel_requested_at"])

    def cleanup_expired(self, retention_days: int) -> int:
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM review_jobs
                WHERE status IN ('succeeded', 'failed') AND finished_at < ?
                """,
                (cutoff,),
            )
            return cursor.rowcount



class ReviewJobWorker:
    """Small in-process worker used by the local deployment."""

    def __init__(self, store: ReviewJobStore, review_fn, poll_seconds: float = 1.0, concurrency: int = 1) -> None:
        self.store = store
        self.review_fn = review_fn
        self.poll_seconds = max(0.1, poll_seconds)
        self.concurrency = max(1, concurrency)
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop_event.clear()
        self._threads = [threading.Thread(target=self._run, name=f"review-job-worker-{i}", daemon=True) for i in range(self.concurrency)]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []

    def run_once(self) -> bool:
        worker_id = threading.current_thread().name
        job = self.store.claim_next_job(worker_id=worker_id)
        if job is None:
            return False
        try:
            result = self.review_fn(job.request)
            if self.store.is_cancel_requested(job.job_id, worker_id):
                self.store.fail_job(job.job_id, "任务已取消。", worker_id=worker_id)
            else:
                self.store.complete_job(job.job_id, result, worker_id=worker_id)
        except Exception:
            logger.exception("Review job %s failed", job.job_id)
            self.store.fail_job(
                job.job_id,
                "审查任务执行失败，请检查模型服务后重试。",
                worker_id=worker_id,
            )
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self.run_once():
                self._stop_event.wait(self.poll_seconds)

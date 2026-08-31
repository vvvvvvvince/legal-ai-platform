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


@dataclass(frozen=True)
class ReviewModification:
    modification_id: str
    job_id: str
    tenant_id: str
    status: str
    risk_key: str | None
    payload: dict[str, Any]
    actor_user_id: str
    actor_display_name: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewModificationEvent:
    event_id: str
    modification_id: str
    action: str
    actor_user_id: str
    actor_display_name: str
    created_at: str


@dataclass(frozen=True)
class ModificationSaveResult:
    saved: ReviewModification
    superseded: ReviewModification | None = None


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
            def create_table(name: str) -> None:
                connection.execute(
                    f"""CREATE TABLE IF NOT EXISTS {name} (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
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
                )"""
                )

            create_table("review_jobs")
            table_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review_jobs'"
            ).fetchone()
            table_sql = str(table_sql_row["sql"] or "")
            if "'cancelled'" not in table_sql:
                legacy_columns = [row["name"] for row in connection.execute("PRAGMA table_info(review_jobs)")]
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("ALTER TABLE review_jobs RENAME TO review_jobs_legacy")
                for index_name in ("idx_review_jobs_queue", "idx_review_jobs_tenant", "idx_review_jobs_idempotency"):
                    connection.execute(f"DROP INDEX IF EXISTS {index_name}")
                create_table("review_jobs")
                target_columns = [row["name"] for row in connection.execute("PRAGMA table_info(review_jobs)")]
                defaults = {
                    "workspace_id": "tenant_id",
                    "created_by_user_id": "'system-legacy'",
                    "created_by_display_name": "'Legacy'",
                    "attempt_count": "0",
                }
                select_values = [
                    column if column in legacy_columns else defaults.get(column, "NULL")
                    for column in target_columns
                ]
                connection.execute(
                    f"INSERT INTO review_jobs ({', '.join(target_columns)}) "
                    f"SELECT {', '.join(select_values)} FROM review_jobs_legacy"
                )
                connection.execute("DROP TABLE review_jobs_legacy")
                connection.commit()

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
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_review_jobs_queue
                    ON review_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_review_jobs_tenant
                    ON review_jobs(tenant_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_review_jobs_idempotency
                    ON review_jobs(tenant_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS review_modifications (
                    modification_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES review_jobs(job_id),
                    tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'reverted')),
                    risk_key TEXT,
                    payload_json TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    actor_display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_review_modifications_active_risk
                    ON review_modifications(job_id, risk_key)
                    WHERE status = 'active' AND risk_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_review_modifications_job
                    ON review_modifications(job_id, tenant_id, status, created_at);
                CREATE TABLE IF NOT EXISTS review_modification_events (
                    event_id TEXT PRIMARY KEY,
                    modification_id TEXT NOT NULL REFERENCES review_modifications(modification_id),
                    action TEXT NOT NULL CHECK(action IN ('accepted', 'superseded', 'reverted')),
                    actor_user_id TEXT NOT NULL,
                    actor_display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_review_modification_events_modification
                    ON review_modification_events(modification_id, created_at);
                """
            )

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

    @staticmethod
    def _from_modification_row(row: sqlite3.Row | None) -> ReviewModification | None:
        if row is None:
            return None
        return ReviewModification(
            modification_id=row["modification_id"],
            job_id=row["job_id"],
            tenant_id=row["tenant_id"],
            status=row["status"],
            risk_key=row["risk_key"],
            payload=json.loads(row["payload_json"]),
            actor_user_id=row["actor_user_id"],
            actor_display_name=row["actor_display_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _from_modification_event_row(row: sqlite3.Row) -> ReviewModificationEvent:
        return ReviewModificationEvent(
            event_id=row["event_id"],
            modification_id=row["modification_id"],
            action=row["action"],
            actor_user_id=row["actor_user_id"],
            actor_display_name=row["actor_display_name"],
            created_at=row["created_at"],
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
        request_json = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM review_jobs WHERE tenant_id = ? AND idempotency_key = ?",
                    (tenant_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    existing_request = json.dumps(
                        json.loads(existing["request_json"]),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if existing_request != request_json:
                        connection.rollback()
                        raise IdempotencyConflict("Idempotency key was already used with a different request.")
                    connection.commit()
                    return self._from_row(existing)  # type: ignore[return-value]
            connection.execute(
                """
                INSERT INTO review_jobs(
                    job_id, tenant_id, job_type, status, request_json,
                    created_at, updated_at, workspace_id, created_by_user_id,
                    created_by_display_name, idempotency_key
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, tenant_id, job_type, request_json, now, now,
                 tenant_id, created_by_user_id, created_by_display_name, idempotency_key),
            )
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            connection.commit()
        return self._from_row(row)  # type: ignore[return-value]

    def get_job(self, job_id: str, tenant_id: str) -> ReviewJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE job_id = ? AND tenant_id = ?",
                (job_id, tenant_id),
            ).fetchone()
        return self._from_row(row)

    def list_jobs(self, tenant_id: str, limit: int = 50) -> list[ReviewJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM review_jobs WHERE tenant_id = ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (tenant_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._from_row(row) for row in rows]  # type: ignore[list-item]

    def save_modification(
        self,
        job_id: str,
        tenant_id: str,
        payload: dict[str, Any],
        *,
        actor_user_id: str,
        actor_display_name: str,
    ) -> ModificationSaveResult:
        if not isinstance(payload, dict):
            raise ValueError("modification payload must be an object")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        risk_key = payload.get("risk_key")
        if not isinstance(risk_key, str) or not risk_key.strip():
            risk_key = None
        now = _utc_now()
        modification_id = str(uuid4())
        superseded_modification: ReviewModification | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT 1 FROM review_jobs WHERE job_id = ? AND tenant_id = ?", (job_id, tenant_id)
            ).fetchone()
            if job is None:
                connection.rollback()
                raise KeyError(f"Review job {job_id} does not exist")
            if risk_key is not None:
                previous = connection.execute(
                    "SELECT * FROM review_modifications WHERE job_id = ? AND risk_key = ? AND status = 'active'",
                    (job_id, risk_key),
                ).fetchone()
                if previous is not None:
                    superseded_modification = self._from_modification_row(previous)
                    connection.execute(
                        "UPDATE review_modifications SET status = 'superseded', updated_at = ? WHERE modification_id = ?",
                        (now, previous["modification_id"]),
                    )
                    connection.execute(
                        """INSERT INTO review_modification_events(
                            event_id, modification_id, action, actor_user_id, actor_display_name, created_at
                        ) VALUES (?, ?, 'superseded', ?, ?, ?)""",
                        (str(uuid4()), previous["modification_id"], actor_user_id, actor_display_name, now),
                    )
            connection.execute(
                """INSERT INTO review_modifications(
                    modification_id, job_id, tenant_id, status, risk_key, payload_json,
                    actor_user_id, actor_display_name, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
                (
                    modification_id, job_id, tenant_id, risk_key, payload_json,
                    actor_user_id, actor_display_name, now, now,
                ),
            )
            connection.execute(
                """INSERT INTO review_modification_events(
                    event_id, modification_id, action, actor_user_id, actor_display_name, created_at
                ) VALUES (?, ?, 'accepted', ?, ?, ?)""",
                (str(uuid4()), modification_id, actor_user_id, actor_display_name, now),
            )
            row = connection.execute(
                "SELECT * FROM review_modifications WHERE modification_id = ?", (modification_id,)
            ).fetchone()
            connection.commit()
        return ModificationSaveResult(
            saved=self._from_modification_row(row),  # type: ignore[return-value]
            superseded=superseded_modification,
        )

    def get_modification(self, modification_id: str, tenant_id: str) -> ReviewModification | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_modifications WHERE modification_id = ? AND tenant_id = ?",
                (modification_id, tenant_id),
            ).fetchone()
        return self._from_modification_row(row)

    def list_modifications(self, job_id: str, tenant_id: str) -> list[ReviewModification]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM review_modifications
                   WHERE job_id = ? AND tenant_id = ? AND status = 'active'
                   ORDER BY created_at ASC""",
                (job_id, tenant_id),
            ).fetchall()
        return [self._from_modification_row(row) for row in rows]  # type: ignore[list-item]

    def revert_modification(
        self,
        modification_id: str,
        tenant_id: str,
        *,
        job_id: str,
        actor_user_id: str,
        actor_display_name: str,
    ) -> ReviewModification | None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE review_modifications SET status = 'reverted', updated_at = ?
                   WHERE modification_id = ? AND job_id = ? AND tenant_id = ? AND status = 'active'""",
                (now, modification_id, job_id, tenant_id),
            )
            if cursor.rowcount != 1:
                connection.commit()
                return None
            connection.execute(
                """INSERT INTO review_modification_events(
                    event_id, modification_id, action, actor_user_id, actor_display_name, created_at
                ) VALUES (?, ?, 'reverted', ?, ?, ?)""",
                (str(uuid4()), modification_id, actor_user_id, actor_display_name, now),
            )
            row = connection.execute(
                "SELECT * FROM review_modifications WHERE modification_id = ?", (modification_id,)
            ).fetchone()
            connection.commit()
        return self._from_modification_row(row)

    def list_modification_events(self, modification_id: str, tenant_id: str) -> list[ReviewModificationEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT event.* FROM review_modification_events AS event
                   JOIN review_modifications AS modification ON modification.modification_id = event.modification_id
                   WHERE event.modification_id = ? AND modification.tenant_id = ?
                   ORDER BY event.created_at ASC""",
                (modification_id, tenant_id),
            ).fetchall()
        return [self._from_modification_event_row(row) for row in rows]

    def claim_next_job(self, worker_id: str = "worker", lease_seconds: float = 120) -> ReviewJob | None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_expires = (now_dt + timedelta(seconds=max(0.1, lease_seconds))).isoformat()
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

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float = 120) -> bool:
        now_dt = datetime.now(timezone.utc)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE review_jobs SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'running' AND lease_owner = ?""",
                ((now_dt + timedelta(seconds=max(0.1, lease_seconds))).isoformat(), now_dt.isoformat(), now_dt.isoformat(), job_id, worker_id),
            )
        return cursor.rowcount == 1

    def complete_job(self, job_id: str, result: dict[str, Any], worker_id: str = "worker") -> None:
        self._finish_job(job_id, "succeeded", result=result, worker_id=worker_id)

    def fail_job(self, job_id: str, error_message: str, worker_id: str = "worker") -> None:
        self._finish_job(job_id, "failed", error_message=error_message, worker_id=worker_id)

    def cancel_running_job(self, job_id: str, worker_id: str) -> None:
        self._finish_job(job_id, "cancelled", error_message="任务已取消。", worker_id=worker_id)

    def _finish_job(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        worker_id: str = "worker",
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled"}:
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
                   status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
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
                WHERE status IN ('succeeded', 'failed', 'cancelled') AND finished_at < ?
                """,
                (cutoff,),
            )
            return cursor.rowcount



class ReviewJobWorker:
    """Small in-process worker used by the local deployment."""

    def __init__(
        self,
        store: ReviewJobStore,
        review_fn,
        poll_seconds: float = 1.0,
        concurrency: int = 1,
        lease_seconds: float = 120,
        heartbeat_interval: float = 30,
    ) -> None:
        self.store = store
        self.review_fn = review_fn
        self.poll_seconds = max(0.1, poll_seconds)
        self.concurrency = max(1, concurrency)
        self.lease_seconds = max(0.1, lease_seconds)
        self.heartbeat_interval = max(0.05, heartbeat_interval)
        if self.heartbeat_interval >= self.lease_seconds:
            raise ValueError("heartbeat_interval must be shorter than lease_seconds")
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
        job = self.store.claim_next_job(worker_id=worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        heartbeat_stop = threading.Event()

        def renew_lease() -> None:
            while not heartbeat_stop.wait(self.heartbeat_interval):
                if not self.store.heartbeat(job.job_id, worker_id, self.lease_seconds):
                    return

        heartbeat_thread = threading.Thread(
            target=renew_lease,
            name=f"{worker_id}-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = self.review_fn(job.request)
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=self.heartbeat_interval + 0.1)
            if self.store.is_cancel_requested(job.job_id, worker_id):
                self.store.cancel_running_job(job.job_id, worker_id=worker_id)
            else:
                self.store.complete_job(job.job_id, result, worker_id=worker_id)
        except Exception:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=self.heartbeat_interval + 0.1)
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

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

from app.services.review_jobs import IdempotencyConflict, ReviewJobStore


def test_store_creates_and_filters_by_tenant(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")

    job = store.create_job(
        tenant_id="acme",
        job_type="deep_review",
        request={"filename": "a.docx"},
    )

    assert job.status == "queued"
    assert store.get_job(job.job_id, "acme").request["filename"] == "a.docx"
    assert store.get_job(job.job_id, "other") is None


def test_store_transitions_and_recovers_running_jobs(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="local", job_type="deep_review", request={})

    claimed = store.claim_next_job()
    assert claimed.job_id == job.job_id
    assert claimed.status == "running"

    assert store.recover_running_jobs() == 0
    assert store.get_job(job.job_id, "local").status == "running"

    with store._connect() as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job.job_id,),
        )

    assert store.recover_running_jobs() == 1
    assert store.get_job(job.job_id, "local").status == "queued"

    claimed = store.claim_next_job()
    store.complete_job(claimed.job_id, {"review_status": "complete"})
    completed = store.get_job(job.job_id, "local")
    assert completed.status == "succeeded"
    assert completed.result["review_status"] == "complete"


def test_store_cleanup_removes_only_old_terminal_jobs(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    old = store.create_job(tenant_id="local", job_type="deep_review", request={})
    assert store.claim_next_job().job_id == old.job_id
    store.complete_job(old.job_id, {})

    removed = store.cleanup_expired(retention_days=0)

    assert removed == 1
    assert store.get_job(old.job_id, "local") is None


def test_store_deduplicates_idempotent_submission(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    first = store.create_job(tenant_id="shared", job_type="deep_review", request={"v": 1}, idempotency_key="k1")
    second = store.create_job(tenant_id="shared", job_type="deep_review", request={"v": 1}, idempotency_key="k1")
    assert second.job_id == first.job_id
    try:
        store.create_job(tenant_id="shared", job_type="deep_review", request={"v": 2}, idempotency_key="k1")
    except IdempotencyConflict:
        pass
    else:
        raise AssertionError("changed payload must reject reused idempotency key")


def test_expired_lease_can_be_reclaimed_by_another_worker(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="shared", job_type="deep_review", request={})
    first = store.claim_next_job("worker-a", lease_seconds=10)
    assert first and first.job_id == job.job_id
    with store._connect() as connection:
        connection.execute(
            "UPDATE review_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job.job_id,),
        )
    second = store.claim_next_job("worker-b", lease_seconds=10)
    assert second and second.lease_owner == "worker-b"
    assert store.heartbeat(job.job_id, "worker-a") is False


def test_concurrent_idempotent_submissions_return_one_job(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    participants = 12
    barrier = threading.Barrier(participants)

    def submit_once(_index):
        barrier.wait(timeout=2)
        return store.create_job(
            tenant_id="shared",
            job_type="deep_review",
            request={"value": 1},
            idempotency_key="same-key",
        ).job_id

    with ThreadPoolExecutor(max_workers=participants) as executor:
        job_ids = list(executor.map(submit_once, range(participants)))

    assert len(set(job_ids)) == 1


def test_queued_cancellation_uses_cancelled_terminal_state(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="shared", job_type="deep_review", request={})

    cancelled = store.request_cancel(job.job_id, "shared")

    assert cancelled.status == "cancelled"
    assert cancelled.error == "任务已取消。"


def test_old_status_constraint_is_migrated_without_losing_jobs(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE review_jobs (
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
            )"""
        )
        connection.execute(
            "INSERT INTO review_jobs(job_id, tenant_id, job_type, status, request_json, created_at, updated_at) VALUES ('legacy', 'shared', 'deep_review', 'queued', '{}', '2026-01-01', '2026-01-01')"
        )

    store = ReviewJobStore(path)
    cancelled = store.request_cancel("legacy", "shared")

    assert cancelled.status == "cancelled"
    assert store.get_job("legacy", "shared") is not None


def test_modifications_are_scoped_to_job_and_keep_audited_authors(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="shared", job_type="deep_review", request={})

    first = store.save_modification(
        job.job_id,
        "shared",
        {"risk_key": "payment", "original": "先付款", "modified": "验收后付款"},
        actor_user_id="user-a",
        actor_display_name="甲同事",
    )
    replacement = store.save_modification(
        job.job_id,
        "shared",
        {"risk_key": "payment", "original": "先付款", "modified": "验收后 30 日付款"},
        actor_user_id="user-b",
        actor_display_name="乙同事",
    )

    active = store.list_modifications(job.job_id, "shared")
    assert [item.modification_id for item in active] == [replacement.modification_id]
    assert active[0].actor_display_name == "乙同事"
    assert store.get_modification(first.modification_id, "shared").status == "superseded"

    reverted = store.revert_modification(
        replacement.modification_id,
        "shared",
        job_id=job.job_id,
        actor_user_id="user-a",
        actor_display_name="甲同事",
    )
    assert reverted and reverted.status == "reverted"
    assert store.list_modifications(job.job_id, "shared") == []
    assert [event.action for event in store.list_modification_events(replacement.modification_id, "shared")] == ["accepted", "reverted"]
    assert store.list_modifications(job.job_id, "other") == []


def test_revert_does_not_cross_review_job_boundaries(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    first_job = store.create_job(tenant_id="shared", job_type="deep_review", request={})
    second_job = store.create_job(tenant_id="shared", job_type="deep_review", request={})
    modification = store.save_modification(
        second_job.job_id,
        "shared",
        {"original": "先付款", "modified": "验收后付款"},
        actor_user_id="user-a",
        actor_display_name="甲同事",
    )

    reverted = store.revert_modification(
        modification.modification_id,
        "shared",
        job_id=first_job.job_id,
        actor_user_id="user-a",
        actor_display_name="甲同事",
    )

    assert reverted is None
    assert store.get_modification(modification.modification_id, "shared").status == "active"

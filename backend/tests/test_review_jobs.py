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

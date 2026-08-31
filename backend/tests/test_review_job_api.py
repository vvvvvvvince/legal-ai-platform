import threading
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services.auth_store import AuthStore
from app.services.review_jobs import ReviewJobStore, ReviewJobWorker


def _payload():
    return {
        "filename": "contract.docx",
        "contract_text": "甲方应按期付款。",
        "settings": {"party_role": "party_a"},
    }


def test_app_uses_lifespan_context_instead_of_legacy_events():
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []
    assert app.router.lifespan_context.__name__ == "review_job_lifespan"


def test_create_review_job_returns_202_and_tenant_filters_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEW_JOB_DB", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("REVIEW_JOB_WORKER_ENABLED", "false")

    with TestClient(app) as client:
        response = client.post(
            "/api/review-jobs",
            headers={"X-Tenant-ID": "acme"},
            json=_payload(),
        )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["job_type"] == "deep_review"

        same_tenant = client.get(
            f"/api/review-jobs/{body['job_id']}",
            headers={"X-Tenant-ID": "acme"},
        )
        assert same_tenant.status_code == 200
        assert same_tenant.json()["status"] == "queued"

        other_tenant = client.get(
            f"/api/review-jobs/{body['job_id']}",
            headers={"X-Tenant-ID": "other"},
        )
        assert other_tenant.status_code == 404


def test_shared_workspace_modifications_keep_the_user_who_made_each_change(monkeypatch, tmp_path):
    auth_db = tmp_path / "auth.sqlite3"
    job_db = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("AUTH_DB", str(auth_db))
    monkeypatch.setenv("REVIEW_JOB_DB", str(job_db))
    monkeypatch.setenv("REVIEW_JOB_WORKER_ENABLED", "false")
    auth = AuthStore(auth_db)
    auth.create_user("alice", "甲同事", "correct-password")
    auth.create_user("bob", "乙同事", "correct-password")

    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "alice", "password": "correct-password"}).status_code == 200
        job = client.post("/api/review-jobs", json=_payload()).json()
        first = client.post(
            f"/api/review-jobs/{job['job_id']}/modifications",
            json={"risk_key": "payment", "original": "先付款", "modified": "验收后付款"},
        )
        assert first.status_code == 201
        assert first.json()["actor_display_name"] == "甲同事"

        client.post("/api/auth/logout")
        assert client.post("/api/auth/login", json={"username": "bob", "password": "correct-password"}).status_code == 200
        shared = client.get(f"/api/review-jobs/{job['job_id']}/modifications")
        assert shared.status_code == 200
        assert shared.json()[0]["actor_display_name"] == "甲同事"

        replacement = client.post(
            f"/api/review-jobs/{job['job_id']}/modifications",
            json={"risk_key": "payment", "original": "先付款", "modified": "验收后 30 日付款"},
        )
        assert replacement.status_code == 201
        assert replacement.json()["actor_display_name"] == "乙同事"
        assert client.post(
            f"/api/review-jobs/{job['job_id']}/modifications/{replacement.json()['modification_id']}/revert"
        ).status_code == 200
        assert client.get(f"/api/review-jobs/{job['job_id']}/modifications").json() == []


def test_worker_persists_successful_result(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="local", job_type="deep_review", request={"value": 7})
    worker = ReviewJobWorker(store, lambda request: {"value": request["value"] * 2})

    assert worker.run_once() is True

    result = store.get_job(job.job_id, "local")
    assert result.status == "succeeded"
    assert result.result == {"value": 14}


def test_worker_persists_safe_failure(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="local", job_type="deep_review", request={})
    worker = ReviewJobWorker(store, lambda _request: (_ for _ in ()).throw(RuntimeError("secret")))

    assert worker.run_once() is True

    result = store.get_job(job.job_id, "local")
    assert result.status == "failed"
    assert result.error == "审查任务执行失败，请检查模型服务后重试。"


def test_worker_renews_lease_while_review_is_running(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="shared", job_type="deep_review", request={})
    started = threading.Event()
    release = threading.Event()

    def slow_review(_request):
        started.set()
        release.wait(timeout=2)
        return {"ok": True}

    worker = ReviewJobWorker(
        store,
        slow_review,
        lease_seconds=0.2,
        heartbeat_interval=0.05,
    )
    thread = threading.Thread(target=worker.run_once, name="worker-a")
    thread.start()
    assert started.wait(timeout=1)
    time.sleep(0.35)

    assert store.claim_next_job("worker-b", lease_seconds=0.2) is None

    release.set()
    thread.join(timeout=2)
    assert store.get_job(job.job_id, "shared").status == "succeeded"


def test_running_cancellation_discards_model_result(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="shared", job_type="deep_review", request={})
    started = threading.Event()
    release = threading.Event()

    def slow_review(_request):
        started.set()
        release.wait(timeout=2)
        return {"must_not_publish": True}

    worker = ReviewJobWorker(store, slow_review, lease_seconds=1, heartbeat_interval=0.1)
    thread = threading.Thread(target=worker.run_once, name="worker-cancel")
    thread.start()
    assert started.wait(timeout=1)
    store.request_cancel(job.job_id, "shared")
    release.set()
    thread.join(timeout=2)

    cancelled = store.get_job(job.job_id, "shared")
    assert cancelled.status == "cancelled"
    assert cancelled.result is None

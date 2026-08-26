# Review Jobs and Frontend Modularization Implementation Plan

> For agentic workers: use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax.

**Goal:** Add resumable SQLite-backed asynchronous deep-review jobs and reduce frontend/src/App.tsx to a composition root without changing visible behavior.

**Architecture:** Add a SQLite review_jobs store, a single in-process worker, and additive POST /api/review-jobs plus GET /api/review-jobs/{id}. Extract frontend DTOs, API calls, pure transforms, workflow polling, and feature components; keep /api/review/deep for compatibility.

**Tech Stack:** FastAPI, Python sqlite3, Pydantic, React 18, TypeScript, Vite, Node node:test, Docker Compose volumes.

**Spec:** docs/superpowers/specs/2026-08-26-review-jobs-and-frontend-modularization-design.md

## Global Constraints

- No Redis, Celery, PostgreSQL, WebSocket, Kubernetes, or global state library.
- Preserve existing UI, CSS classes, copy, export behavior, PDF behavior, and synchronous endpoints.
- Store extracted text and structured result only; never persist original binaries or credentials.
- Enforce tenant identity on both job creation and lookup.
- Each task ends with focused tests and an atomic commit.

---

### Task 1: Persistent review-job store

**Files:** Create backend/app/services/review_jobs.py and backend/tests/test_review_jobs.py.

**Interfaces:** ReviewJobStore(path), create_job(tenant_id, job_type, request), get_job(job_id, tenant_id), claim_next_job(), complete_job(job_id, result), fail_job(job_id, error), recover_running_jobs(), cleanup_expired(retention_days).

- [ ] Step 1: Write a failing test.
~~~python
def test_store_creates_and_filters_by_tenant(tmp_path):
    store = ReviewJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(tenant_id="acme", job_type="deep_review", request={"filename": "a.docx"})
    assert job.status == "queued"
    assert store.get_job(job.job_id, "acme").request["filename"] == "a.docx"
    assert store.get_job(job.job_id, "other") is None
~~~
- [ ] Step 2: Run backend/.venv/Scripts/python.exe -m pytest backend/tests/test_review_jobs.py -q and confirm the missing-symbol failure.
- [ ] Step 3: Implement idempotent schema creation, JSON serialization, UTC timestamps, and BEGIN IMMEDIATE atomic claims.
- [ ] Step 4: Run the focused store tests and verify create/read/claim/complete/fail/recovery/cleanup.
- [ ] Step 5: Commit with git add backend/app/services/review_jobs.py backend/tests/test_review_jobs.py and message feat: add persistent review job store.

### Task 2: Async API and worker

**Files:** Modify backend/app/main.py and backend/app/services/review_jobs.py; create backend/tests/test_review_job_api.py.

**Interfaces:** POST /api/review-jobs returns 202 and a job summary; GET /api/review-jobs/{job_id} returns tenant-filtered status/result; ReviewJobWorker(store, review_fn) executes queued jobs.

- [ ] Step 1: Write a failing API test.
~~~python
def test_create_review_job_returns_202(client, monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEW_JOB_DB", str(tmp_path / "jobs.sqlite3"))
    response = client.post("/api/review-jobs", json={
        "filename": "a.docx", "contract_text": "合同", "settings": {}
    })
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
~~~
- [ ] Step 2: Run backend/.venv/Scripts/python.exe -m pytest backend/tests/test_review_job_api.py -q and confirm the route is absent.
- [ ] Step 3: Implement startup initialization/recovery/cleanup, one worker, safe failure persistence, and validated ReviewResponse result persistence.
- [ ] Step 4: Run backend/.venv/Scripts/python.exe -m pytest backend/tests/test_review_job_api.py backend/tests/test_deep_review.py -q.
- [ ] Step 5: Commit with message feat: add asynchronous review job API.

### Task 3: Docker persistence and recovery

**Files:** Modify docker-compose.yml, backend/.env.example, and README.md.

- [ ] Step 1: Add this failing assertion to backend/tests/test_review_job_api.py.
~~~python
def test_job_database_config_is_exposed(monkeypatch):
    monkeypatch.setenv("REVIEW_JOB_DB", "/app/data/review_jobs.sqlite3")
    monkeypatch.setenv("REVIEW_JOB_RETENTION_DAYS", "7")
    assert job_runtime_config() == {
        "path": "/app/data/review_jobs.sqlite3",
        "retention_days": 7,
    }
~~~
- [ ] Step 2: Run backend/.venv/Scripts/python.exe -m pytest backend/tests/test_review_job_api.py -q and confirm the missing configuration helper failure.
- [ ] Step 3: Add backend_data:/app/data, REVIEW_JOB_DB=/app/data/review_jobs.sqlite3, worker concurrency, and retention settings.
- [ ] Step 4: Run docker compose config --quiet and the startup-recovery test.
- [ ] Step 5: Commit with message chore: persist review jobs in compose volume.

### Task 4: Frontend API and domain extraction

**Files:** Create frontend/src/domain/reviewTypes.ts, frontend/src/domain/reviewTransforms.ts, frontend/src/api/legalApi.ts, frontend/src/api/reviewJobs.ts, and frontend/tests/review-job-utils.test.mjs; modify frontend/src/App.tsx.

- [ ] Step 1: Write a failing Node test.
~~~javascript
test("normalizes a succeeded review job", () => {
  const job = normalizeReviewJob({ job_id: "j1", status: "succeeded", result: { risks: [] } });
  assert.equal(job.status, "succeeded");
  assert.deepEqual(job.result, { risks: [] });
});
~~~
- [ ] Step 2: Run npm.cmd --prefix frontend run test and confirm the missing export failure.
- [ ] Step 3: Move shared DTOs, response normalization, pure editor transforms, and all fetch calls into the new modules.
- [ ] Step 4: Run frontend tests and npm.cmd --prefix frontend run build.
- [ ] Step 5: Commit with message refactor: extract frontend API and domain modules.

### Task 5: Workflow hook, feature components, and async UI

**Files:** Create frontend/src/hooks/useReviewWorkflow.ts, frontend/src/features/intake/IntakePanel.tsx, frontend/src/features/review/ReviewPanel.tsx, frontend/src/features/editor/EditorPanel.tsx; modify frontend/src/App.tsx and frontend/src/api/reviewJobs.ts.

- [ ] Step 1: Write a failing polling test.
~~~javascript
test("polling stops at terminal states", () => {
  assert.equal(shouldPollReviewJob("queued"), true);
  assert.equal(shouldPollReviewJob("running"), true);
  assert.equal(shouldPollReviewJob("succeeded"), false);
  assert.equal(shouldPollReviewJob("failed"), false);
});
~~~
- [ ] Step 2: Run the test and confirm the missing workflow export failure.
- [ ] Step 3: Extract the hook and presentational components without changing CSS classes or visible behavior.
- [ ] Step 4: Store job ID/session in localStorage, poll every two seconds, recover after refresh, and switch runDeepReview to create/poll jobs.
- [ ] Step 5: Run frontend tests and build; commit with message feat: connect frontend to resumable review jobs.

### Task 6: Full verification

- [ ] Run backend/.venv/Scripts/python.exe -m pytest backend -q.
- [ ] Run npm.cmd --prefix frontend run test and npm.cmd --prefix frontend run build.
- [ ] Run docker compose up -d --build; verify backend/frontend healthy and the job database exists in the named volume.
- [ ] Submit a job, observe queued/running, retrieve terminal result, and verify cross-tenant lookup returns 404.
- [ ] Run git diff --check and git status --short --branch; ensure only intentional files changed.
- [ ] Commit any final scoped fix atomically and report exact evidence.

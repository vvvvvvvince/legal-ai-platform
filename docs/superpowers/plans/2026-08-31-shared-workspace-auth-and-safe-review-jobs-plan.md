# Shared Workspace Authentication and Safe Review Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace caller-supplied identity with two-user shared-workspace sessions and make SQLite review jobs safe for concurrent workers, recovery, cancellation, and duplicate submissions.

**Architecture:** Keep the FastAPI/React modular monolith and single-host SQLite deployment. Add an authentication/session store in `/app/data`, bind every request to a fixed shared workspace and user, and add renewable job leases with atomic claims and idempotency keys.

**Tech Stack:** FastAPI, Pydantic, Python `sqlite3`, `hashlib.scrypt`, React 18, TypeScript, Node tests, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-31-shared-workspace-auth-and-safe-review-jobs-design.md`

## Global Constraints

- Both users use `workspace_id=shared`; they can read the same contracts, jobs, and results.
- Browser authentication uses HttpOnly SameSite cookies; no reusable API token is returned to JavaScript.
- Passwords are provisioned interactively and stored only as salted scrypt hashes.
- Existing review result, editing, and export schemas remain compatible.
- Existing queued and terminal jobs are migrated additively; no data is deleted.
- Job completion is accepted only from the current lease owner.
- No Redis, PostgreSQL, Celery, OIDC, or multi-host execution is added.
- Every implementation step starts with a failing test and ends with a focused passing test.

---

### Task 1: Shared authentication store and password verification

**Files:**
- Create: `backend/app/services/auth_store.py`
- Modify: `backend/app/services/request_auth.py`
- Test: `backend/tests/test_auth_store.py`

**Interfaces:**
- `AuthStore(path: str | Path)` initializes `users`, `sessions`, and `login_attempts` tables.
- `AuthStore.create_user(username, display_name, password, workspace_id="shared") -> UserIdentity`.
- `AuthStore.authenticate(username, password) -> UserIdentity | None`.
- `AuthStore.create_session(user_id, lifetime_seconds) -> str`.
- `AuthStore.get_identity(raw_token) -> UserIdentity | None`.
- `AuthStore.revoke_session(raw_token) -> None`.
- `require_request_identity(request: Request) -> RequestIdentity` reads the session cookie and returns the authenticated actor.

- [ ] **Step 1: Write the failing tests**

```python
def test_password_hash_only_authenticates_the_original_password(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("alice", "Alice", "correct-password")
    assert store.authenticate("alice", "correct-password").user_id == user.user_id
    assert store.authenticate("alice", "wrong-password") is None

def test_session_round_trip_returns_shared_workspace_identity(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("alice", "Alice", "secret")
    token = store.create_session(user.user_id, 3600)
    assert store.get_identity(token).workspace_id == "shared"
    store.revoke_session(token)
    assert store.get_identity(token) is None
```

- [ ] **Step 2: Run `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_auth_store.py -q` and confirm the missing-symbol failure.**
- [ ] **Step 3: Implement scrypt hashing, constant-time verification, token hashing, expiry, revocation, and transactional schema creation.**
- [ ] **Step 4: Run the focused auth-store tests and verify both tests pass.**
- [ ] **Step 5: Commit `feat: add shared workspace authentication store`.**

### Task 2: Login/session API and request identity migration

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/request_auth.py`
- Modify: `backend/.env.example`
- Test: `backend/tests/test_auth_api.py`

**Interfaces:**
- `POST /api/auth/login` accepts `{username, password}`, sets `legal_ai_session`, and returns `{user_id, username, display_name, workspace_id}`.
- `GET /api/auth/session` returns the same metadata or `401`.
- `POST /api/auth/logout` revokes and clears the cookie.
- Business routes use the session-derived `RequestIdentity` and reject tenant/user headers.

- [ ] **Step 1: Write failing API tests for login, session, logout, missing cookie, and spoofed `X-Tenant-ID`.**

```python
def test_login_sets_cookie_and_session_returns_identity(client, auth_db):
    provision_user(auth_db, "alice", "Alice", "secret")
    login = client.post("/api/auth/login", json={"username": "alice", "password": "secret"})
    assert login.status_code == 200
    assert "legal_ai_session=" in login.headers["set-cookie"]
    assert client.get("/api/auth/session").json()["workspace_id"] == "shared"

def test_business_route_rejects_missing_session_and_ignores_spoofed_headers(client):
    response = client.get("/api/system-status", headers={"X-Tenant-ID": "other"})
    assert response.status_code == 401
```

- [ ] **Step 2: Run `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_auth_api.py -q` and confirm the routes/auth dependency are absent or fail.**
- [ ] **Step 3: Add auth routes, startup bootstrap validation, cookie settings, and a single session-based dependency.**
- [ ] **Step 4: Replace business-route identity calls and feedback actor capture with `RequestIdentity`; keep development startup explicit about required users.**
- [ ] **Step 5: Run auth API tests plus existing `test_p0_controls.py` and verify spoofed headers cannot select identity.**
- [ ] **Step 6: Commit `feat: enforce session authentication for business APIs`.**

### Task 3: Frontend login and cookie-only API client

**Files:**
- Create: `frontend/src/api/authApi.ts`
- Create: `frontend/src/hooks/useAuth.ts`
- Modify: `frontend/src/api/legalApi.ts`
- Modify: `frontend/src/api/reviewJobs.ts`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/scripts/run-auth-tests.mjs`

**Interfaces:**
- `login(username, password) -> Promise<SessionIdentity>`.
- `getSession() -> Promise<SessionIdentity | null>`.
- `logout() -> Promise<void>`.
- The shared API client uses `credentials: "same-origin"` and emits neither `X-Tenant-ID` nor `X-API-Token`.

- [ ] **Step 1: Add a failing Node test asserting API requests omit identity headers and normalize a session response.**
- [ ] **Step 2: Run `npm.cmd --prefix frontend test` and verify the new assertions fail against the current header implementation.**
- [ ] **Step 3: Implement auth API, login state, a compact login screen, and centralized cookie-based request headers.**
- [ ] **Step 4: Handle `401` by clearing client state and returning to login while leaving server jobs intact.**
- [ ] **Step 5: Run frontend tests and `npm.cmd --prefix frontend run build`.**
- [ ] **Step 6: Commit `feat: add two-user session login`.**

### Task 4: Add actor/workspace/idempotency fields to review jobs

**Files:**
- Modify: `backend/app/services/review_jobs.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_review_jobs.py`

**Interfaces:**
- `create_job(workspace_id, created_by_user_id, created_by_display_name, job_type, request, idempotency_key) -> ReviewJob`.
- Duplicate `(workspace_id, idempotency_key)` with the same request returns the original job; changed payload raises `IdempotencyConflict`.
- Existing rows migrate to `workspace_id="shared"` and actor `system-legacy`.

- [ ] **Step 1: Write failing tests for actor persistence, duplicate idempotency, payload conflict, and additive migration.**
- [ ] **Step 2: Run `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_review_jobs.py -q` and confirm the new arguments/columns are missing.**
- [ ] **Step 3: Implement schema migration using `PRAGMA table_info`, unique workspace/key index, and stable JSON request fingerprints.**
- [ ] **Step 4: Update job creation and feedback callers to pass the authenticated identity.**
- [ ] **Step 5: Run focused job tests and existing review-job API tests.**
- [ ] **Step 6: Commit `feat: attribute review jobs to authenticated users`.**

### Task 5: Lease-based worker, heartbeat, cancellation, and concurrency

**Files:**
- Modify: `backend/app/services/review_jobs.py`
- Modify: `backend/app/main.py`
- Modify: `backend/.env.example`
- Test: `backend/tests/test_review_jobs.py`
- Test: `backend/tests/test_review_job_api.py`

**Interfaces:**
- `claim_next_job(worker_id, lease_seconds) -> ReviewJob | None` atomically claims queued or expired jobs.
- `heartbeat(job_id, worker_id, lease_seconds) -> bool` renews only the current lease.
- `complete_job(job_id, worker_id, result)` and `fail_job(job_id, worker_id, error)` reject stale owners.
- `request_cancel(job_id, workspace_id) -> ReviewJob` transitions queued jobs immediately and marks running jobs for cancellation.
- `recover_expired_jobs()` only requeues jobs whose lease has expired.

- [ ] **Step 1: Write failing tests for two-worker exclusivity, stale-owner rejection, expired-lease recovery, queued cancellation, running cancellation-before-publish, and effective concurrency.**
- [ ] **Step 2: Run the focused job tests and confirm the current worker has no lease/cancellation behavior.**
- [ ] **Step 3: Add lease columns, atomic claim/update predicates, and `cancelled` status.**
- [ ] **Step 4: Run the review function through a configurable `ThreadPoolExecutor`; each worker renews its lease during model execution and checks cancellation before persistence.**
- [ ] **Step 5: Replace startup `recover_running_jobs()` with expiry-based recovery and schedule retention cleanup independently of startup.**
- [ ] **Step 6: Add `POST /api/review-jobs/{job_id}/cancel` with shared-workspace authorization and safe responses.**
- [ ] **Step 7: Run focused worker/API tests plus existing deep-review tests.**
- [ ] **Step 8: Commit `feat: make review jobs lease-safe and cancellable`.**

### Task 6: Connect idempotency and cancellation to the frontend workflow

**Files:**
- Modify: `frontend/src/api/reviewJobs.ts`
- Modify: `frontend/src/hooks/useReviewWorkflow.ts`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/scripts/run-review-utils-tests.mjs`

- [ ] **Step 1: Add failing tests for stable submission keys, cancellation requests, terminal `cancelled` state, and bounded polling.**
- [ ] **Step 2: Run `npm.cmd --prefix frontend test` and confirm the new workflow assertions fail.**
- [ ] **Step 3: Generate one UUID idempotency key per submission, send it as `Idempotency-Key`, and persist it with the active job.**
- [ ] **Step 4: Add `cancelReviewJob()` and call it from the existing stop control; ensure abort listeners are removed and polling has a maximum timeout/backoff.**
- [ ] **Step 5: Run frontend tests and production build.**
- [ ] **Step 6: Commit `feat: connect review cancellation and idempotency`.**

### Task 7: Bootstrap, deployment, migration, and verification

**Files:**
- Create: `backend/scripts/bootstrap_users.py`
- Modify: `docker-compose.yml`
- Modify: `backend/.env.example`
- Modify: `README.md`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_official_law_ingest.py`

- [ ] **Step 1: Write failing configuration/migration tests for `AUTH_DB`, session cookie security, two-user bootstrap, and hermetic law-ingest fixtures.**
- [ ] **Step 2: Run the focused tests and record failures.**
- [ ] **Step 3: Add the interactive bootstrap script, persist `/app/data`, configure worker lease/concurrency settings, and document the exact provisioning command.**
- [ ] **Step 4: Replace the personal absolute law-library path with a repository fixture or an explicit skip when the corpus is unavailable.**
- [ ] **Step 5: Run `docker compose config --quiet`, the full backend suite, frontend tests, frontend build, and `git diff --check`.**
- [ ] **Step 6: Perform a local smoke test: login as both users, submit one idempotent job, observe queued/running/completed, cancel one job, refresh, and verify both users see the shared result with distinct actor attribution.**
- [ ] **Step 7: Commit `chore: document and verify shared auth deployment`.**

## Verification Gate

Before claiming completion, all of these must be true:

- Anonymous business requests return `401`.
- Spoofed tenant/user headers do not change the server identity.
- Two users can log in and access the same workspace.
- Jobs and feedback contain trusted actor identity.
- Duplicate idempotency keys create one job.
- Two workers cannot publish the same unexpired lease.
- Expired jobs recover; actively leased jobs are not reset on startup.
- Queued and running cancellation end in `cancelled` without publishing a result.
- Backend tests, frontend tests, frontend build, Compose config, and diff checks pass.


# Review Jobs and Frontend Modularization Design

**Status:** Proposed for review  
**Date:** 2026-08-26

## Goal

Make contract review resumable across page refreshes and backend restarts, while reducing the frontend's single-file coupling without changing the current user-visible workflow or visual design.

## Scope

This design covers two coordinated changes:

1. Extract the current `frontend/src/App.tsx` responsibilities into focused API, domain, hook, and feature modules.
2. Add a persistent asynchronous deep-review job workflow backed by SQLite and an in-process worker.

The existing synchronous review endpoints, DOCX export behavior, PDF parsing behavior, risk-location logic, and visual styling remain compatible during the migration.

## Non-goals

- No Redis, Celery, PostgreSQL, WebSocket, or Kubernetes dependency in this increment.
- No redesign of the current UI or copy.
- No pause/cancel/priority controls for jobs.
- No storage of original uploaded binary files.
- No change to model, Qdrant, or tenant-isolation policy beyond carrying the validated tenant ID into the job record and execution context.

## Architecture

The application remains a modular FastAPI monolith and React SPA.

```text
React SPA
  ├─ API client and DTO normalization
  ├─ review workflow hook
  └─ intake / editor / review feature components
          │
          ├─ POST /api/review-jobs       -> 202 + job summary
          └─ GET  /api/review-jobs/{id}  -> status/result
                         │
                         ▼
                 FastAPI job service
                         │
             SQLite review_jobs database
                         │
                 in-process worker
                         │
                 review_contract_deeply()
```

The synchronous `/api/review/deep` route is retained as a compatibility path. The current frontend deep-review action migrates to the job API after the backend contract is available.

## Backend design

### Job record

SQLite stores one row per review job. The database path is configurable through `REVIEW_JOB_DB` and defaults to `/app/data/review_jobs.sqlite3` in the container.

Columns:

- `job_id`: UUID text primary key.
- `tenant_id`: validated request tenant, defaulting to `local` in development.
- `job_type`: currently `deep_review`.
- `status`: `queued`, `running`, `succeeded`, or `failed`.
- `request_json`: serialized filename, extracted contract text, deep-review settings, and document quality.
- `result_json`: serialized `ReviewResponse` when successful.
- `error_message`: safe user-facing failure text when failed; no API keys or raw stack traces.
- `created_at`, `started_at`, `finished_at`, `updated_at`: UTC ISO timestamps.
- `attempt_count`: integer, incremented whenever the worker claims a job.

The database is initialized idempotently at application startup. A cleanup pass removes completed or failed rows older than `REVIEW_JOB_RETENTION_DAYS` (default seven days). Queued and running rows are never removed by retention cleanup.

### Worker semantics

- The worker starts during FastAPI startup and stops during shutdown.
- It claims at most one job at a time by default (`REVIEW_JOB_WORKER_CONCURRENCY=1`).
- Claiming uses a SQLite transaction so two worker loops cannot claim the same queued row.
- A job left in `running` state after a process restart is returned to `queued` during startup recovery.
- Execution is at-least-once: a process crash after the model call and before result commit may retry the model call.
- The worker calls the existing `review_contract_deeply()` service and persists the returned Pydantic model as JSON.
- All model, parsing, and validation exceptions become a `failed` job with a safe error category. Detailed diagnostics remain in the existing local audit log.

### API contract

`POST /api/review-jobs`

- Requires the same `X-API-Token` and `X-Tenant-ID` validation as `/api/review/deep`.
- Accepts the existing `DeepReviewRequest` JSON body.
- Returns HTTP `202` with:

```json
{
  "job_id": "uuid",
  "job_type": "deep_review",
  "status": "queued",
  "created_at": "2026-08-26T00:00:00+00:00",
  "updated_at": "2026-08-26T00:00:00+00:00"
}
```

`GET /api/review-jobs/{job_id}`

- Requires the same request identity headers.
- Returns the job summary for the same tenant.
- Returns `404` for an unknown ID or a job belonging to another tenant.
- Includes `result` only for `succeeded` jobs.
- Includes `error` only for `failed` jobs.

The API must never allow a caller to select or override another tenant's job. The job's tenant is taken from the authenticated request identity, not from the JSON body.

### Persistence and container deployment

Compose adds a named `backend_data` volume mounted at `/app/data`. The backend receives `REVIEW_JOB_DB=/app/data/review_jobs.sqlite3`. No credentials or original upload binaries are stored in this volume.

The existing audit and feedback JSONL paths remain unchanged in this increment; making those logs durable is a follow-up task.

## Frontend design

`App.tsx` becomes a composition root. Existing CSS classes and rendered markup are preserved wherever practical.

Target modules:

- `src/domain/reviewTypes.ts`: shared frontend DTO and workflow types.
- `src/domain/reviewTransforms.ts`: response normalization and deterministic editor transforms currently embedded in `App.tsx`.
- `src/api/legalApi.ts`: overview, intake chat, legal research, export, feedback, and synchronous compatibility calls.
- `src/api/reviewJobs.ts`: create/poll job calls and job DTOs.
- `src/hooks/useReviewWorkflow.ts`: session epoch, upload/intake/deep-review state, polling, resume, and error transitions.
- `src/features/intake/`: overview and intake chat presentation.
- `src/features/review/`: risk, coverage, preflight, and deep-review result presentation.
- `src/features/editor/`: Tiptap editor, location selection, auto-applied modifications, and export controls.

The first extraction keeps `App.tsx` responsible for layout composition and passes explicit props/callbacks to feature components. No global state library is added.

### Job polling and resume behavior

- After `POST /api/review-jobs`, the hook stores the `job_id` in `localStorage` together with the current filename and a workflow session identifier.
- While the job is `queued` or `running`, it polls every two seconds.
- Polling stops on `succeeded`, `failed`, component unmount, or a newer workflow session.
- On page load, a stored job ID is queried once; if it belongs to the current local session and is unfinished, polling resumes.
- A successful result is normalized through the same code path as the existing synchronous result and then opens the existing modification stage.
- A failed job leaves the upload and intake state intact and offers the existing retry path.
- A stale or `404` job is removed from local storage without affecting the selected file.

## Error and safety rules

- Contract text and settings are stored only in the local SQLite volume to support restart recovery; original binaries are never persisted.
- API responses expose safe error messages only. Stack traces and provider responses stay in local audit logs.
- The frontend treats `result` as untrusted JSON and uses the existing normalization guards before rendering or editing.
- The worker never marks a job `succeeded` unless the result passes the existing `ReviewResponse` validation and deep-review completion checks.
- A job that cannot be safely located back to contract text remains manually reviewable, matching current behavior.
- Authentication and tenant checks are enforced on both create and read endpoints.

## Testing strategy

### Backend

- Unit tests for SQLite schema initialization, create/read, status transitions, tenant filtering, retention cleanup, and startup recovery.
- Service tests for worker claim exclusivity and conversion of success/failure into persisted job records.
- API tests for `202` creation, status polling, completed result, failed result, missing job, and cross-tenant rejection.
- Existing deep-review and review API tests must remain green.

### Frontend

- Node tests for DTO normalization, job status transitions, polling stop conditions, stale-job cleanup, and local-storage resume decisions.
- Existing review utility tests remain green.
- TypeScript production build remains required.
- Browser smoke verification covers upload/intake, starting a job, visible queued/running state, completion into the modification workspace, and export controls.

## Rollout sequence

1. Add backend job store and API tests, without changing the frontend.
2. Add worker lifecycle and Compose persistence volume; verify recovery with a controlled test.
3. Add frontend API client and workflow hook tests.
4. Extract presentational and domain modules while keeping behavior unchanged.
5. Switch the deep-review button to create/poll jobs and verify the end-to-end browser flow.
6. Keep `/api/review/deep` available for compatibility and rollback.

## Acceptance criteria

- A deep review submitted through the UI returns immediately to a queued/running state.
- Refreshing the page does not lose an in-progress job; the result can be recovered.
- Restarting backend leaves queued/running jobs recoverable from SQLite.
- A completed result opens the same modification and export workspace as before.
- A tenant cannot read another tenant's job.
- `App.tsx` is reduced to composition/orchestration rather than containing all API, DTO, editor, and review logic.
- Backend tests, frontend tests, TypeScript build, and browser smoke checks pass.

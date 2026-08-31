# Shared Workspace Authentication and Safe Review Jobs Design

**Status:** Proposed for review  
**Date:** 2026-08-31

## Goal

Remove the two P0 architecture risks without adding infrastructure that is disproportionate for a two-user internal deployment:

1. Replace the browser-exposed shared API token and caller-supplied tenant identity with server-authenticated user sessions.
2. Make persistent review jobs safe when more than one API process or worker thread is running.

Both users belong to one shared workspace and can see the same contracts, jobs, and results. Their individual identities must remain stable so later annotation and document-edit history can attribute every change to the correct person.

## Scope

This increment covers:

- Two fixed user accounts with password login.
- Server-side sessions carried by an HttpOnly cookie.
- One shared workspace with user-level actor identity.
- User attribution on review jobs and review feedback.
- SQLite job leases, heartbeats, cancellation intent, idempotent creation, and configurable local concurrency.
- Migration of the existing frontend API client from `X-API-Token` and `X-Tenant-ID` to session cookies.
- Compatibility migration for existing SQLite review-job data.

## Non-goals

- No OIDC provider, SSO, public registration, password reset email, or user-administration UI.
- No PostgreSQL, Redis, Celery, Kubernetes, or multi-host job execution.
- No per-user data isolation; both users intentionally share one workspace.
- No annotation or document-edit history UI in this increment. The identity and audit foundation for that later feature is included.
- No service-token or machine-to-machine authentication in this increment.
- No guaranteed interruption of an already-running external model HTTP request. Cancellation prevents queued work from starting and prevents a cancelled running job from publishing a successful result after the provider call returns.

## Selected Architecture

The application remains a FastAPI modular monolith and React SPA deployed on one host.

```text
Browser
  -> POST /api/auth/login
  <- HttpOnly session cookie
  -> authenticated API requests
          |
          v
FastAPI identity dependency
  -> user + shared workspace
          |
          +-> review/feedback APIs record actor identity
          |
          +-> SQLite review_jobs queue
                  |
                  +-> N in-process worker threads
                      using renewable leases
```

SQLite remains appropriate for two users and a single host. The design removes unsafe global recovery and adds lease ownership so multiple API processes or worker threads cannot legitimately own the same job at the same time.

## Authentication and Identity

### Users

The authentication database uses `AUTH_DB=/app/data/auth.sqlite3` in the persistent `/app/data` volume. A `users` table contains:

- `user_id`: stable opaque identifier.
- `username`: unique normalized login name.
- `display_name`: name shown in future annotations and audit history.
- `password_hash`: versioned scrypt password hash with a per-user random salt.
- `workspace_id`: fixed to `shared` in this deployment.
- `is_active`: disables access without deleting attribution history.
- `created_at` and `updated_at`: UTC timestamps.

The two accounts are provisioned through an explicit interactive bootstrap command executed inside the backend container. Password input is hidden by the terminal; plaintext passwords are never accepted as command-line arguments, committed, logged, or retained in the database.

### Sessions

A `sessions` table contains only a hash of the random session token:

- `session_id`: UUID.
- `token_hash`: SHA-256 hash of a cryptographically random token.
- `user_id`: owning user.
- `created_at`, `expires_at`, and `last_seen_at`.
- `revoked_at`: logout or administrative invalidation.

The raw token is returned only in a cookie with these properties:

- `HttpOnly`.
- `SameSite=Lax`.
- `Secure` when `APP_ENV=production` or `SESSION_COOKIE_SECURE=true`.
- Explicit configurable lifetime, defaulting to eight hours.
- Path `/`.

State-changing API requests require the same-origin session cookie. The production deployment must not enable wildcard credentialed CORS. Because the frontend and API are served through the same Nginx origin, no cross-origin browser authentication is required.

### API endpoints

- `POST /api/auth/login`: validates username/password, rotates into a new session, sets the cookie, and returns safe user metadata.
- `GET /api/auth/session`: returns the current user and workspace or `401`.
- `POST /api/auth/logout`: revokes the current session and clears the cookie.

All business endpoints use one `require_request_identity()` dependency returning:

```text
RequestIdentity(user_id, username, display_name, workspace_id)
```

The browser can no longer select a tenant or user through headers. `VITE_API_AUTH_TOKEN` and `VITE_TENANT_ID` are removed from browser runtime behavior.

Legacy `X-API-Token` and `X-Tenant-ID` behavior is removed from business endpoints when session authentication is enabled. There is no compatibility flag that permits the browser to select an identity. Existing integrations must migrate to a user session before enforcement is enabled.

## Shared Workspace and Actor Attribution

Both accounts use `workspace_id=shared`. Authorization checks compare persisted `workspace_id` with the authenticated identity, so both users can access the same job while unauthenticated callers cannot.

Review jobs add:

- `workspace_id`.
- `created_by_user_id`.
- `created_by_display_name` as an immutable audit snapshot.

Feedback records use the authenticated identity rather than raw request headers. The audit event shape used by future annotations is:

```json
{
  "workspace_id": "shared",
  "actor_user_id": "...",
  "actor_display_name": "...",
  "action": "...",
  "resource_type": "...",
  "resource_id": "...",
  "before": null,
  "after": null,
  "created_at": "UTC timestamp"
}
```

This increment does not add editor actions to that log, but it guarantees that all future changes can reuse a trusted actor identity without redesigning authentication.

## Safe Review-Job Semantics

### Schema additions

`review_jobs` adds:

- `workspace_id` and creator fields.
- `idempotency_key`, unique within a workspace when non-null.
- `lease_owner` and `lease_expires_at`.
- `heartbeat_at`.
- `cancel_requested_at` and `cancelled_at`.
- Status `cancelled` in addition to existing states.

Existing rows are migrated idempotently to `workspace_id=shared`; their creator is recorded as a legacy system actor. Migration is additive and does not delete queued or completed jobs.

### Creation and idempotency

`POST /api/review-jobs` accepts an `Idempotency-Key` header generated once per frontend submission. Repeating the same key in the shared workspace returns the original job instead of creating another model call. A key cannot be reused with a different request payload; that returns `409`.

### Atomic claim

A worker claims one of these jobs in a single `BEGIN IMMEDIATE` transaction:

- A `queued` job with no cancellation request.
- A `running` job whose lease has expired.

The transaction writes a unique `lease_owner`, an expiry timestamp, `heartbeat_at`, and increments `attempt_count`. Completion or failure succeeds only when the caller still owns the lease.

There is no startup operation that resets every running job. Recovery is based exclusively on lease expiry.

### Heartbeat and expiry

Each running worker renews its lease periodically while the model call is active. Defaults:

- Lease duration: 120 seconds.
- Heartbeat interval: 30 seconds.
- Worker concurrency: 2.

Configuration validation rejects an interval that is not safely below the lease duration. If the process dies, another worker can reclaim the job after expiry. The model call remains at-least-once after a hard crash; the idempotency key prevents duplicate submission, while lease ownership prevents two healthy workers from publishing the same result.

### Cancellation

`POST /api/review-jobs/{job_id}/cancel` requires an authenticated shared-workspace user.

- A queued job becomes `cancelled` immediately.
- A running job receives `cancel_requested_at`.
- The worker checks cancellation before starting and again before persisting a result.
- If cancellation was requested during an external model call, the returned result is discarded and the job becomes `cancelled`.

The UI may stop polling independently, but it must call this endpoint when the user explicitly cancels the review.

### Retry policy

This increment automatically reclaims only jobs abandoned by an expired lease. Provider or validation failures remain `failed`; they are not automatically retried because repeated legal-model calls can incur cost and produce different advice. A later explicit retry endpoint can create a new job linked to the failed job.

## Frontend Changes

The SPA adds a small login screen and an authentication hook:

- On load, call `GET /api/auth/session`.
- Render the existing application only after a valid session is confirmed.
- Send API requests with same-origin cookies; centralize request behavior in one API client.
- On `401`, clear client workflow state and return to login without deleting server jobs.
- Display the authenticated user's `display_name` in a compact account control.
- Generate and persist one idempotency key for each deep-review submission.
- Add backend cancellation to the existing stop behavior for review jobs.

The existing visual design, contract-review stages, result schema, and export behavior remain unchanged.

## Security and Error Handling

- Login failures return one generic error and use constant-time password verification.
- Repeated failed logins are rate-limited per normalized username and client address using a small SQLite table in `AUTH_DB`, so the limit is shared across API processes and survives a restart.
- Session and password material is never logged.
- Authentication failures are `401`; disabled users are treated as unauthenticated.
- Job lookup continues to return `404` for resources outside the authenticated workspace.
- Lease conflicts do not expose internal owner IDs to clients.
- Database migrations run transactionally and fail startup rather than partially applying.
- Production startup validates that authentication is configured and the two active users exist.

## Testing Strategy

### Authentication

- Password hashes are salted and verify only the correct password.
- Login sets a secure HttpOnly cookie and does not return a token in JSON.
- Session lookup, logout, expiry, revocation, and disabled users behave correctly.
- Spoofed `X-Tenant-ID` and `X-API-Token` headers cannot change identity.
- Both users can read jobs in the shared workspace, while anonymous requests receive `401`.
- Feedback and new jobs record the authenticated actor.

### Jobs

- Idempotency returns one job for duplicate submissions and rejects payload mismatch.
- Two workers cannot hold an unexpired lease on one job.
- Startup does not reset an unexpired running job.
- An expired lease is reclaimable.
- A stale owner cannot complete or fail a reclaimed job.
- Heartbeats renew only the current owner's lease.
- Queued and running cancellation reach `cancelled` correctly.
- Configured concurrency starts the requested number of worker loops.

### Frontend and deployment

- Authentication state and `401` transitions have deterministic tests.
- API calls no longer emit tenant or browser API-token headers.
- Idempotency survives page refresh while a submission is active.
- Frontend tests and production build pass.
- Focused backend tests and the hermetic backend suite pass.
- Docker Compose smoke testing verifies login, shared job visibility, concurrent claiming, restart recovery, cancellation, and logout.

## Migration and Rollout

1. Back up the existing `/app/data/review_jobs.sqlite3` volume.
2. Add authentication/session storage and the bootstrap-user command.
3. Provision the two users and verify login before enforcing authentication.
4. Add request identity to business APIs and migrate the frontend to cookies.
5. Apply the additive job-schema migration and leased claim semantics.
6. Enable concurrency 2 and cancellation/idempotency in the frontend.
7. Disable legacy shared-token identity.
8. Run end-to-end verification, then remove the temporary migration flag from deployed configuration.

Rollback keeps the database backup and container image from before migration. Because the schema migration is additive, rolling back the application does not require deleting new columns; however, jobs created with new statuses must be drained or archived before an older binary is restored.

## Acceptance Criteria

- Neither browser source nor browser storage contains a reusable API credential.
- A request cannot choose its user or workspace using headers or request JSON.
- Both configured users can log in and see the same review jobs and results.
- Every new job and feedback record has a trusted actor identity.
- Duplicate submission with one idempotency key causes at most one queued job.
- Multiple workers do not concurrently publish results for the same valid lease.
- Restarting another API process does not reset an actively leased job.
- Abandoned jobs recover only after lease expiry.
- Users can cancel queued jobs and prevent a cancelled running job from publishing a result.
- Worker concurrency configuration is effective and defaults to two.
- Existing review, editing, and export behavior remains compatible.
- Focused security/job tests, frontend tests, production build, and Docker smoke checks pass.

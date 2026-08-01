# Runtime and storage specification

## Components

```text
GitLab Webhook
       |
       v
FastAPI API ---- SQLite Store ---- Worker poller
                                      |
                                      +-- Policy loader
                                      +-- GitLab adapter
                                      +-- Git worktree
                                      +-- Agent CLI
                                      +-- Notifiers
```

The API must acknowledge Webhooks quickly. The Worker owns all potentially slow operations: Pipeline polling, Git operations, Agent execution, GitLab writes and notifications.

## Review Run state

The first version uses:

```text
pending -> running -> succeeded
       ^          \-> failed
       |          \-> cancelled
       +-- deferred Pipeline Gate or Draft check
```

The Store must atomically claim an eligible pending Run so two Workers cannot execute the same Run. A Run is unique by `(project_id, mr_iid, sha, kind)`. A deferred Run has a `next_attempt_at` and does not block later eligible Runs.

Each running Run has a lease expiration and opaque claim token. A Worker clears the lease when it finishes or defers the Run. If a Worker dies, the next claim operation recovers an expired Run to `pending`; after three claims without a terminal update, the Store marks it `failed` with a lease-expiry diagnostic. Ordinary Agent non-zero exits are terminal failures and are not automatically retried.

## Required storage records

`review_runs` must retain at least:

- numeric run ID;
- Project ID, MR IID and exact SHA;
- `kind` (`normal` or future event kinds);
- status and timestamps;
- diagnostic/error summary;
- `next_attempt_at` for deferred work;
- `lease_until`, `attempts` and `claim_token` for crash recovery and stale-claim protection.

`auto_fix_mrs` must retain the parent MR, parent Run, Auto-fix Project/MR, status and `merged_sha`.
An `auto_fix_merge_events` table retains an Auto-fix merge event that arrives before its relation is written, so the later relation write can apply the merge suppression atomically.

## Deployment defaults

- SQLite is the first storage implementation.
- One API process and one Worker are the reference deployment.
- Database polling is the first queue implementation.
- Postgres and Redis are future implementations behind interfaces, not v0.1 dependencies.
- Webhook secret, GitLab token, Review Bot credentials and channel secrets are runtime inputs.

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
                  \-> failed
                  \-> cancelled
```

The Store must atomically claim a pending Run so two Workers cannot execute the same Run. A Run is unique by `(project_id, mr_iid, sha, kind)`.

## Required storage records

`review_runs` must retain at least:

- numeric run ID;
- Project ID, MR IID and exact SHA;
- `kind` (`normal` or future event kinds);
- status and timestamps;
- diagnostic/error summary in the next schema iteration.

`auto_fix_mrs` must retain the parent MR, parent Run, Auto-fix Project/MR, status and `merged_sha`.

## Deployment defaults

- SQLite is the first storage implementation.
- One API process and one Worker are the reference deployment.
- Database polling is the first queue implementation.
- Postgres and Redis are future implementations behind interfaces, not v0.1 dependencies.
- Webhook secret, GitLab token, Review Bot credentials and channel secrets are runtime inputs.

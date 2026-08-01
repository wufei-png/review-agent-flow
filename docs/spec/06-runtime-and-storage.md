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

The Store must atomically claim an eligible pending Run so two Workers cannot claim the same Run at once. A Run is unique by `(project_id, mr_iid, sha, kind)`. A deferred Run has a `next_attempt_at` and does not block later eligible Runs. This is at-least-once execution: a stale lease may cause a logical Run to execute again, so external side effects must use stable markers/coordination rather than an exactly-once claim guarantee.

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

`auto_fix_intents` must retain the parent MR/SHA/Run, generated source and target branches, generated title and the optional child MR identity before Agent execution. A terminal Run that has been reconciled without a child MR changes the intent to `abandoned`; only `planned` and `created` intents remain attachable. The child MR description must carry the exact `ReviewFlow-Auto-Fix-Run-ID:<run-id> SHA:<parent-sha>` marker before a branch-shaped webhook can attach an intent. `auto_fix_mrs` must retain the parent MR, parent Run, Auto-fix Project/MR, status and `merged_sha`.
An `auto_fix_merge_events` table retains an Auto-fix merge event that arrives before its relation is written, so the later relation write can apply the merge suppression atomically.

`notification_outbox` must retain one record per notification/channel with the correlation ID, rendered event fields, channel identity, attempt count, lease, next-attempt time, final status and last error. `pending -> running -> delivered` or `failed` is independent of the Review Run state.

`worktree_cleanup` must retain each created worktree path, Run, SHA, cleanup attempts/lease and terminal cleanup status. A cleanup failure is retried independently and must not re-run the Agent.

The reference implementation uses SQLite schema version 2. This version is a fresh pre-release schema; there is no backward-compatible migration of old channel indexes or old records.

## Deployment defaults

- SQLite is the first storage implementation.
- One API process and one Worker are the reference deployment.
- Database polling is the first queue implementation.
- One API/Worker process is bound to exactly one GitLab Project; the API requires a webhook secret and Project ID.
- The repository is a persistent writable non-shallow checkout/mirror. The Worker explicitly ensures a requested SHA with a locked, bounded fetch before worktree creation.
- Postgres and Redis are future implementations behind interfaces, not v0.1 dependencies.
- Webhook secret, bound Project ID, GitLab token, Review Bot credentials, repository remote access and channel secrets are runtime inputs.

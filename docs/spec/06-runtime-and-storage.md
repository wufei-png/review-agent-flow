# Runtime and storage specification

## Components

```text
GitLab Webhook
       |
       v
FastAPI API -------- SQLite Store
                           ^
                           |
                 supervised Worker process
                    |       |       |
                    |       |       +-- Reconciliation/Cleanup loop
                    |       +---------- Notification loop
                    +------------------ Review/Publication loop
```

The API acknowledges Webhooks quickly. One supervised Worker OS process runs three independent loops: Review/Publication, Notification and Reconciliation/Cleanup. Each loop isolates per-item errors and uses independent claims/backoff so a stuck notification does not block reviews. Unexpected death of any loop terminates the Worker process; the process supervisor restarts the whole Worker. The Agent remains a child process. Separate Dispatcher/Reconciler OS processes are deferred until operational evidence requires them.

## Review Run and effect lifecycle

A Review Run is unique by `(project_id, mr_iid, sha)` and has no kind discriminator. Its normal lifecycle is:

```text
pending -> running -> succeeded
       ^          \-> failed
       |          \-> cancelled
       +-- deferred Pipeline Gate or Draft check
```

The Store atomically claims eligible work with an opaque token and lease. Expired work is recovered with bounded attempts; stale tokens cannot heartbeat, finish or mutate a reclaimed item. Transient GitLab, rate-limit and transport failures use capped backoff. Configuration, policy, invalid SHA and Agent contract failures are terminal. A head change cancels or supersedes the old key and enqueues the new exact SHA without publishing old effects.

Note, Auto-fix and notification state is independent from process success. Requested Outputs, the Effective Run Decision and Output Expectations are frozen before effects proceed. User-visible Review Outcome is a projection over those durable expectations and receipts, not a synonym for Agent exit status.

## Fresh SQLite schema

The pre-release implementation performs a hard cutover to a fresh schema; it does not migrate or read legacy ReviewRun kinds, Run-ID markers or Agent-owned publication state. The schema retains at least:

- `review_runs`: ReviewKey, status, attempts, lease/token, timestamps and redacted diagnostics;
- `requested_outputs`, `effective_run_decisions` and `output_expectations`: immutable decisions used by the Outcome projection;
- `note_publication_requests` and effect receipts: canonical marker, state, attempt, trusted external identity and deletion observation;
- `auto_fix_publication_intents`: immutable generation and nonce, with at most one active generation per ReviewKey;
- `auto_fix_publication_plans`: full canonical Plan, digest, verification evidence and publication state;
- `auto_fix_relations`: parent identity, child identity, observed state, divergence and merge SHA;
- `notification_requests` and delivery outbox: immutable event identity and generation, rendered fields, channel, attempts, lease and status;
- `review_run_failures`: failure class, retryability, claim attempt and redacted diagnostic;
- `worktree_cleanup`: workspace path, Run/SHA, attempts, lease and cleanup status;
- `operator_recoveries`: recovery ID, action, exact supplied identities, reason, principal, host, tool version, result and redacted diagnostic.

Auto-fix retries reuse the active Intent generation and nonce. An explicit re-arm closes the prior generation, creates a new immutable generation/nonce and retains history. A child MR webhook can reference only an existing local Intent or Relation.

## Immutable payloads and retention

Before any Auto-fix GitLab write, the Store persists the complete immutable Publication Plan, including base SHA, ordered content/actions and modes, branch, marker template, commit message, title, target, readiness and CI behavior. `plan_sha256` is the digest of canonical UTF-8 JSON for those independent inputs, excluding the derived digest value and final rendered marker. The Store freezes that payload, digest and digest-bearing marker together. A pending or uncertain plan remains recoverable without recapturing an Agent workspace.

The Operator may configure retention for sensitive review, diff and notification payloads. After successful publication or explicit abandonment, ReviewFlow may purge content while retaining the digest, stable identities, decision, timestamps and audit record. Purge never changes the meaning of a completed receipt.

A deployment that requires complete Store-loss recovery must retain the canonical Publication Plan as trusted backup evidence before payload purge; the digest or GitLab marker alone is deliberately insufficient authority. The recovery CLI consumes that retained canonical JSON but never writes it back into the active Store after successful reconstruction.

## Deployment defaults

- SQLite and database polling are the first storage/queue implementation.
- One API process and one supervised Worker process are the reference deployment.
- One deployment is bound to exactly one GitLab Project.
- The repository is a persistent writable non-shallow checkout or mirror. The Worker obtains a requested SHA through a locked, bounded fetch before creating a detached worktree.
- Agent, verification and publication credentials are distinct Operator-owned inputs; the Agent receives no GitLab publication token.
- Postgres, Redis and separately deployed Worker loops are future implementations behind interfaces.

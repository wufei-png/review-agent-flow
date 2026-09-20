# Runtime and storage specification

## Components

```mermaid
flowchart LR
    GitLab["GitLab Webhooks and API"]
    Supervisor["Process supervisor"]
    Store[("SQLite Store<br/>only shared directory")]

    subgraph APIService["FastAPI service - separate OS process and user"]
        API["Validate and acknowledge Webhooks"]
    end

    subgraph WorkerService["Supervised Worker - one OS process and separate user"]
        Worker["Fail-fast Worker process"]
        Review["Review/Publication loop<br/>Agent, Note, and Auto-fix"]
        Notify["Notification loop<br/>Feishu and WeCom deliveries"]
        Reconcile["Reconciliation/Cleanup loop<br/>remote recovery and worktrees"]
        Retention["Retention sweep<br/>at startup and hourly"]
        Worker -->|"independent loop"| Review
        Worker -->|"independent loop"| Notify
        Worker -->|"independent loop"| Reconcile
        Reconcile --> Retention
    end

    Private["Worker-private repository, worktrees,<br/>Plan backups, and credentials"]
    Agent["Agent child process<br/>no GitLab publication token"]
    Providers["Feishu and WeCom"]

    Supervisor --> API
    Supervisor -->|"restart after any loop exits unexpectedly"| Worker
    GitLab -->|"bound-Project Webhook"| API
    API -->|"enqueue or wake only"| Store

    Store <-->|"independent claim, lease, and backoff"| Review
    Store <-->|"independent claim, lease, and backoff"| Notify
    Store <-->|"independent claim, lease, and backoff"| Reconcile

    Review <--> Private
    Review --> Agent
    Agent -->|"strict Result Path and optional diff"| Private
    Review -->|"Program-owned Note, commit, and child MR"| GitLab
    Reconcile -->|"exact-identity observation only"| GitLab
    Notify --> Providers
```

The API acknowledges Webhooks quickly. One supervised Worker OS process runs three independent loops: Review/Publication, Notification and Reconciliation/Cleanup. The third loop also runs the startup/hourly Retention sweep. Before either the supervised service or CLI `--once` claims work, it must atomically own the Store's singleton runtime lease. A separate heartbeat renews it during long Agent and verifier calls. A second process fails to start while the lease is valid; after expiry, a new token fences the old owner from heartbeat or release. Loss of ownership stops new loop claims and fails the process. The stored owner, heartbeat and expiry are safe for read-only operator diagnostics; the token is never exposed. Each loop isolates per-item errors and uses independent claims/backoff so a stuck notification does not block reviews. Unexpected death of any loop terminates the Worker process; the process supervisor restarts the whole Worker. The Agent remains a child process. Separate Dispatcher/Reconciler OS processes are deferred until operational evidence requires them.

## Review Run and effect lifecycle

A Review Run is unique by `(project_id, mr_iid, sha)` and has no kind discriminator. Its normal lifecycle is:

```text
pending -> running -> succeeded
       ^          \-> failed
       |          \-> cancelled
       +-- deferred Pipeline Gate or Draft check
```

The Store atomically claims eligible work with an opaque token and lease. Expired work is recovered with a dedicated bounded lease-recovery counter; stale tokens cannot heartbeat, finish or mutate a reclaimed item. Transient GitLab, rate-limit and transport failures use capped backoff and a separate failure-attempt counter. Claim count is diagnostic and eligibility deferrals do not consume either failure budget. Configuration, policy, invalid SHA and Agent contract failures are terminal. A head change cancels or supersedes the old key and enqueues the new exact SHA without publishing old effects. All accepted Git SHAs are canonicalized to lowercase before they participate in identity or marker construction.

Note, Auto-fix and notification state is independent from process success. Requested Outputs, the Effective Run Decision and Output Expectations are frozen before effects proceed. User-visible Review Outcome is a projection over those durable expectations and receipts, not a synonym for Agent exit status. A `superseded` effect was safely suppressed and contributes no delivery; an all-superseded ReviewKey has `no_effect`. An outbox `delivered` state records provider acceptance, not recipient visibility, and at-least-once delivery may duplicate messages.

The Project-bound Run status query reads persisted Note request, Auto-fix Plan and Relation, and notification delivery facts without claiming work or contacting GitLab. It exposes only selected identity, state, attempt, schedule, lease-validity and redacted diagnostic fields. Its finite `next_action` values are hints to wait for the Worker, wait for reconciliation of an uncertain write, or inspect a blocked/divergent effect. Notification `delivered` is labeled server acceptance with possible duplicates; the query never asserts recipient visibility.

The local `ops-status` CLI opens SQLite read-only and reports bound-Project Run backlog, oldest pending age, expired Run leases, Note blocked/uncertain counts, Auto-fix reconciliation/blocked counts, notification pending/running/failed counts and oldest pending age, and Relation divergence. The singleton Worker owner/heartbeat/expiry and last persisted retention purge are deployment-wide facts. A purge receipt proves only that the purge transaction completed; the overall last sweep result, including Plan backup work or failed attempts, is unavailable without a durable source. An absent database is unavailable rather than an empty healthy queue.

## Fresh SQLite schema

The pre-release implementation uses schema 17 and performs a hard cutover to a fresh database; schema 16 is rejected, not migrated. It does not read legacy ReviewRun kinds, Run-ID markers or Agent-owned publication state. The schema retains at least:

- `review_runs`: ReviewKey, status, claim/failure/lease-recovery counters, lease/token, timestamps and redacted diagnostics;
- `agent_task_snapshots`: immutable per-Run MR presentation, exact GitLab base/start/head diff refs, and frozen bounded Skill source/content/digest used to render `AgentTaskV1`;
- `requested_outputs`, `effective_run_decisions` and `output_expectations`: immutable decisions used by the Outcome projection;
- `note_publication_requests` and effect receipts: canonical marker, state, attempt, trusted external identity and deletion observation;
- `auto_fix_publication_intents`: immutable generation and nonce, with at most one active generation per ReviewKey;
- `auto_fix_publication_plans`: full canonical Plan, digest, verification evidence and publication state;
- `auto_fix_relations`: parent identity, child identity, observed state, divergence and merge SHA;
- `notification_requests` and delivery outbox: immutable event identity and generation, rendered fields, channel, non-sensitive payload digest, attempts, lease, status and stable terminal time;
- `worker_runtime_lease`: singleton Worker owner, opaque claim token, heartbeat time and expiry;
- `review_run_failures`: failure class, retryability, diagnostic claim number and redacted diagnostic;
- `worktree_cleanup`: workspace path, Run/SHA, attempts, lease and cleanup status;
- `operator_recoveries`: recovery ID, action, exact supplied identities, reason, principal, host, tool version, result and redacted diagnostic.
- `operator_plan_actions`: immutable Plan rearm/abandon action ID, Plan digest, prior/resulting state, write phase, reason, principal, host, tool version, result and redacted rejection diagnostic.

Auto-fix retries reuse the active Intent generation and nonce. Explicit Plan rearm preserves the same immutable payload, digest, generation and nonce: it resets a blocked attempt budget and resumes normal publication only from `preflight`; any Plan that crossed an external-write boundary resumes reconciliation only. New candidate content requires a new Intent rather than rearm. A child MR webhook can reference only an existing local Intent or Relation.

## Immutable payloads and retention

Before any Auto-fix GitLab write, the Store persists the complete immutable Publication Plan, including base SHA, ordered content/actions and modes, branch, marker template, commit message, title, target, readiness and CI behavior. `plan_sha256` is the digest of canonical UTF-8 JSON for those independent inputs, excluding the derived digest value and final rendered marker. The Store freezes that payload, digest and digest-bearing marker together. Publisher, reconciler and explicit recovery parse the same strict typed `PublicationPlanV1` contract instead of maintaining separate identity and digest validators. A pending or uncertain plan remains recoverable without recapturing an Agent workspace.

Operator configuration v4 sets sensitive review, diff, notification and diagnostic retention from 1 to 3650 days, defaulting to 30. The supervised Worker sweeps at startup and hourly. It purges only terminal payloads and retains digests, stable identities, decisions, timestamps, expectations, receipts, Relations and audits. Terminal Agent Task snapshots retain Project/MR identity and exact diff SHAs but purge title and branch presentation. A Note receives a stable retention timestamp on its first terminal publication outcome; successful reconciliation cannot postpone expiry. A purged published Note stops exact-content reconciliation because its original body is intentionally unavailable; its last confirmed identity and state remain historical evidence. A notification delivery sets `terminal_at` once when delivered or finally failed, including exhausted lease recovery; retries have no terminal time. Its payload expires from that time, not enqueue time. A multi-channel Auto-fix notification request expires only after every delivery is terminal and the latest terminal time exceeds retention. A request with no deliveries expires from its creation time. Notification presentation, recipients and channels retain only a digest after purge, so recovery-ID replay still rejects changed inputs. Purge never changes the meaning of a completed receipt.

A Publication Plan remains in the active Store after publication or abandonment until the Worker writes canonical JSON to the configured Operator-owned mode-`0700` backup directory, verifies ownership, exact bytes, digest and mode `0600`, fsyncs the file and directory, and records an immutable backup receipt. With no backup directory, Plan payloads are not purged. The digest or GitLab marker alone is deliberately insufficient authority. The recovery CLI consumes retained canonical JSON but never writes it back into the active Store after successful reconstruction.

## Deployment defaults

- SQLite and database polling are the first storage/queue implementation.
- One API process and one supervised Worker process with separate Unix identities are the reference deployment. They share only the SQLite directory; repository, worktrees, Plan backups and Worker credentials are Worker-private.
- One deployment is bound to exactly one GitLab Project.
- The repository is a persistent writable non-shallow checkout or mirror. The Worker obtains a requested SHA through a locked, bounded fetch before creating a detached worktree.
- Agent, verification and publication credentials are distinct Operator-owned inputs; the Agent receives no GitLab publication token.
- Postgres, Redis and separately deployed Worker loops are future implementations behind interfaces.
- SIGTERM stops new Worker claims and waits for in-flight loops; backend and verifier timeouts are individually capped at 1800 seconds and the reference supervisor applies a 3700-second final stop timeout.

---
status: accepted
date: 2026-08-10
---

# Retain sensitive payloads for 30 days and supervise separate API and Worker processes

Operator configuration v3 owns a sensitive-payload retention period from 1 to
3650 days, defaulting to 30. ReviewFlow permanently retains stable identities,
digests, decisions, expectations, lifecycle states, Effect Receipts, Relations,
backup receipts, purge audits, and Operator audits. After the retention period
it removes terminal Note bodies, terminal notification presentation/recipient
fields, and terminal diagnostics. Stable terminal timestamps govern expiry;
periodic reconciliation does not extend retention. Notification deliveries expire
from a stable `terminal_at` set on delivery or final failure; retries remain
nonterminal. A multi-channel request expires after all deliveries are terminal
and its latest terminal time is old enough. An empty request expires from its
creation time. Notification presentation,
recipient, and channel bindings retain a non-sensitive digest so idempotent
replays remain immutable after payload purge.

Published or explicitly abandoned Auto-fix Publication Plans require stronger
handling because canonical Plan JSON is Store-loss recovery authority. A Plan
payload may be purged only after the Worker writes `<plan_sha256>.json` to an
Operator-owned `0700` directory, verifies its owner, `0600` file mode, digest
and exact bytes, fsyncs the file and directory, and records an immutable backup
receipt in the Store. Without a configured backup
directory, Plan payloads are retained indefinitely. Payload purge never changes
the state or meaning of a receipt.

The Linux reference deployment uses separate API and Worker OS processes and
Unix identities under systemd. A shared group grants both identities access
only to SQLite; the repository, worktrees, Plan backups and Worker credentials
remain Worker-private. The API receives the webhook secret but no publication
or Agent credentials. The Worker receives GitLab, Agent, verification,
notification and retention authority and continues to host independent
internal loops. Each Agent or verifier timeout is capped at 1800 seconds.
SIGTERM stops new claims and waits for in-flight Worker loops; a 3700-second
systemd stop timeout bounds a sequential Agent plus verifier.

SQLite and the Plan backup directory form one protected recovery set. Live
GitLab, reverse-proxy, filesystem-backup and restore testing remain deployment
acceptance gates rather than unit-test claims.

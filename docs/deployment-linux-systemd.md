# Linux systemd reference deployment

The reference deployment runs one API process and one supervised Worker
process for exactly one GitLab Project. They use separate Unix identities and
share only the SQLite directory through `reviewflow-shared`. Only the API
receives the webhook secret; only the Worker can access the repository,
worktrees, Plan backups, GitLab Review Bot, Agent Provider, and notification
credentials. SQLite necessarily remains readable by both service identities.

## Layout

- application virtualenv: `/opt/review-agent-flow/.venv`
- API identity: `reviewflow-api:reviewflow-shared`
- Worker identity: `reviewflow-worker:reviewflow-shared`
- API environment: `/etc/review-agent-flow/api.env`, root-owned mode `0600`
- Worker environment and Operator configuration: root-owned mode `0600`
- shared Store directory: `/var/lib/review-agent-flow/shared`, mode `2770`,
  group `reviewflow-shared`
- Worker-private state: `/var/lib/review-agent-flow-worker`, mode `0700`, owned
  by `reviewflow-worker`; it contains the non-shallow repository, worktrees and
  `plan-backups` directory

Create the identities and state boundaries before starting either unit:

```bash
sudo groupadd --system reviewflow-shared
sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
  --gid reviewflow-shared reviewflow-api
sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
  --gid reviewflow-shared reviewflow-worker
sudo install -d -o root -g reviewflow-shared -m 2770 \
  /var/lib/review-agent-flow/shared
sudo install -d -o reviewflow-worker -g reviewflow-shared -m 0700 \
  /var/lib/review-agent-flow-worker
```

Both units set `UMask=0007`, so SQLite database, WAL and shared-memory files
remain group-writable without becoming world-readable.
This release uses fresh SQLite schema 17 and rejects schema 16. Before a
pre-production cutover, retain the old canary evidence and a consistent
database snapshot; start the new deployment with a fresh database only after
that evidence is secured. Production data requires a planned backup and
rollback path before any later schema change.

Copy the examples from `deploy/`, install both units, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now review-agent-flow-api review-agent-flow-worker
sudo systemctl status review-agent-flow-api review-agent-flow-worker
```

Terminate with `systemctl stop`. The Worker handles SIGTERM by stopping new
claims, asking all independent loops to stop, and waiting for in-flight work to
finish. Operator configuration bounds each Agent or verification command to
1800 seconds. Since an Agent and its verifier may run sequentially, the
reference Worker unit allows 3700 seconds before systemd applies its final kill
bound. The API delegates SIGTERM handling to Uvicorn.
The Worker holds a Store-backed singleton runtime lease and renews it from an
independent heartbeat while Agent or verifier calls run. A second Worker,
including `worker --once`, fails to start while the first lease is valid.
Normal shutdown releases its token; after a crash, takeover waits for lease
expiry. Lost ownership fails the Worker so systemd can restart it.

Expose `127.0.0.1:8000` only through an Operator-managed TLS reverse proxy.
Configure GitLab to send webhooks to that proxy. `/healthz` is liveness and
`/readyz` checks the local Store; neither performs a live provider call.

## Retention and recovery evidence

Operator configuration v4 defaults sensitive payload retention to 30 days.
The Worker runs a sweep at startup and hourly. Published or explicitly
abandoned Publication Plans are copied to the configured backup directory as
`<plan_sha256>.json`, written with mode `0600` inside a Worker-owned `0700`
directory, digest-checked, fsynced, and acknowledged in SQLite before their
active payload is purged. Pre-existing backup directories and files with a
different owner or broader permissions are rejected. If no backup directory
is configured, Plan payloads are retained indefinitely while other eligible
payloads still expire.

The sweep removes expired Note bodies, terminal notification presentation and
recipient fields, and terminal diagnostics. It retains ReviewKey and external
identities, Plan and policy digests, lifecycle states, Effect Receipts,
expectations, Relations, notification payload digests, backup receipts, purge
audits, and Operator audits. The stable Note retention timestamp is set on its
first terminal publication outcome and is not refreshed by reconciliation.
Notification payload retention begins at each delivery's first terminal
outcome. An Auto-fix request with multiple channels is retained until every
channel's terminal time is old enough; a request with no deliveries expires
from its creation time.
After a Note body is purged, periodic exact-content reconciliation stops; its
last confirmed identity/state remains historical evidence.

Back up the SQLite database and `plan-backups` directory as one protected
recovery set. A digest or GitLab marker without the canonical Plan JSON is not
sufficient Store-loss recovery authority.

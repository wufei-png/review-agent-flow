# Single-Project operations runbook

This runbook is for the schema-17, one-Project Linux systemd deployment in
[the reference layout](deployment-linux-systemd.md). Run commands on the
deployment host with the stated service identity. Keep tokens, webhook URLs,
canonical Plans, database bundles and raw review content in private operator
storage; the evidence document contains only redacted identities and digests.

## Before starting a canary

- Reserve a new canary Project and parent MR head SHA, distinct from the
  [A1 historical run](evidence/jihulab-opencode-a1-2026-08-10.md). Use a
  non-shallow Worker clone with fetch access to the exact MR diff SHAs.
- Confirm the Review Bot can read the Project, MR and repository, write MR
  Notes, create the Auto-fix branch commit and create/read the child MR.
  Keep its token in `worker.env` only. Confirm a healthy OpenCode executable,
  selected model and Operator-approved provider credential under the Worker
  identity. The Agent must not receive the GitLab token or notification URL.
- Prepare an authorized notification transport, an Operator channel binding and a
  canary recipient who will separately confirm visibility. For an owner-only
  Feishu canary, use `feishu_dm` with `recipient_username` bound to the parent
  MR creator and `recipient_open_id_env` set to that same person's open ID for
  this Feishu app. Keep app credentials in the Worker environment; do not
  configure a group webhook or a second recipient. Verify the
  target-branch Policy v4 requests the authorized backend, profile, Auto-fix
  and channel IDs, and retain its target SHA and policy digest.
- Provide a private backup medium and an isolated recovery host or directory
  with the same absolute Store and Plan paths. Preserve the old canary
  evidence and a consistent schema-16 snapshot before a fresh schema-17
  cutover. Confirm a rollback decision and private evidence location.
- Install the two example units and environment files from `deploy/`. On a
  Linux host with systemd, verify both installed units before enabling them:

  ```bash
  systemd-analyze verify /etc/systemd/system/review-agent-flow-api.service \
    /etc/systemd/system/review-agent-flow-worker.service
  ```

  Check ownership and modes from the reference layout; do not loosen sandbox
  settings based on a local or simulated failure.

## Start, observe and stop

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now review-agent-flow-api
curl --fail --silent --show-error --retry 10 --retry-delay 1 \
  --retry-connrefused http://127.0.0.1:8000/readyz
sudo systemctl enable --now review-agent-flow-worker
sudo systemctl status review-agent-flow-api review-agent-flow-worker
curl --fail --silent http://127.0.0.1:8000/healthz
curl --fail --silent http://127.0.0.1:8000/readyz
sudo -u reviewflow-worker /opt/review-agent-flow/.venv/bin/review-agent-flow \
  ops-status --project-id 42 \
  --db /var/lib/review-agent-flow/shared/review-agent-flow.sqlite3
sudo journalctl -u review-agent-flow-worker -u review-agent-flow-api --since '15 minutes ago'
```

Replace `42` with the deployment's bound Project ID. `/healthz` proves API
liveness; `/readyz` checks configuration, Project binding and local SQLite
read/write. Neither checks the Worker or a live provider. `ops-status` is a
read-only Store snapshot. Its `worker.state` must be `active` with advancing
`heartbeat_at` and future `lease_until` while the service runs. Check
`systemctl is-active` and the journal too: the Worker supervises separate
Review/Publication, Notification and Reconciliation/Cleanup loops and fails
the process if ownership is lost or a loop dies. The Retention sweep runs in
the third loop at startup and hourly. `retention.last_purge_at` records only a
completed purge transaction; `last_sweep_result` remains `unavailable`.

For a Run ID returned by intake, inspect local effect state without a provider
call:

```bash
sudo -u reviewflow-worker /opt/review-agent-flow/.venv/bin/review-agent-flow \
  status --project-id 42 --run-id 1 \
  --db /var/lib/review-agent-flow/shared/review-agent-flow.sqlite3
```

The status result includes the exact ReviewKey, frozen policy digest,
expectations, redacted receipts and `effects.note`, `effects.auto_fix_plan`,
`effects.auto_fix_relation` and `effects.notifications`. Use `next_action`
as a local hint, then verify external identities in GitLab or the channel.
`delivered` on a notification means server acceptance and may duplicate; it
does not mean the recipient saw or read it. Keep the Store `run_id`, Project,
MR IID, SHA, log `correlation_id`, Note ID, Plan ID/digest, commit SHA, child MR
IID, Relation ID, request generation and delivery ID together in the private
incident record. Do not copy raw payloads or credentials into shared logs.

Review `ops-status` for growing `runs.pending` or
`oldest_pending_age_seconds`, `runs.expired_leases`, `notes.blocked` or
`uncertain`, `auto_fix.reconciliation` or `blocked`,
`auto_fix.relation_divergence`, and `notifications.pending`, `running`,
`failed` or `oldest_pending_age_seconds`. No fixed admission or alert threshold
is established yet; compare trends with the canary workload and page the
Operator for persistent growth, a stopped service, absent/expired ownership,
or blocked/divergent effects. Verify a suspected provider outage with a
separate Operator-controlled provider request. The protected
`GET /probes/gitlab` route requires `X-ReviewFlow-Internal-Token` equal to the
API webhook secret, but the reference CLI deployment does not inject a live
probe: it returns `503 not_configured`. Keep the secret out of shell history
and evidence logs.

To stop all new processing, stop the Worker first, wait for its in-flight work
to finish, then stop the API if intake must also cease:

```bash
sudo systemctl stop review-agent-flow-worker
sudo systemctl status review-agent-flow-worker
sudo systemctl stop review-agent-flow-api
```

The Worker has a 3700-second final stop budget for sequential Agent and
verifier calls. Confirm no Worker process remains and `ops-status` reports
absent or expired ownership before maintenance. Do not start `worker --once`
beside the supervised Worker: both use the same singleton lease.

## Contain an external-write incident

Stop the Worker and preserve the Store, journal, exact ReviewKey, Plan digest,
`write_phase`, lease and remote identities before changing configuration.
This halts Note, Auto-fix and notification writes, plus reconciliation. For a
selective future policy change, commit the target-branch Policy or edit the
Operator configuration while stopped, then review existing frozen requests
before restarting. Changing policy or removing a webhook alone does not
cancel a request or Plan already persisted. Leave the Worker stopped if any
such effect must remain contained; escalate for an explicit per-effect
decision. Keep the API running only if continued intake is intended.

| Local evidence | Remote check and decision |
| --- | --- |
| Note pending or `write_uncertain`; no confirmed Note ID | Search the parent MR for the exact ReviewKey marker and Review Bot Note. A unique matching Note can be reconciled; a missing or ambiguous Note needs Operator investigation before replay. A confirmed user-deleted Note must not be recreated. |
| Plan `preflight`, before external write | Check current parent head and frozen Plan. A valid expired lease may be reclaimed normally after Worker restart. |
| Plan `commit_write_started` without durable `branch_commit_sha` | Inspect the planned branch and GitLab commit against the immutable Plan. An ambiguous commit is never reissued automatically. Keep writes contained and escalate if identity cannot be proved. |
| Plan `commit_confirmed` with durable commit SHA | Confirm the remote branch commit, its parent and actions against the Plan, then allow reconciliation to continue toward MR creation only when exact identities agree. |
| Plan `mr_write_started` without confirmed child IID | Check Review Bot child MRs for the exact marker, branch and commit. Reconciliation requires two unique zero-candidate observations at least five seconds apart before it may recreate an MR; ambiguity or a mismatch requires Operator review. |
| Plan `mr_confirmed` or published Relation | Confirm the child IID, source head and target. A persisted Relation survives title and Draft/Ready edits; source, target or tree divergence needs Operator review. |

`status.effects.*.next_action` is not remote proof. Preserve the Plan and
Store snapshot before any manual repair. Do not manually create a replacement
commit, MR or notification under a ReviewFlow marker. Restart the Worker only
after the uncertain boundary has an exact remote identity or a documented
reconciliation path. After checking a blocked Plan's immutable payload and
remote state, set `PLAN_ID` and `REASON` to the reviewed values and run one
of these commands against the active Store:

```bash
sudo -u reviewflow-worker /opt/review-agent-flow/.venv/bin/review-agent-flow \
  rearm-autofix-plan --db /var/lib/review-agent-flow/shared/review-agent-flow.sqlite3 \
  --plan-id "$PLAN_ID" --reason "$REASON"
sudo -u reviewflow-worker /opt/review-agent-flow/.venv/bin/review-agent-flow \
  abandon-autofix-plan --db /var/lib/review-agent-flow/shared/review-agent-flow.sqlite3 \
  --plan-id "$PLAN_ID" --reason "$REASON"
```

Rearm resets the attempt budget; a Plan past `preflight` resumes reconciliation
only. Abandon terminally stops an idle frozen, blocked or
reconciliation-needed Plan. Record the returned action ID and state/phase.
Neither command removes a remote effect. A busy or unresolved Plan needs
escalation, not repeated rearm. These commands run as the Worker service user,
so record the human Operator and incident reference in the reason and private
operations log alongside the CLI audit.

For notification `pending` or `running`, inspect the outbox state and provider
response before restart; a crash after provider acceptance may send the same
request again. `failed` needs channel/credential and recipient review. Ask the
recipient whether it is visible before deciding on a manual resend. Only an
Operator may resend. Set `CHILD_MR_IID` and `REASON` after reviewing the frozen
Policy Snapshot and currently authorized channel binding. On the Linux host,
run the command as a transient service so systemd reads the root-owned
`worker.env` and supplies the same credential environment as the Worker;
neither the token nor webhook URL appears in command arguments:

```bash
sudo systemd-run --wait --pipe --collect \
  --property=User=reviewflow-worker --property=Group=reviewflow-shared \
  --property=UMask=0007 \
  --property=EnvironmentFile=/etc/review-agent-flow/worker.env \
  --property=WorkingDirectory=/var/lib/review-agent-flow-worker/repository \
  /opt/review-agent-flow/.venv/bin/review-agent-flow \
  resend-autofix-notification --child-mr-iid "$CHILD_MR_IID" \
  --db /var/lib/review-agent-flow/shared/review-agent-flow.sqlite3 \
  --operator-config /etc/review-agent-flow/operator.yml --reason "$REASON"
```

The command requires the retained frozen Policy Snapshot and creates a new
audited request generation. Record the prior and new generations and the risk
of duplicate delivery. The transient command runs as the Worker service user;
record the human Operator and incident reference in the reason and private
operations log.

Store-loss Relation recovery is a separate audited action requiring the full
canonical Plan, exact parent and child identities and remote verification.
Prefer restoration of a verified complete bundle. If exceptional Relation
reconstruction is required, set `PARENT_MR_IID`, `PARENT_SHA`,
`INTENT_NONCE`, `PLAN_SHA256`, `PLAN_FILE`, `CHILD_MR_IID` and `REASON` from
the protected evidence, then run under the same transient identity and
credential environment:

```bash
sudo systemd-run --wait --pipe --collect \
  --property=User=reviewflow-worker --property=Group=reviewflow-shared \
  --property=UMask=0007 \
  --property=EnvironmentFile=/etc/review-agent-flow/worker.env \
  --property=WorkingDirectory=/var/lib/review-agent-flow-worker/repository \
  /opt/review-agent-flow/.venv/bin/review-agent-flow \
  recover-autofix-relation --parent-mr-iid "$PARENT_MR_IID" \
  --parent-sha "$PARENT_SHA" --intent-nonce "$INTENT_NONCE" \
  --plan-sha256 "$PLAN_SHA256" --plan-file "$PLAN_FILE" \
  --child-mr-iid "$CHILD_MR_IID" --reason "$REASON" \
  --db /var/lib/review-agent-flow/shared/review-agent-flow.sqlite3
```

Check the [integration specification](spec/05-integrations.md) before this
exceptional action. Recovery does not restore notification resend authority
when the frozen Policy Snapshot is missing.

## Rotate credentials and restore

Coordinate a webhook-secret change with the GitLab webhook and the TLS proxy:
stop intake or route it to maintenance, update root-owned `api.env` and the
GitLab configuration, restart the API, then verify a new signed webhook and
reject an old secret. Rotate the Review Bot token and Agent-provider key in
root-owned `worker.env` while the Worker is stopped; confirm GitLab and
OpenCode access under the Worker identity before restart. Rotate Feishu/WeCom
webhook URLs in `worker.env` and keep their Operator channel IDs stable;
verify server acceptance and recipient visibility on a canary delivery.
There is no credential hot reload. Do not expose credentials via CLI flags,
journal output or saved evidence. If the new credential fails, keep the
affected service stopped or channel contained and use the approved secret
rollback procedure.

Take and verify a protected bundle with `backup-store` and `verify-backup` as
documented in [backup and restore](backup-restore.md). A live SQLite file copy
is not a consistent snapshot. For recovery, stop both services, restore the
bundle into an empty isolated target at the exact original receipt paths,
verify modes, digests, SQLite integrity and schema, inspect representative
`status` and `ops-status` results, and only then restart services. Keep the
source bundle immutable. If validation fails, discard the incomplete target,
escalate and retry from a verified bundle in a new empty target.

## Canary record and exit decision

In a new `docs/evidence/` document, record the deployment code commit,
Python/OpenCode versions, Operator configuration version and digest, target
Policy SHA/digest, Project and parent MR IID, exact head SHA and ReviewKey.
For the same SHA, record the Run and redacted correlation IDs, Note ID and
marker, Plan digest and write phase, Auto-fix branch commit SHA, child MR IID,
Relation ID, notification request ID/generation and channel delivery status.
Keep private logs or GitLab links in protected operator storage.

Check that duplicate intake and Worker replay create no second Note,
Auto-fix Relation or notification request generation. Record notification
outbox persistence, provider business acceptance and the recipient's separate
visibility confirmation as three observations. Exercise an interruption
after the commit write boundary and after the MR write boundary on separate
canary attempts; preserve before/after Store phases and remote commit/MR
identities, then prove reconciliation did not duplicate either remote effect.
Run the OpenCode runtime-isolation test on the canary host and record a real
pass, not a skip. Complete the isolated full-bundle restore with service
stop/start and path/identity checks. If any write is ambiguous, stop the
Worker, keep the failure evidence and follow the table above. A go/no-go
decision requires the positive path, both crash windows, recipient
confirmation, safe exit path and isolated restore; record missing evidence as
unverified rather than inferring success from local tests.

# JiHuLab/OpenCode canary preparation — 2026-09-21

## Status

This is a **partial preparation record**, not a successful exact-SHA canary or
a pilot go/no-go decision. The API and Worker are stopped and disabled. No
ReviewFlow Run was enqueued, no Program-owned Note, Auto-fix commit, child MR,
Relation or notification outbox delivery was created. The previous A1 Notes
are not used as receipts for this MR.

## Live identities and exact target

- Private [canary Project](https://jihulab.com/wufei-png-group/reviewflow-canary-20260921):
  ID `373614`, under `wufei-png-group`. The only human Project member observed
  was `wufei-png`. A Project service account with Developer access was created
  for ReviewFlow; its API token is held in private local and Worker-only host
  files, not in this repository or the Agent environment.
- [Parent MR !1](https://jihulab.com/wufei-png-group/reviewflow-canary-20260921/-/merge_requests/1):
  target baseline `0dd5e4afe1276fe0c9393ee1e212901e5f644990`, exact source
  head `8610a97e552468a78787aaba0d31f8817b54ca48`; the MR's `diff_refs`
  matched these commits when checked. The target freezes Policy v4 and the
  Skill; the source change doubles shipping in `pricing.py`, and its unit test
  fails `20 != 15`. Policy requests Auto-fix and only `feishu-owner-dm`.
- Host alias `ubuntu`: systemd 245, Python 3.11.13, OpenCode 1.18.26 and
  lark-cli 1.0.30. Separate `reviewflow-api` and `reviewflow-worker` Unix
  identities use `reviewflow-shared` only for the Store. The Worker cloned the
  private Project and fetched the exact MR head with its own GitLab credential.

## Checked boundaries

- The dedicated Worker identity passed OpenCode's runtime-isolation test after
  its passwd home was set to `/var/lib/review-agent-flow-worker`. Before that
  correction, OpenCode failed creating a synthetic home directory.
- `systemd-analyze verify` returned zero for the installed units. On systemd
  245 it warned that `ProtectProc` and `ProcSubset` are ignored; this is not
  evidence that those sandbox settings are active.
- The API returned `/healthz` and `/readyz` with Project and SQLite checks
  ready. The fresh Store is schema 17. The Worker identity performed a real
  SQLite write while the API was active; after an API restart the Worker could
  still open the Store. Database, WAL and SHM were group-owned and mode `0660`;
  a final `PRAGMA quick_check` returned `ok`. Both services were then stopped.
- SQLite's WAL files are mapped and can be unlinked when clients close. An
  initial runtime permission repair committed as `6d56bb8` proved unsafe in
  a concurrent probe: six of ten Worker-identity probe processes ended with
  `Bus error`. The API was stopped and the database checked. Commit `ce20858`
  removed that repair and switched the reference deployment to pre-creating a
  fresh `0660` database in a setgid shared directory. On a disposable host
  Store using the original runtime code, 20 concurrent processes alternating
  the two Unix identities completed without failures. This stress probe is
  deployment evidence, not proof of all crash windows.
- The failed probe Store held no ReviewFlow business Run. During the reset to
  a fresh Store, a shell glob did not expand across the shared directory's
  permissions, so the intended quarantine move did not retain that particular
  failed-probe database. An earlier pre-guard Store copy remains private on
  the host. The failed probe therefore cannot be replayed from its exact DB
  bytes; the process failures and later clean-Store checks are the available
  evidence.
- The local `feishu-shadow-agent/config.yaml` identified 吴非 but contained
  neither a reusable ReviewFlow app credential nor a group-free webhook. The
  local owner open ID differed from the app-specific ID verified by the host's
  authenticated lark-cli. A single `lark-cli` bot request to **吴非's own
  verified open ID** succeeded; it was a transport-only DM, not a ReviewFlow
  outbox delivery. No group chat or other recipient was addressed. Human
  visibility has not been confirmed.
- The canary Operator configuration has exactly one `feishu_dm` channel,
  bound to GitLab username `wufei-png` and the owner's app-specific open ID.
  It has no group webhook or second recipient. The dedicated Feishu app ID
  and secret are still absent, so the Worker was never started.
- A fresh read-only review found that a queued DM could otherwise be rebound
  to a different open ID under the same channel ID. Commit `f8c99f9` persists
  a digest of the app and intended recipient with the outbox delivery and
  rejects changed bindings before any send. The regression test covers this
  path. The corrected code is on the host, but no live ReviewFlow send has
  occurred.
- The dedicated Worker's model-key probe for `openai/gpt-5.6-terra` returned
  `Incorrect API key`. A separate probe through the host user's personal
  OpenCode OAuth returned `OK`; those broad personal credentials were not
  copied to the Worker. This does not establish a healthy dedicated Provider.

## Outstanding acceptance evidence

- A valid dedicated model credential and the Feishu app ID/secret must be
  installed in the root-owned Worker environment. The recipient should confirm
  whether the transport-only DM was visible before treating visibility as
  evidence. No secret or open ID should be pasted into a shared record.
- TLS webhook ingress, real MR intake, frozen Policy/Plan digest, RESULT_PATH,
  Program-owned Note, Auto-fix commit and child MR, Relation, and ReviewFlow
  outbox server acceptance are unverified. Owner visibility of an actual
  ReviewFlow notification remains unverified.
- Duplicate enqueue/Worker replay, post-commit and post-MR crash recovery,
  a full Store plus canonical Plan backup/restore on an isolated path, and
  live Worker ownership/three-loop observation have not run. An empty-Store
  health check cannot stand in for these receipts.

Keep the Worker disabled and the API stopped until the dedicated credentials,
owner-only channel, and remaining ingress and recovery prerequisites are
checked. Make the pilot go/no-go decision only from a completed canary record.

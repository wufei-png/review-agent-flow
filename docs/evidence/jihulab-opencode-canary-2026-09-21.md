# JiHuLab/OpenCode canary — 2026-09-21

## Verdict and scope

This is a live, exact-SHA canary with a **no-go** pilot decision. The positive
ReviewFlow path and the post-MR-write recovery passed. The post-commit-write
fault stopped safely but could not complete publication from the persisted
identity; the Operator abandoned that Plan after verifying the unique remote
commit. TLS webhook intake and a dedicated Provider identity remain
unverified. Both systemd units were stopped and disabled at the end. No group
message or DM to another person was sent.

All times below are UTC on 2026-09-20 (Asia/Shanghai: 2026-09-21). Private
Worker files retain Store and backup payloads; this document contains only
redacted operational receipts. The earlier [A1 record](jihulab-opencode-a1-2026-08-10.md)
is historical and supplies no acceptance receipt here.

## Identities, configuration and safety preflight

- Private [Project `373614`](https://jihulab.com/wufei-png-group/reviewflow-canary-20260921)
  and [parent MR `!1`](https://jihulab.com/wufei-png-group/reviewflow-canary-20260921/-/merge_requests/1)
  used target baseline `0dd5e4afe1276fe0c9393ee1e212901e5f644990`.
  Frozen Policy v4 digest on all three Runs was
  `021891b7e32082ea3b41191f92d38b8531824cc5e72fa5ab7711bb7bcbdc32c1`.
  The target-frozen Skill and Policy request Auto-fix and channel
  `feishu-owner-dm`. ReviewFlow's Project service account is the author of
  the Notes and child MRs; the only human Project member observed was
  `wufei-png`.
- Host `ubuntu`: systemd 245, Python 3.11.13, OpenCode 1.18.26 and lark-cli
  1.0.30. API and Worker have separate Unix identities. The Worker fetched
  the exact MR commits with its own GitLab credential. The Store is schema 17;
  database, WAL and SHM were group-owned mode `0660`, and `PRAGMA quick_check`
  returned `ok`.
- The final Operator profile invokes the installed
  `review-agent-flow-opencode` entry point with model
  `openai/gpt-5.6-luna`. Its allowlist exposes only `OPENCODE_AUTH_SOURCE`
  and the trusted `PATH` to the Agent Backend. A Worker-owned mode-`0600`
  file contains only the existing host user's `openai` OAuth record. The
  wrapper copies that record into each mode-`0700` Run directory as
  `opencode-data/opencode/auth.json` mode `0600`. A real Worker-identity
  `run --pure --model openai/gpt-5.6-luna` probe exited 0 and returned `OK`.
  OpenCode's built-in OAuth plugin was required for this credential; the
  wrapper enables it while `--pure` continues to exclude external plugins.
  The installed wrapper SHA-256 matched the repository source:
  `241252191d0e2b114076498b0eee5dd92442a39fb8ec5550f618afdc7726eeb0`.
  This is a scoped copy of a personal OAuth credential, not a dedicated
  Provider account. Per-Run token refreshes are not copied back to the stable
  source; it will require controlled renewal.
- The Worker environment holds the Feishu app ID/secret recovered from the
  host's encrypted lark-cli store, and the app-specific 吴非 open ID from that
  host's `feishu-shadow-agent/config.yaml`. They are in a root-owned
  mode-`0600` environment file, not in Git or Agent environment. The Operator
  has exactly one `feishu_dm` channel, bound to GitLab username `wufei-png`,
  and no group webhook. `FeishuDirectNotifier` rejected any recipient tuple
  other than the bound owner in automated tests. One direct adapter probe to
  吴非 returned Feishu business success. The earlier lark-cli transport-only
  DM was separately confirmed visible by 吴非; it is not an outbox receipt.
- `systemd-analyze verify` returned 0 for both units, with systemd 245 warnings
  that `ProtectProc` and `ProcSubset` were ignored. API `/readyz` later returned
  ready for config, Project and SQLite. Earlier cross-identity SQLite checks
  and a disposable 20-process probe passed after the unsafe runtime permission
  repair was removed. The failed earlier probe had six `Bus error` processes;
  its exact database bytes were not retained because a shell glob missed the
  quarantine move. That failure is not treated as a passing stress result.

## Positive Run and idempotent replay

| Receipt | Exact observed value |
| --- | --- |
| ReviewKey | Project `373614`, MR `!1`, head `8610a97e552468a78787aaba0d31f8817b54ca48` |
| Run / frozen Plan | Run `1`, Plan `1`, digest `a58faebde9fb730b437af64a03ea2aeadfb89ef05243ff8867bcf3f32fd39add` |
| Program Note | [Note `9207699`](https://jihulab.com/wufei-png-group/reviewflow-canary-20260921/-/merge_requests/1#note_9207699), authored by the ReviewFlow service account with the exact ReviewKey marker |
| Auto-fix | Commit `85fcd9c9640de3e4dc90a0a0d68eb4b4eb49d327`, [child MR `!2`](https://jihulab.com/wufei-png-group/reviewflow-canary-20260921/-/merge_requests/2), Relation `1` (`opened`) |
| Notification | Request `1`, generation `1`, outbox delivery `1` on `feishu-owner-dm`, one attempt, `delivered` at `18:11:49.585`; this means Feishu server acceptance and may duplicate after a crash |

The parent head's `python3 -m unittest -q` failed `20 != 15`; the child
commit changed only `pricing.py` to add shipping once, and its test passed.
`review-mr --mr-iid 1` repeated on the same head returned `existing`, Run
`1`. A Worker restart left one Note, one Plan, one Relation, one request at
generation `1`, and one delivered outbox row; no second remote Note or child
MR was observed. 吴非 later supplied a screenshot showing this outbox message
in the personal Feishu conversation.

## Fault-window attempts

1. **After remote commit, before local commit identity:** parent head
   `7f5b1a48ed4d1d0d9e44314418afa32014f4000f`, Run `3`, Note `9207710`,
   Plan `2` digest
   `f9157c2b5bc83ad542e1410ed4657b31802937ee519ccaa1c9516c8f39726b2e`.
   A one-time Worker harness exited `137` immediately after GitLab returned
   commit `7a3fee17e22f86f6ef6462688c18c23761831551`, before the Store
   recorded its SHA. The Store showed `commit_write_started`, `write_uncertain`
   and no child IID. Independent Git fetch confirmed the unique planned branch
   head, exact parent SHA, only changed path `pricing.py`, and new content
   SHA-256 `acef132f0e7385d155f62c658db07213129d8dffbd09e462466168fe57921f83`,
   matching the immutable Plan action. No matching child MR existed. On restart,
   ReviewFlow did **not** repeat the commit or create an MR; it moved to
   `blocked/operator_check` because the commit SHA was not durably recorded.
   After a verified backup, the Operator used the audited
   `abandon-autofix-plan` action `a72bffe4a76c4e94b1c788d0ae98ed62`.
   The remote branch remains as contained canary evidence. This proves a safe
   stop and no duplicate write, **not** a completed post-commit recovery.
2. **After remote child MR, before local IID:** parent head
   `585df7a87aca6c52d53520047bb8c773e24073e6`, Run `4`, Note `9207716`,
   Plan `3` digest
   `fd083e76ed582b0623c9c11313c3ebdcc56199e14f2a972ddb375b68c5bc59f6`.
   Commit `fd896a29643f4f9da23f7f7e0d92d4c4a9fd44af` was durable. The
   one-time Worker harness exited `137` immediately after GitLab returned
   [child MR `!3`](https://jihulab.com/wufei-png-group/reviewflow-canary-20260921/-/merge_requests/3),
   leaving `mr_write_started` and no stored child IID. After lease expiry,
   normal Worker reconciliation found the unique existing MR, recorded its IID
   and Relation `2`, and did not create another MR. Request generation `1`,
   outbox delivery `2`, one attempt, was server accepted at `18:26:12.961`.
   A remote search found exactly one child MR for this Plan's source branch.
   吴非's screenshot also shows this outbox message in the same personal
   conversation.

On 2026-09-21, 吴非 supplied one screenshot showing the earlier transport
check, the direct adapter probe, and the ReviewFlow Auto-fix messages linking
to child MRs `!2` and `!3`. This is recipient confirmation that both actual
outbox messages were visible; it is distinct from the two Feishu server
acceptance receipts and does not establish read timing. The image is kept only
in private local operator evidence at
`~/.local/state/reviewflow-canary/evidence/feishu-owner-visible-2026-09-21.png`
(mode `0600`, SHA-256
`b5657a0d3bb4da15bfe5665ef6271cc90eac59df25617703819dce1e9507ca12`);
the image itself is not committed to the repository.

The fault harnesses were temporary host scripts that wrapped the actual
GitLab client method and used `os._exit(137)` only after the corresponding
remote API returned. They were not installed in the service or repository.
The normal systemd Worker was used for reconciliation. No group or other
recipient was addressed by either delivery.

## Backup and restore evidence

- With both units stopped after Run `1`, a Worker-owned, mode-`0700` bundle
  captured the live SQLite Store at `18:14:09.471`, schema `17`, and passed
  `verify-backup`. It had `plan_count=0` because the new Plan had not reached
  its 30-day retention backup point. In a private Linux mount namespace with
  private bind mounts at the original absolute Store and Plan paths, a fresh
  mode-`0660` database restored from that bundle passed digest, ownership,
  SQLite integrity, `status` and `ops-status` checks: one Run, Note, Relation
  and outbox row matched Run `1`.
- On that **isolated copy only**, the same retention Plan-file writer and
  Store receipt method materialized Plan `1` early. A second bundle at
  `18:16:31.191` contained one mode-`0600` canonical Plan file and passed
  `verify-backup`. A second fresh mount namespace restored its database and
  Plan to the exact absolute receipt paths; digest, schema, ownership, Plan
  receipt path, Run `1`, child MR `!2` and delivered outbox status matched.
  No online Store record or retention clock was changed for this drill.
- After the drill, the original API and Worker started successfully: `/readyz`
  was ready, Worker ownership had a fresh heartbeat, and `ops-status` showed
  no pending Run, uncertain Note or notification. This restart was on the
  original Store, not on the isolated restored copy. A separate recovery host,
  independent backup medium and systemd start against a restored Store were
  not tested.

## Final state, checks and remaining gates

The final read-only `ops-status` showed no pending/running Run, blocked Plan,
uncertain Note, failed/pending notification or active Worker ownership. Both
units are inactive and disabled. The abandoned post-commit Plan and its orphan
branch remain visible in immutable Store/GitLab evidence; zero blocked count
must not be read as successful publication.

Local checks after the OAuth wrapper fix: `pytest` 427 passed with one
Starlette/httpx deprecation warning; targeted OpenCode isolation and wrapper
tests 4 passed; `ruff check .`, `compileall -q src tests`, and `git diff --check`
passed. The host's real Worker-identity Luna probe, ReviewFlow notifier probe,
API readiness, systemd ownership, exact-SHA GitLab effects, backup verification
and isolated restores are live observations, not mocked integration tests.

Still required before a pilot go decision: a supported recovery path that
completes a post-commit crash after an exact remote commit has been verified;
TLS webhook ingress and signed live webhook intake; a dedicated renewable
Provider identity; and a recovery-host/systemd restore exercise if the pilot
requires host loss readiness. Do not infer any of these from `delivered`, a
local test, or the isolated namespace drill.

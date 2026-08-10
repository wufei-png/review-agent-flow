# JiHuLab/OpenCode A1 canary evidence — 2026-08-10

## Verdict

The A1 canary is **partial, not complete**. The OpenCode composition completed
exact-SHA review and Program-owned GitLab Note publication on JiHuLab. Manual
trigger idempotency, fail-closed Auto-fix candidate handling, expired-lease
reclaim and worktree cleanup were also observed against real runtime state.

Program-owned Auto-fix commit/MR publication did not complete, so no Relation
was established and the durable notification outbox path was never entered. A
separate notification transport canary also found that the available Feishu
webhook was stale. This evidence applies only to the OpenCode adapter
composition; it says nothing about generic command, Codex or Claude Code
Backend parity.

## Live boundary and setup

- GitLab host: `https://jihulab.com/` only. Every direct GitLab resource command
  used host-qualified `glab`; no SenseTime Project was read or modified.
- Private canary Project: [wufei-png-group/reviewflow-opencode-a1-20260810](https://jihulab.com/wufei-png-group/reviewflow-opencode-a1-20260810), numeric ID `360321`.
- Parent MRs: [!1](https://jihulab.com/wufei-png-group/reviewflow-opencode-a1-20260810/-/merge_requests/1) and [!2](https://jihulab.com/wufei-png-group/reviewflow-opencode-a1-20260810/-/merge_requests/2).
- Runtime: OpenCode `1.18.11`, native `run --pure --agent reviewflow`,
  adapter-owned permissions/configuration, isolated per-Run XDG state and no
  GitLab token in the Agent environment.
- Controlled defect: `calculate_total` added the shipping charge twice. The
  explicit repository Skill required the smallest safe one-line repair and
  strict RESULT_PATH v1 JSON.

## Confirmed receipts

OpenCode Run results for MR `!1` produced three canonical Program-owned Notes:

| Run | Exact head SHA | GitLab Note | Result |
| --- | --- | --- | --- |
| 3 | `f656876d2926209d5f3a596a453c5cb9d5a726e7` | `8762137` | Note published; Auto-fix candidate blocked |
| 4 | `cf2baf747d3569fab6bf472b7e87c169312f71b9` | `8762173` | Note published; Auto-fix candidate blocked |
| 5 | `e715c92bff258794cc18a42c4577d51f7d37c1dd` | `8762204` | Note published; binary path diagnosed |

Each Note begins with the exact marker
`<!-- reviewflow:v1;project=360321;mr=1;sha=<head>;effect=note -->`, and JiHuLab
reported author `wufei-png`. Run 5 identified the rejected candidate path as
`__pycache__/pricing.cpython-314.pyc`. The Project then added normal Python
ignore rules; ReviewFlow did not weaken its bounded text-only Diff Envelope.

Repeating `review-mr` for MR `!1` at
`c7f25a54691d25a44bb9b8f87b5906be79f5b91f` returned `status=existing`,
`run_id=6`, and the Store still contained exactly one row for that ReviewKey.

## Crash and recovery drill

Run 6 was interrupted after OpenCode had written a valid RESULT_PATH but before
the Worker could persist the result. The process disappeared while the Store
remained `running` under its original lease. No database lease was edited.

After natural lease expiry, a new Worker reclaimed the same Run. The durable
record became `attempts=2` and `lease_recoveries=1`; it created a separate
`run-6-attempt-2` worktree rather than trusting the abandoned result. The
provider request then reached the configured 600-second timeout, so the Run
failed with `failure_class=agent_timeout` and published no effect. Both the
original and recovery worktree records ended `cleaned`.

This proves reclaim and cleanup behavior, but not successful post-crash effect
publication.

The successful Note runs also predate the final hardening that freezes the Skill
from the target commit, embeds its bounded bytes, and disables OpenCode's general
repository `read` and ambient `skill` tools. They therefore do not prove the
instruction-source isolation of the final adapter configuration; that
configuration still needs a live canary.

## Unmet A1 evidence

- No Program-owned Auto-fix commit or child MR exists. Runs 3–5 correctly
  blocked untracked Python bytecode before Publication Plan creation. Run 6
  timed out after reclaim. Runs 8 and 9 on MR `!2` failed before Agent output
  when the OpenAI provider returned repeat `Unexpected server error` responses.
- No durable notification request/outbox delivery exists because no Auto-fix
  Relation was established.
- The final adapter's target-frozen trusted Skill plus disabled repository `read`
  and ambient `skill` tools were added after these live runs and have only local
  runtime configuration/permission evidence.
- A separate live call through ReviewFlow's Feishu adapter was attempted and
  explicitly labeled as a transport canary. Feishu rejected the stored webhook
  with business code `19001` (`incoming webhook access token invalid`), so it is
  evidence of fail-closed provider response handling, not notification delivery.

A later A1 rerun needs one healthy OpenCode model credential and one fresh
Feishu or WeCom webhook. It should use a new exact MR head and must record the
Auto-fix commit SHA, child MR IID/marker, delivered notification outbox row and
an idempotent Worker replay before declaring A1 complete.

# Implementation status

## Implemented

| Spec area | Current implementation |
| --- | --- |
| Policy defaults/schema subset | `src/review_agent_flow/policy.py` |
| Policy template CLI | `src/review_agent_flow/cli.py` |
| Generic Agent CLI | `src/review_agent_flow/runner.py` |
| OpenAI-compatible Provider protocol | `src/review_agent_flow/provider.py` |
| Exact-SHA worktree helper | `src/review_agent_flow/worktree.py` |
| SQLite idempotency, claim and Auto-fix relation | `src/review_agent_flow/store.py` |
| SQLite notification outbox and per-channel retry | `src/review_agent_flow/store.py`, `worker.py` |
| GitLab event normalization | `src/review_agent_flow/events.py` |
| GitLab protocol and python-gitlab adapter | `src/review_agent_flow/gitlab.py`, `gitlab_client.py` |
| Feishu/WeCom webhook adapters | `src/review_agent_flow/notifications.py` |
| FastAPI Webhook ingress | `src/review_agent_flow/web.py` |
| Worker orchestration and Pipeline Gate | `src/review_agent_flow/worker.py` |
| Worker CLI | `src/review_agent_flow/cli.py` |
| Default Skill | `src/review_agent_flow/defaults/review-agent/SKILL.md` |
| Unit tests | `tests/` |

## Remaining implementation

1. Add real GitLab sandbox verification for Note, branch and Auto-fix MR APIs.
2. Add Pipeline webhook fixtures for GitLab versions used by deployments.
3. Add configurable retry/backoff policy beyond the Worker defaults.
4. Add full recipient identity lookup/mapping for Feishu and WeCom.
5. Add real Agent CLI contract tests and Auto-fix MR creation fixtures.
6. Add Postgres/Redis implementations when multi-instance deployment is required.
7. Add package build/publish automation after the public API stabilizes.

## v0.1 acceptance criteria

- A duplicate Webhook for the same Project/MR/SHA creates one Run.
- Webhook startup fails without `GITLAB_WEBHOOK_SECRET` or `REVIEW_AGENT_FLOW_PROJECT_ID`.
- Webhook and Worker reject or ignore work outside their configured Project.
- Normal review intake accepts only a valid exact head SHA and never falls back to `merge_commit_sha`.
- Two Worker claims cannot execute one pending Run twice.
- A normal Run uses an exact-SHA worktree and always cleans it up.
- A stale, closed or merged MR cannot trigger notification or Agent execution.
- Agent command receives the documented environment and direct GitLab token context.
- Auto-fix creates a separate MR and never writes directly to the parent source branch.
- Auto-fix MR notification targets only the parent MR creator.
- Auto-fix merge suppresses exactly one parent SHA; a later user SHA triggers normally.
- Feishu and WeCom can both be configured without changing the core workflow.
- A deferred Pipeline Gate or Draft Run does not block later eligible Runs.
- An expired Worker claim is recovered at most three times and stale claim tokens cannot finish a reclaimed Run.
- Notification delivery failure leaves the Review Run successful, retries per channel up to three times, and records terminal failure for audit.

# Implementation status

## Implemented

| Spec area | Current implementation |
| --- | --- |
| Policy defaults/schema subset | `src/review_agent_flow/policy.py` |
| Policy template CLI | `src/review_agent_flow/cli.py` |
| Generic Agent CLI | `src/review_agent_flow/runner.py` |
| OpenAI-compatible Provider protocol | `src/review_agent_flow/provider.py` |
| Exact-SHA availability and worktree helper | `src/review_agent_flow/repository.py`, `worktree.py` |
| SQLite schema v2 idempotency, claim, Auto-fix intent/relation and cleanup | `src/review_agent_flow/store.py` |
| SQLite notification outbox, stable channel IDs and per-channel retry | `src/review_agent_flow/store.py`, `worker.py` |
| GitLab event normalization | `src/review_agent_flow/events.py` |
| GitLab protocol and python-gitlab adapter | `src/review_agent_flow/gitlab.py`, `gitlab_client.py` |
| Feishu/WeCom webhook adapters | `src/review_agent_flow/notifications.py` |
| FastAPI Webhook ingress with required secret and Project binding | `src/review_agent_flow/web.py` |
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
- Two Worker claims cannot execute one pending Run twice.
- A normal Run uses an exact-SHA worktree; cleanup is recorded and retried independently when removal fails.
- Agent command receives the documented environment and direct GitLab token context.
- Auto-fix creates a separate MR and never writes directly to the parent source branch.
- Auto-fix MR notification targets only the parent MR creator.
- Auto-fix merge suppresses exactly one parent SHA; a later user SHA triggers normally.
- Feishu and WeCom can both be configured without changing the core workflow.
- A deferred Pipeline Gate or Draft Run does not block later eligible Runs.
- An expired Worker claim is recovered at most three times and stale claim tokens cannot finish a reclaimed Run.
- Notification delivery failure leaves the Review Run successful, retries per channel up to three times, and records terminal failure for audit.
- A Pipeline Hook never creates a standalone Run; when Gate is enabled it wakes an existing deferred normal Run for the same SHA.
- A title/branch-only Auto-fix lookalike follows the normal path; only a server-owned intent/relation enters the Auto-fix path.
- Auto-fix reconciliation verifies the Run/SHA marker, abandons a terminal intent with no child MR, paginates trusted Review Bot Note markers, and prunes missing worktree metadata.
- A missing local SHA is fetched only through the explicit locked and bounded `ensure_sha` step before worktree creation.

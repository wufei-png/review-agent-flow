# Agent and Provider contract

## Separation

`AIProvider` and `AgentBackend` are different abstractions:

- `AIProvider` is a model inference endpoint using the OpenAI-compatible API shape.
- `AgentBackend` is an executable Agent workflow such as a local CLI or an OpenCode local service.
- The core does not ship vendor-specific model SDKs.
- Deployment documentation recommends LiteLLM as the gateway for model routing, auth, budget and observability.

## Local CLI input

The Worker invokes the configured command with `shell=False`, the Review Run worktree as `cwd`, and these environment variables:

| Variable | Meaning |
| --- | --- |
| `REVIEW_AGENT_FLOW_PROJECT_ID` | GitLab Project identifier |
| `REVIEW_AGENT_FLOW_MR_IID` | Merge Request IID |
| `REVIEW_AGENT_FLOW_RUN_ID` | Numeric Review Run identifier; use it in the Auto-fix branch name |
| `REVIEW_AGENT_FLOW_SHA` | Exact reviewed commit |
| `REVIEW_AGENT_FLOW_GITLAB_URL` | GitLab base URL |
| `REVIEW_AGENT_FLOW_SKILL` | Project Skill path when it exists, otherwise the packaged default Skill |
| `REVIEW_AGENT_FLOW_AUTO_FIX_ENABLED` | `true` only when Project policy enables auto-fix |
| `REVIEW_AGENT_FLOW_AUTO_FIX_BRANCH` | Service-registered Auto-fix branch; present when auto-fix is enabled |
| `REVIEW_AGENT_FLOW_AUTO_FIX_TARGET_BRANCH` | Original MR source branch; the Auto-fix MR must target this branch |
| `REVIEW_AGENT_FLOW_AUTO_FIX_TITLE` | Service-registered Auto-fix title |
| `REVIEW_AGENT_FLOW_AUTO_FIX_MARKER` | Stable intent marker that must be included verbatim in the Auto-fix MR description |
| `REVIEW_AGENT_FLOW_NOTE_MARKER` | Stable `ReviewFlow-Run-ID:<run-id> SHA:<sha>` marker for Note deduplication |
| `GITLAB_TOKEN` | Review Bot token, when injected by runtime |

The command receives no required JSON output contract. The Agent uses the worktree and GitLab context to submit Notes itself. Before posting, it must search for the exact Note marker and must include it in a newly posted Note. When it creates an Auto-fix MR, it must include the exact Auto-fix marker in the MR description; this is the identity check that prevents a rebuilt SQLite database from adopting an old MR with a reused numeric Run ID. The Worker only needs process status, stdout/stderr for diagnostics, timeout handling and lifecycle state.

## Worktree

The Worker ensures the exact SHA is available locally before creating a detached worktree. The Agent must not modify the parent source branch directly. The Worker registers an Auto-fix intent before Agent execution; a terminal Run that produces no child MR closes that intent as abandoned. Auto-fix output is created on the supplied `autofix/MR-<iid>/run-<run-id>` branch and pushed as a separate MR targeting the supplied parent source branch. An Auto-fix-looking title or branch without a matching intent and marker is not trusted.

## Default Skill responsibilities

The built-in Skill tells an Agent to:

1. inspect the exact worktree and MR context;
2. review correctness, security, reliability and maintainability;
3. submit an actionable GitLab Note;
4. never approve or block the parent MR;
5. create an Auto-fix MR only when enabled and safe;
6. notify only the parent MR creator for Auto-fix review.

Project `.gitlab/review-agent/SKILL.md` takes precedence over the built-in Skill.

## Trusted execution

The first version assumes a trusted Worker deployment. It does not attempt to sandbox Agent tools, network or credentials. This is a deployment precondition, not an assertion that arbitrary multi-tenant code is safe.

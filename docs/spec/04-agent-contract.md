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
| `REVIEW_AGENT_FLOW_SHA` | Exact reviewed commit |
| `REVIEW_AGENT_FLOW_GITLAB_URL` | GitLab base URL |
| `REVIEW_AGENT_FLOW_SKILL` | Project Skill path when it exists, otherwise the packaged default Skill |
| `REVIEW_AGENT_FLOW_AUTO_FIX_ENABLED` | `true` only when Project policy enables auto-fix |
| `GITLAB_TOKEN` | Review Bot token, when injected by runtime |

The command receives no required JSON output contract. The Agent uses the worktree and GitLab context to submit Notes itself. The Worker only needs process status, stdout/stderr for diagnostics, timeout handling and lifecycle state.

## Worktree

The Worker creates a detached worktree at the exact SHA. The Agent must not modify the parent source branch directly. Auto-fix output is created on `autofix/MR-<iid>/run-<run-id>` and pushed as a separate MR targeting the parent source branch.

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

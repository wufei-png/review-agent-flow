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
| `REVIEW_AGENT_FLOW_NOTE_MARKER` | Canonical standalone token `<!-- review-run:<run-id>:note -->` that must appear exactly once in the Agent-owned Note body |
| `GITLAB_TOKEN` | Review Bot token, when injected by runtime |

The command receives no required JSON output contract. The Agent uses the worktree and GitLab context to submit Notes itself. The Worker only needs process status, stdout/stderr for diagnostics, timeout handling and lifecycle state.

The Worker persists Effect Receipts for Agent execution, the expected
Agent-owned Note, and (when enabled) the expected Auto-fix MR. These receipts
are an audit and reconciliation surface, not an Agent response schema. After
the Agent exits successfully, the Worker asks the optional GitLab Note observer
to find the Note containing the exact canonical marker. Only a scoped Note
with exactly one marker line and a strict positive-integer native ID becomes
`observed` and receives an external identity. A successful
Agent process does not prove that a Note or Auto-fix MR was observed; a missing
or unavailable Note remains `not_observed`, and the Review Run status is kept
independent from that external side effect.

Auto-fix reconciliation queries the deterministic branch across opened,
closed, and merged Merge Requests. A delayed or temporarily invisible MR
remains a pending receipt for a later Worker cycle. If the MR is merged, the
relation stores its resulting merge SHA and suppresses only that exact parent
SHA. This is at-least-once processing with reconciliation, not exactly-once
external side effects.

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

# Host-native Quick Start

This path runs ReviewFlow directly on one host with OpenCode and a manual MR trigger. It does not use Docker Compose. The safe default publishes one ReviewFlow-owned Note; Auto-fix and notifications remain disabled until explicitly configured.

## 1. Prerequisites

- Python 3.11 or newer, Git, and OpenCode 1.18.11 or newer;
- one non-shallow local clone of the GitLab Project to review;
- a Project Access Token or bot token that can read the repository/MR and create MR Notes;
- an OpenCode model credential exposed through an Operator-approved environment variable.

Install ReviewFlow from this repository:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Create private state outside the reviewed repository:

```bash
install -d -m 0700 "$HOME/.local/state/reviewflow-quickstart"
install -d -m 0700 "$HOME/.local/state/reviewflow-quickstart/worktrees"
```

## 2. Enable AI review in the target Project

Commit this `.gitlab/reviewflow.yml` to the MR target branch. Replace no executable fields here: the Project may request only an Operator-authorized Backend/Profile.

```yaml
version: 4
pipeline_gate: false
human:
  enabled: false
  reviewers: []
ai:
  enabled: true
  backend: opencode-local
  profile: default
  auto_fix: false
notifications:
  channels: []
```

## 3. Create the external Operator configuration

Write `$HOME/.local/state/reviewflow-quickstart/operator.yml`, replacing the OpenCode path, numeric GitLab Project ID, and a model listed by `opencode models`:

```yaml
version: 4
backends:
  opencode-local:
    enabled: true
    adapter: opencode
    command: [/absolute/path/to/opencode]
    allowed_projects: ["12345"]
    profiles:
      default:
        timeout_seconds: 1800
        model: openai/gpt-5.6-terra
        environment: [OPENAI_API_KEY]
autofix_publication:
  enabled: false
  allowed_projects: []
  initial_readiness: draft
  ci_trigger: manual
notifications:
  channels: {}
retention:
  sensitive_payload_days: 30
```

Keep this file outside the reviewed repository and mode `0600`:

```bash
chmod 0600 "$HOME/.local/state/reviewflow-quickstart/operator.yml"
```

The OpenCode adapter uses `run --pure --agent reviewflow`, embeds the Skill frozen from the target-branch commit in the canonical task prompt, disables general file reads and ambient Skill lookup, disables Project-owned OpenCode configuration and sharing, isolates OpenCode state per Run, and never passes `--auto`. Exact review-range Git diff plus grep/LSP remain the source-inspection surface. The Agent receives no GitLab token. The Trusted Workspace is still not an OS sandbox; use a dedicated host identity or stronger host isolation for untrusted deployments.

## 4. Export the bound deployment settings

```bash
export GITLAB_URL="https://gitlab.example.com"
export GITLAB_TOKEN="replace-with-review-bot-token"
export GITLAB_REVIEW_BOT_USERNAME="review-bot"
export REVIEW_AGENT_FLOW_PROJECT_ID="12345"
export REVIEW_AGENT_FLOW_REPOSITORY="/absolute/path/to/non-shallow/project-clone"
export REVIEW_AGENT_FLOW_DB="$HOME/.local/state/reviewflow-quickstart/reviewflow.db"
export REVIEW_AGENT_FLOW_WORKTREES="$HOME/.local/state/reviewflow-quickstart/worktrees"
export REVIEW_AGENT_FLOW_OPERATOR_CONFIG="$HOME/.local/state/reviewflow-quickstart/operator.yml"
export OPENAI_API_KEY="replace-with-provider-secret"
```

The repository remote must be able to fetch the exact MR SHAs using host-native Git credentials. Do not put GitLab or notification credentials in the Operator Backend environment allowlist.

## 5. Trigger and process one MR

Resolve and idempotently enqueue the MR's current head:

```bash
review-agent-flow review-mr --mr-iid 7
```

The JSON response is `enqueued` for a new ReviewKey or `existing` for the same Project/MR/head. This command only reads GitLab and writes local queue state; it does not publish.

Process one item:

```bash
review-agent-flow worker --once
```

The Worker fetches the frozen base/start/head commits, runs OpenCode in a detached worktree, parses the strict Result JSON, and publishes the ReviewFlow-owned MR Note. If GitLab is still preparing `diff_refs`, the Run is deferred without consuming its failure budget; run `worker --once` again later. Repeating `review-mr` or retrying the Worker does not create a second Review Run or duplicate Note for the same ReviewKey.

Auto-fix captures every tracked or non-ignored untracked workspace change. Ensure normal
tool-generated files such as Python `__pycache__/` or build output are covered
by the Project's committed ignore rules; otherwise binary or unrelated output
is deliberately rejected rather than silently omitted from the candidate.

For continuous webhook operation and separate API/Worker identities, continue with [the Linux systemd deployment](deployment-linux-systemd.md).

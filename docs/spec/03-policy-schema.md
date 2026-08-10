# Project and Operator configuration

ReviewFlow has two configuration authorities:

- Project-owned `.gitlab/reviewflow.yml` requests review behavior.
- Operator-owned configuration authorizes executable backends, environment, verification and publication.

Project configuration is untrusted input. It cannot provide commands, credentials, GitLab publication fields or host-isolation settings.

## Project Policy v4

If `.gitlab/reviewflow.yml` is absent, ReviewFlow uses the safe defaults below. It may generate a template but must not rewrite a Project-owned file.

```yaml
version: 4

pipeline_gate: true

human:
  enabled: true
  reviewers:
    - gitlab-username

ai:
  enabled: false
  backend: local-agent
  profile: default
  skill: .gitlab/review-agent/SKILL.md
  auto_fix: false

notifications:
  channels:
    - feishu-reviewers
    - wecom-reviewers
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `version` | integer | `4` | Schema version; every other version is rejected |
| `pipeline_gate` | boolean | `false` | Wait for the latest successful Pipeline before a Review Run |
| `human.enabled` | boolean | `true` | Enable the human notification path |
| `human.reviewers` | list of strings | `[]` | GitLab usernames; no CODEOWNERS lookup in v0.1 |
| `ai.enabled` | boolean | `false` | Request AI Review execution |
| `ai.backend` | non-empty string | none | Operator-defined backend name; required when AI is enabled |
| `ai.profile` | non-empty string | none | Operator-defined profile name; required when AI is enabled |
| `ai.skill` | string | packaged default | Optional regular UTF-8 Skill path read from the frozen target-branch commit |
| `ai.auto_fix` | boolean | `false` | Request an Auto-fix proposal; publication still requires Operator authorization |
| `notifications.channels` | list of strings | `[]` | Stable Operator-defined channel IDs requested by the Project |

Unknown fields, malformed known fields and Policy v1-v3 fail loading. The Worker reads the file from the MR target branch, resolves that branch to an exact commit, and freezes canonical credential-free JSON plus its digest per Run. A configured Project Skill is read as a bounded regular Git blob from that same frozen commit, never from the MR-head worktree; symlinks, non-UTF-8 content and content over 64 KiB are rejected. Its exact content and digest are frozen with the Agent Task Snapshot. A retry never re-reads a moved target branch or changed packaged Skill. This pre-release hard cutover has no legacy compatibility path.

## Operator configuration

`REVIEW_AGENT_FLOW_OPERATOR_CONFIG` must name a readable file outside the reviewed repository. The service loads and validates it strictly at startup; changing the file does not hot-reload a running process.

```yaml
version: 4

backends:
  local-agent:
    adapter: opencode
    command: [opencode]
    allowed_projects: ["42"]
    profiles:
      default:
        timeout_seconds: 1800
        model: openai/gpt-5.6-terra
        environment: [OPENAI_API_KEY]
        verification:
          command: [pytest, -q]

autofix_publication:
  enabled: false
  allowed_projects: ["42"]
  initial_readiness: draft
  ci_trigger: manual

notifications:
  channels:
    feishu-reviewers:
      type: feishu
      webhook_env: FEISHU_WEBHOOK
      allowed_projects: ["42"]
      user_ids:
        gitlab-username: ou_provider_user_id

retention:
  sensitive_payload_days: 30
  publication_plan_backup_dir: /var/lib/review-agent-flow-worker/plan-backups
```

The Operator file is trusted deployment input. Version 4 adds the required `adapter` discriminator. `command` accepts an arbitrary argv for `adapter: command`, but `adapter: opencode` accepts exactly one executable and ReviewFlow appends the native `run --pure --agent reviewflow`, optional Profile `--model`, and canonical prompt. The OpenCode adapter never enables `--auto`: it embeds the complete selected Skill in the canonical prompt, disables Project config and ambient Project instructions, disables sharing, isolates OpenCode config/data/cache under the private Run directory, reserves `OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT` and `OPENCODE_CONFIG_DIR` from Profile injection, selects an inline adapter-owned Agent, disables general file reads and ambient Skill lookup, denies shell commands by default with only exact whole-review-range Git diff forms allowed, denies built-in web tools/subagents/questions, and permits edit-tool workspace changes only when Auto-fix is authorized. Shell permission patterns never contain a trailing wildcard; path-specific source inspection uses grep/LSP rather than an extensible shell command. These permissions are defense in depth, not a host sandbox. Version 3 introduced Operator-owned retention with a 30-day default and an optional absolute Publication Plan backup directory. Backend and verification timeouts are each bounded to 1–1800 seconds so the reference supervisor can enforce a finite graceful-stop envelope. The selected backend/profile and every requested notification channel must exist and allow the bound Project. Provider type, webhook environment name and user-ID mapping are Operator-owned and never read from Project Policy. Environment entries are an allowlist copied from the Worker environment; ReviewFlow additionally supplies the Worker-owned `PATH` (or the platform default) so bare Operator commands and their shebang interpreters resolve without inheriting the rest of the Worker environment. `PATH` is not treated as a secret. ReviewFlow does not inject GitLab publication credentials into the Agent. Auto-fix is allowed only when both Project Policy and Operator configuration enable it for that Project.

`initial_readiness` is `draft` or `ready`. `ci_trigger` is `manual` or `automatic`. The supported combinations are `draft/manual`, `draft/automatic` and `ready/automatic`; `ready/manual` is rejected because a ready MR must not silently skip CI. Manual CI adds `[skip ci]` to the Program-owned commit. These values describe creation only and are frozen into the immutable Publication Plan; later user Draft/Ready changes do not invalidate a Relation.

The Operator may disable a backend or Auto-fix publication as a kill switch. A kill switch prevents new execution/publication; it does not mutate already-persisted history.

## Precedence and secrets

1. Built-in safe defaults.
2. Project Policy requests.
3. Operator authorization and runtime bindings.

Operator authorization can narrow or deny a Project request, never expand it silently. Credentials, GitLab tokens, host paths and executable commands are not valid Project Policy fields. Webhook URLs, GitLab tokens, repository access and Agent environment values come from the Operator environment or a secret manager.

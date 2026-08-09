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
| `ai.skill` | string | packaged default | Optional Project Skill path supplied as Agent input |
| `ai.auto_fix` | boolean | `false` | Request an Auto-fix proposal; publication still requires Operator authorization |
| `notifications.channels` | list of strings | `[]` | Stable Operator-defined channel IDs requested by the Project |

Unknown fields, malformed known fields and Policy v1-v3 fail loading. The Worker reads the file from the MR target branch, resolves that branch to an exact commit, and freezes canonical credential-free JSON plus its digest per Run. A retry never re-reads a moved target branch. This pre-release hard cutover has no legacy compatibility path.

## Operator configuration

`REVIEW_AGENT_FLOW_OPERATOR_CONFIG` must name a readable file outside the reviewed repository. The service loads and validates it strictly at startup; changing the file does not hot-reload a running process.

```yaml
version: 2

backends:
  local-agent:
    command: [opencode, run]
    allowed_projects: ["42"]
    profiles:
      default:
        timeout_seconds: 1800
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
```

The Operator file is trusted deployment input. The selected backend/profile and every requested notification channel must exist and allow the bound Project. Provider type, webhook environment name and user-ID mapping are Operator-owned and never read from Project Policy. Environment entries are an allowlist copied from the Worker environment; ReviewFlow additionally supplies the Worker-owned `PATH` (or the platform default) so bare Operator commands and their shebang interpreters resolve without inheriting the rest of the Worker environment. `PATH` is not treated as a secret. ReviewFlow does not inject GitLab publication credentials into the Agent. Auto-fix is allowed only when both Project Policy and Operator configuration enable it for that Project.

`initial_readiness` is `draft` or `ready`. `ci_trigger` is `manual` or `automatic`. The supported combinations are `draft/manual`, `draft/automatic` and `ready/automatic`; `ready/manual` is rejected because a ready MR must not silently skip CI. Manual CI adds `[skip ci]` to the Program-owned commit. These values describe creation only and are frozen into the immutable Publication Plan; later user Draft/Ready changes do not invalidate a Relation.

The Operator may disable a backend or Auto-fix publication as a kill switch. A kill switch prevents new execution/publication; it does not mutate already-persisted history.

## Precedence and secrets

1. Built-in safe defaults.
2. Project Policy requests.
3. Operator authorization and runtime bindings.

Operator authorization can narrow or deny a Project request, never expand it silently. Credentials, GitLab tokens, host paths and executable commands are not valid Project Policy fields. Webhook URLs, GitLab tokens, repository access and Agent environment values come from the Operator environment or a secret manager.

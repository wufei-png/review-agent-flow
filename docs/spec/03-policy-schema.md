# Project Policy schema

The optional file is `.gitlab/reviewflow.yml`. If absent, the service uses the safe defaults below. The service may generate a template but must not rewrite a Project-owned file.

## Example

```yaml
version: 1

pipeline_gate: true

human:
  enabled: true
  reviewers:
    - gitlab-username

ai:
  enabled: false
  command:
    - opencode
    - run
  skill: .gitlab/review-agent/SKILL.md
  auto_fix: false

notifications:
  channels:
    - id: feishu-reviewers
      type: feishu
      webhook_env: FEISHU_WEBHOOK
    - id: wecom-reviewers
      type: wecom
      webhook_env: WECOM_WEBHOOK
```

## Fields

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `version` | integer | `1` | Schema version; unknown versions are rejected |
| `pipeline_gate` | boolean | `false` | Wait for the latest successful Pipeline before a normal Run |
| `human.enabled` | boolean | `true` | Enable human notification/review path |
| `human.reviewers` | list of strings | `[]` | GitLab usernames; no CODEOWNERS lookup in v0.1 |
| `ai.enabled` | boolean | `false` | Enable Agent Review path |
| `ai.command` | string or list | `[]` | Local Agent CLI command; required when AI is enabled |
| `ai.skill` | string | `null` | Project Skill path overriding the built-in Skill |
| `ai.auto_fix` | boolean | `false` | Allow Agent to create Auto-fix MR |
| `notifications.channels` | list of mappings | `[]` | Feishu/WeCom channel definitions |
| `notifications.channels[].id` | non-empty string | required | Stable channel identity; unique within the Project |
| `notifications.channels[].type` | `feishu` or `wecom` | required | Notification provider |
| `notifications.channels[].webhook_env` | non-empty string | required | Runtime environment variable containing the webhook URL |
| `notifications.channels[].user_ids` | mapping of strings | `{}` | Optional GitLab username to provider user-ID mapping |

For version 1, unknown fields and malformed known fields must fail startup or policy loading rather than silently changing behavior. Future schema versions may define their own compatibility rules.

## Precedence

1. Built-in safe defaults.
2. Optional Project `.gitlab/reviewflow.yml` values.
3. Explicit runtime/operator settings that do not mutate Project policy, such as database path, webhook secret, bound Project ID and worker polling interval.

v0.1 has no `mode`, `approvals_required` or approval/enforcement policy field. Those concepts require a separate versioned contract.

Project policy must not contain credentials. Webhook URLs, GitLab tokens and channel secrets come from the runtime environment or a secret manager.

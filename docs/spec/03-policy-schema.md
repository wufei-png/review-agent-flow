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
    - type: feishu
      webhook_env: FEISHU_WEBHOOK
    - type: wecom
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

Unknown fields should be ignored only when they belong to a future schema version. For version 1, malformed known fields must fail startup or policy loading rather than silently changing behavior.

## Precedence

1. Built-in safe defaults.
2. Optional Project `.gitlab/reviewflow.yml` values.
3. Explicit runtime/operator settings that do not mutate Project policy, such as database path, webhook secret and worker polling interval.

Project policy must not contain credentials. Webhook URLs, GitLab tokens and channel secrets come from the runtime environment or a secret manager.

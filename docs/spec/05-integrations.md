# Integration specification

## GitLab ingress

The API accepts GitLab Merge Request Hooks at `POST /webhooks/gitlab` and checks `X-Gitlab-Token` when `GITLAB_WEBHOOK_SECRET` is configured. It must use the Project ID, MR IID and exact head SHA from the payload. Missing identity or SHA is rejected without enqueueing work.

The current endpoint is intentionally small. The remaining GitLab adapter must provide:

- MR fetch and source/target branch details;
- Pipeline status for Pipeline Gate;
- Note creation;
- reviewer lookup/mention data;
- branch push and Auto-fix MR creation;
- parent/Auto-fix MR merge relationship lookup.

The adapter should use `python-gitlab` behind a narrow interface so the domain and Worker do not depend on GitLab SDK objects.

## Human notifications

The first release supports both Feishu and WeCom. A Project may configure both channels. Normal human review can notify configured reviewers; Auto-fix MR notifications go only to the original parent MR creator. Channel credentials and identity mappings are runtime secrets/configuration, not committed policy content. Notification payloads include the event title, body and optional URL.

The Worker writes one SQLite outbox record per configured channel before attempting delivery. A channel failure does not fail or roll back the Review Run, and does not prevent another channel from being delivered. Failed deliveries retry up to three attempts with exponential delay and remain auditable as `failed` after the final attempt. Delivery is at-least-once across a Worker crash between provider delivery and outbox acknowledgement, so providers may receive a duplicate.

Each notifier should implement a small common interface such as:

```python
notify(event: NotificationEvent) -> NotificationResult
```

The event should contain Project/MR identity, recipient identity, title, URL, summary and correlation ID. Provider-specific mention markup stays inside the adapter.

## OpenCode and other Agents

OpenCode Web is an operator-facing local UI. Automated execution should use a CLI or a programmatic local endpoint such as OpenCode Server/ACP. Other tools (`claude`, `codex`, and future Agents) are supported through the generic configured CLI command before dedicated adapters are justified.

## Model layer

The core only defines an OpenAI-compatible Provider contract. LiteLLM is a deployment recommendation and should be documented as an external gateway, not imported by the core package.

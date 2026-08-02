---
status: accepted
date: 2026-08-02
---

# Separate liveness, readiness, and live provider probes

ReviewFlow exposes process liveness separately from operational readiness.
`/healthz` reports that the API process is alive. `/readyz` checks required
configuration, the bound Project identity, and SQLite read/write availability;
it does not call GitLab by default. A live GitLab/provider probe is explicit so
short provider outages do not cause an orchestrator to repeatedly remove and
restart an otherwise healthy webhook process.

## Implementation contract

- `GET /health` remains the compatibility endpoint and returns
  `{"status":"ok"}`.
- `GET /healthz` returns HTTP 200 with `{"status":"alive"}`.
- `GET /readyz` returns HTTP 200 with `{"status":"ready",...}` only when
  required configuration, the bound Project and a SQLite temporary-table
  read/write probe pass. Failure returns HTTP 503 with
  `{"status":"not_ready",...}`.
- `GET /probes/gitlab` is the explicit live-probe surface. It accepts an
  injected `LiveGitLabProbe`; when no probe is injected it returns HTTP 503
  with `reason=not_configured`. It requires the
  `X-ReviewFlow-Internal-Token` header whose value is the configured
  `GITLAB_WEBHOOK_SECRET`; missing or incorrect tokens return HTTP 401 before
  invoking the probe. `/readyz` never calls this interface.

The status HTTP routes require the same internal token and are also read-only
and Project-bound. They expose a whitelisted snapshot rather than serializing
internal records, so `claim_token` and provider/notification credentials
cannot cross the API boundary. The CLI status query remains local and
read-only.

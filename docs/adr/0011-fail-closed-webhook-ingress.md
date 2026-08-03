---
status: accepted
date: 2026-08-02
---

# Fail-closed webhook ingress

ReviewFlow requires `GITLAB_WEBHOOK_SECRET` before the service starts because
an unauthenticated webhook could enqueue work for an Agent that inherits
trusted host capabilities and credentials. Tests and explicitly embedded
local callers may inject a secret into `create_app`, but there is no anonymous
webhook mode.

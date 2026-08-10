---
status: accepted
date: 2026-08-10
---

# Freeze target-branch Policy and keep notification bindings Operator-owned

Project Policy authority is the parent MR target branch, not the Worker's local
checkout or the source branch under review. The Worker resolves the target
branch to an exact commit, reads `.gitlab/reviewflow.yml` at that commit and
freezes canonical Policy JSON and its digest for the Review Run. Retries reuse
that snapshot even if the target branch later moves.

Policy v4 may request notification channel IDs only. Operator configuration v4
owns provider type, webhook environment binding, Project allowlist and provider
user-ID mapping. A missing or unauthorized requested channel fails closed
before a user-visible side effect. Provider responses are bounded before JSON
parsing.

The same pre-effect transaction boundary freezes Requested Outputs, the
Operator-narrowed Effective Run Decision and Note/per-channel Output
Expectations. Review Outcome is projected from those immutable records and the
durable Note/outbox state as `pending`, `no_effect`, `delivered`, `degraded`,
`undelivered` or `indeterminate`; Run Lifecycle remains stable.

This is a pre-production hard cutover. Policy v1-v3, Operator v1-v3 and SQLite
schema versions other than the current development version 15 are rejected
rather than migrated.

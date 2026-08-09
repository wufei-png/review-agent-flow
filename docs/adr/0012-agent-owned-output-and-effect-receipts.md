---
status: superseded by ADR-0019
date: 2026-08-02
---

# Keep Agent-owned output and add effect receipts

The Agent continues to submit GitLab Notes and create Auto-fix Merge
Requests directly; the generic CLI contract does not require a JSON result.
ReviewFlow adds durable effect receipts and reconciliation keyed by the
Review Run, a canonical deterministic Note marker, and platform identities. This preserves
existing Agent compatibility while making replay and duplicate side effects
auditable; it deliberately provides at-least-once behavior with reconciliation,
not an exactly-once guarantee. This decision targets a trusted internal Project
deployment: the Operator accepts that Project Policy and admitted MR content can
influence a credentialed Agent. Environment minimization, Operator-owned backend
selection and process isolation harden that deployment, but do not turn it into
an unattended execution boundary for arbitrary untrusted contributions.

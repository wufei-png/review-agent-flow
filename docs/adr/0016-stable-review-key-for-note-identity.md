---
status: accepted
date: 2026-08-08
---

# Use the stable Review Key for Note identity

A Review Bot Note is identified by the logical Review Key for its exact
Project, Merge Request, SHA and review kind, rather than by a local numeric or
random Run identifier. A correctly scoped Note from the trusted Review Bot can
therefore satisfy the same review after the local Store is rebuilt. Run
identifiers remain useful for tracing attempts but do not define external Note
idempotency.

The canonical standalone Note marker is scoped to the configured GitLab
instance and has this form:

`<!-- reviewflow:v1;project=<project-id>;mr=<iid>;sha=<sha>;kind=<kind>;effect=note -->`

The current `<!-- review-run:<run-id>:note -->` marker remains legacy evidence
for an intact Store, but cannot prove a Review Key after Store loss and is not
eligible for recovery deduplication. The stable marker migration is accepted
but not yet implemented in the Agent, observer, receipts or tests.

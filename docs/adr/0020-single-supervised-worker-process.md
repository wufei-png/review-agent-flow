---
status: accepted
date: 2026-08-09
supersedes: ADR-0008
---

# Run independent responsibilities in one supervised Worker process

The v0.1 reference deployment uses one supervised Worker OS process with
independent Review/Publication, Notification, and Reconciliation/Cleanup
loops. Each loop owns its polling, claims, leases, retries, and per-item error
handling; if a loop itself exits unexpectedly, the whole Worker exits so an
external supervisor restarts a complete service rather than leaving a silent
partial Worker.

This keeps the single-Project SQLite deployment operationally small while
preventing long Agent execution from starving notification or maintenance
work. Dispatcher, Reconciler, and cleanup remain separate components and Store
interfaces, so they may become independent OS processes when deployments need
separate scaling or fault domains and have adopted a storage topology suitable
for that concurrency.

## Considered options

- Separate v0.1 OS processes were rejected because they add deployment,
  credential, health-check, and SQLite contention costs without a current
  single-Project scaling requirement.
- One sequential loop was rejected because long reviews delay unrelated
  delivery and reconciliation.

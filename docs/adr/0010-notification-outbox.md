# Notification outbox is independent of Review Runs

Notification delivery is a side effect of a Review Run, not part of its correctness result. A failed Feishu or WeCom request must not turn a successful human/AI Review Run into a failed Run or prevent another configured channel from being attempted.

The Worker persists one outbox record for each configured channel before delivery. Records have their own attempts, lease, claim token, backoff, terminal status and last error. Failed records retry three times by default and remain auditable after the final failure. A correlation ID makes repeated processing of the same Review Run idempotent, including a Worker crash after provider delivery but before the outbox update.

The delivery contract is at-least-once: a crash in that window may produce a duplicate provider message. Exactly-once delivery is not assumed because Feishu and WeCom webhooks do not provide a shared transaction with SQLite.

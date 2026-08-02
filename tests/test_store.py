import sqlite3

import pytest

from review_agent_flow.store import SQLiteStore

SHA = "a" * 40
OLD_SHA = "b" * 40
NEW_SHA = "c" * 40
MERGED_SHA = "d" * 40
LATER_SHA = "e" * 40


def test_enqueue_is_idempotent_and_claimable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    first = store.enqueue("42", 1, SHA)
    duplicate = store.enqueue("42", 1, SHA)
    assert duplicate.run_id == first.run_id
    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.run_id == first.run_id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.claim_token
    assert claimed.lease_until
    assert store.claim_next() is None


def test_deferred_run_does_not_block_a_later_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    first = store.enqueue("42", 1, OLD_SHA)
    second = store.enqueue("42", 2, NEW_SHA)
    claimed = store.claim_next()
    assert claimed is not None and claimed.run_id == first.run_id
    assert store.requeue(
        claimed.run_id,
        "waiting for successful pipeline",
        delay_seconds=3600,
        claim_token=claimed.claim_token,
    )
    next_run = store.claim_next()
    assert next_run is not None
    assert next_run.run_id == second.run_id


def test_expired_worker_lease_is_reclaimed_with_a_new_token(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next(lease_seconds=60)
    assert claimed is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_runs SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )
    reclaimed = store.claim_next(lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.run_id == run.run_id
    assert reclaimed.attempts == 2
    assert reclaimed.claim_token != claimed.claim_token
    assert store.finish(run.run_id, "succeeded", claim_token=claimed.claim_token) is False
    assert store.finish(run.run_id, "succeeded", claim_token=reclaimed.claim_token) is True


def test_legacy_running_run_without_a_lease_is_recovered(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_runs SET status = 'running', attempts = 0, lease_until = NULL WHERE id = ?",
            (run.run_id,),
        )
    reclaimed = store.claim_next(lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.run_id == run.run_id
    assert reclaimed.attempts == 1


def test_expired_worker_lease_fails_after_max_attempts(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    for _ in range(3):
        claimed = store.claim_next(lease_seconds=60, max_attempts=3)
        assert claimed is not None
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE review_runs SET lease_until = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", run.run_id),
            )
    assert store.claim_next(lease_seconds=60, max_attempts=3) is None
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status, error FROM review_runs WHERE id = ?", (run.run_id,)
        ).fetchone()
    assert row == ("failed", "worker lease expired")


def test_project_scoped_claim_does_not_recover_another_project_lease(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    project_a_run = store.enqueue("A", 1, SHA)
    project_b_run = store.enqueue("B", 1, OLD_SHA)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE review_runs
            SET status = 'running', attempts = 3, lease_until = ?, claim_token = ?
            WHERE id = ?
            """,
            ("2000-01-01T00:00:00+00:00", "project-b-token", project_b_run.run_id),
        )

    claimed = store.claim_next(project_id="A", max_attempts=3)

    assert claimed is not None
    assert claimed.run_id == project_a_run.run_id
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status, attempts, lease_until FROM review_runs WHERE id = ?",
            (project_b_run.run_id,),
        ).fetchone()
    assert row == ("running", 3, "2000-01-01T00:00:00+00:00")


def test_effect_receipt_claim_reclaim_is_project_scoped(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    project_a_run = store.enqueue("A", 1, SHA)
    project_b_run = store.enqueue("B", 1, OLD_SHA)
    project_a_receipt = store.create_effect_receipt(
        project_a_run.run_id,
        "autofix_reconciliation",
        "autofix/MR-1/run-1",
        state="pending",
    )
    project_b_receipt = store.create_effect_receipt(
        project_b_run.run_id,
        "autofix_reconciliation",
        "autofix/MR-1/run-2",
        state="running",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE effect_receipts
            SET lease_until = ?, claim_token = ?
            WHERE id = ?
            """,
            ("2000-01-01T00:00:00+00:00", "project-b-token", project_b_receipt.receipt_id),
        )

    claimed = store.claim_pending_effect_receipts(
        "autofix_reconciliation",
        project_id="A",
    )

    assert [receipt.receipt_id for receipt in claimed] == [project_a_receipt.receipt_id]
    assert claimed[0].state == "running"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state, claim_token FROM effect_receipts WHERE id = ?",
            (project_b_receipt.receipt_id,),
        ).fetchone()
    assert row == ("running", "project-b-token")


def test_effect_receipt_crash_reclaim_stops_at_max_attempts(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    receipt = store.create_effect_receipt(
        run.run_id,
        "autofix_reconciliation",
        "autofix/MR-1/run-1",
        state="pending",
    )

    first_claim = store.claim_pending_effect_receipts(
        "autofix_reconciliation", max_attempts=3, project_id="42"
    )
    assert len(first_claim) == 1
    assert first_claim[0].attempt == 2

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE effect_receipts SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", receipt.receipt_id),
        )

    second_claim = store.claim_pending_effect_receipts(
        "autofix_reconciliation", max_attempts=3, project_id="42"
    )
    assert len(second_claim) == 1
    assert second_claim[0].attempt == 3

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE effect_receipts SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", receipt.receipt_id),
        )

    assert (
        store.claim_pending_effect_receipts(
            "autofix_reconciliation", max_attempts=3, project_id="42"
        )
        == ()
    )
    exhausted = store.get_effect_receipt(receipt.receipt_id)
    assert exhausted is not None
    assert exhausted.state == "not_observed"
    assert exhausted.attempt == 3
    assert exhausted.claim_token is None
    assert exhausted.lease_until is None
    assert exhausted.error == "effect receipt attempt limit reached after lease expiry"


def test_pending_effect_receipt_at_max_attempts_is_not_claimed(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    receipt = store.create_effect_receipt(
        run.run_id,
        "autofix_reconciliation",
        "autofix/MR-1/run-1",
        state="pending",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE effect_receipts SET attempt = ?, next_attempt_at = NULL WHERE id = ?",
            (2, receipt.receipt_id),
        )

    assert (
        store.claim_pending_effect_receipts(
            "autofix_reconciliation", max_attempts=2, project_id="42"
        )
        == ()
    )
    exhausted = store.get_effect_receipt(receipt.receipt_id)
    assert exhausted is not None
    assert exhausted.state == "not_observed"
    assert exhausted.next_attempt_at is None


def test_auto_fix_merge_suppresses_only_the_merged_parent_sha(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    relation = store.record_auto_fix("42", 1, SHA, run.run_id, "42", 9)
    assert relation.parent_mr_iid == 1
    assert store.is_suppressed_parent_sha("42", 1, SHA) is False
    store.mark_auto_fix_merged("42", 9, MERGED_SHA)
    assert store.is_suppressed_parent_sha("42", 1, MERGED_SHA) is True
    assert store.is_suppressed_parent_sha("42", 1, LATER_SHA) is False


def test_closed_auto_fix_merged_sha_is_not_recorded_or_suppressed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)

    relation = store.record_auto_fix(
        "42",
        1,
        SHA,
        run.run_id,
        "42",
        9,
        status="closed",
        merged_sha=MERGED_SHA,
    )

    assert relation.status == "closed"
    assert relation.merged_sha is None
    assert store.is_suppressed_parent_sha("42", 1, MERGED_SHA) is False


def test_auto_fix_merge_before_relation_write_is_preserved(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    store.mark_auto_fix_merged("42", 9, MERGED_SHA)
    run = store.enqueue("42", 1, SHA)
    relation = store.record_auto_fix("42", 1, SHA, run.run_id, "42", 9)
    assert relation.status == "merged"
    assert relation.merged_sha == MERGED_SHA
    assert store.is_suppressed_parent_sha("42", 1, MERGED_SHA) is True


def test_merged_auto_fix_sha_is_backfilled_without_changing_existing_sha(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    initial = store.record_auto_fix(
        "42", 1, SHA, run.run_id, "42", 9, status="merged"
    )
    assert initial.status == "merged"
    assert initial.merged_sha is None

    backfilled = store.record_auto_fix(
        "42", 1, SHA, run.run_id, "42", 9, status="merged", merged_sha=MERGED_SHA
    )
    assert backfilled.status == "merged"
    assert backfilled.merged_sha == MERGED_SHA
    assert store.is_suppressed_parent_sha("42", 1, MERGED_SHA) is True

    preserved = store.record_auto_fix(
        "42", 1, SHA, run.run_id, "42", 9, status="merged", merged_sha=LATER_SHA
    )
    assert preserved.merged_sha == MERGED_SHA
    assert store.is_suppressed_parent_sha("42", 1, LATER_SHA) is False


def test_invalid_sha_is_rejected_before_enqueue(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    with pytest.raises(ValueError, match="invalid SHA"):
        store.enqueue("42", 1, "abc")
    assert store.claim_next() is None


def test_notification_outbox_is_idempotent_per_channel(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    first = store.enqueue_notifications(
        "review-run:1:human",
        "Review requested",
        "Please review",
        "https://mr",
        ("alice",),
        ((0, "feishu"), (1, "wecom")),
    )
    duplicate = store.enqueue_notifications(
        "review-run:1:human",
        "Review requested",
        "Please review",
        "https://mr",
        ("alice",),
        ((0, "feishu"), (1, "wecom")),
    )
    assert [item.delivery_id for item in duplicate] == [item.delivery_id for item in first]
    claimed = store.claim_next_notification()
    assert claimed is not None
    assert claimed.channel == "feishu"
    assert claimed.recipients == ("alice",)
    assert claimed.attempts == 1
    assert store.finish_notification(
        claimed.delivery_id, True, claim_token=claimed.claim_token
    )
    next_delivery = store.claim_next_notification()
    assert next_delivery is not None
    assert next_delivery.channel == "wecom"


def test_notification_outbox_retries_then_records_terminal_failure(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    deliveries = store.enqueue_notifications(
        "review-run:2:autofix",
        "Auto-fix review",
        "Please review",
        None,
        ("alice",),
        ((0, "feishu"),),
    )
    delivery_id = deliveries[0].delivery_id
    for attempt in range(3):
        claimed = store.claim_next_notification()
        assert claimed is not None
        assert claimed.delivery_id == delivery_id
        assert claimed.attempts == attempt + 1
        assert store.finish_notification(
            delivery_id,
            False,
            "timeout",
            claim_token=claimed.claim_token,
            retry_delay_seconds=0,
        )
    terminal = store.get_notification_delivery(delivery_id)
    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.attempts == 3
    assert terminal.last_error == "timeout"
    assert store.claim_next_notification() is None


def test_stale_notification_claim_cannot_finish_reclaimed_delivery(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    deliveries = store.enqueue_notifications(
        "review-run:3:human",
        "Review requested",
        "Please review",
        None,
        (),
        ((0, "feishu"),),
    )
    first = store.claim_next_notification(lease_seconds=60)
    assert first is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE notification_outbox SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", deliveries[0].delivery_id),
        )
    reclaimed = store.claim_next_notification(lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.claim_token != first.claim_token
    assert store.finish_notification(
        deliveries[0].delivery_id, True, claim_token=first.claim_token
    ) is False
    assert store.finish_notification(
        deliveries[0].delivery_id, True, claim_token=reclaimed.claim_token
    ) is True


def test_review_lease_heartbeat_requires_current_unexpired_claim_token(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next(lease_seconds=60)
    assert claimed is not None and claimed.claim_token
    old_lease = claimed.lease_until

    assert store.extend_lease(run.run_id, claimed.claim_token, lease_seconds=120) is True
    renewed = store.get_review_run(run.run_id)
    assert renewed is not None
    assert renewed.lease_until != old_lease
    assert store.extend_lease(run.run_id, "stale-token", lease_seconds=120) is False

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_runs SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )
    assert store.extend_lease(run.run_id, claimed.claim_token, lease_seconds=120) is False
    takeover = store.claim_next(lease_seconds=60)
    assert takeover is not None
    assert takeover.claim_token != claimed.claim_token
    assert store.finish(run.run_id, "succeeded", claim_token=claimed.claim_token) is False


def test_expired_run_token_cannot_update_receipt_or_finish(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next(lease_seconds=60)
    assert claimed is not None and claimed.claim_token
    receipt = store.create_effect_receipt(
        run.run_id,
        "agent_execution",
        "review-run:1:agent",
        state="running",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_runs SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )

    assert (
        store.update_effect_receipt(
            receipt.receipt_id,
            state="succeeded",
            run_id=run.run_id,
            run_claim_token=claimed.claim_token,
        )
        is False
    )
    assert store.finish(run.run_id, "succeeded", claim_token=claimed.claim_token) is False
    saved_receipt = store.get_effect_receipt(receipt.receipt_id)
    assert saved_receipt is not None
    assert saved_receipt.state == "running"
    saved_run = store.get_review_run(run.run_id)
    assert saved_run is not None
    assert saved_run.status == "running"


def test_effect_receipt_is_idempotent_queryable_and_redacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "super-secret-token")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    receipt = store.create_effect_receipt(
        run.run_id,
        "agent_execution",
        "review-run:1:agent",
        state="running",
        detail="Agent execution",
    )
    duplicate = store.create_effect_receipt(
        run.run_id,
        "agent_execution",
        "review-run:1:agent",
        state="planned",
    )
    assert duplicate.receipt_id == receipt.receipt_id
    assert store.update_effect_receipt(
        receipt.receipt_id,
        state="failed",
        external_identity="https://gitlab.example/api?access_token=super-secret-token",
        error="authorization: Bearer super-secret-token glpat-abc123",
    )
    saved = store.get_effect_receipt(receipt.receipt_id)
    assert saved is not None
    assert saved.state == "failed"
    assert "super-secret-token" not in (saved.external_identity or "")
    assert "glpat-abc123" not in (saved.error or "")
    assert "Bearer super-secret-token" not in (saved.error or "")
    assert store.list_effect_receipts(run.run_id)[0].attempt == 1


def test_runtime_redaction_secret_covers_plain_audit_text(tmp_path) -> None:
    token = "runtime-store-token-123"
    store = SQLiteStore(tmp_path / "review.db", redaction_secrets=(token,))
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next()
    assert claimed is not None

    receipt = store.create_effect_receipt(
        run.run_id,
        "agent_execution",
        "plain-effect",
        state="failed",
        error=f"plain text {token} without assignment or URL",
    )
    assert store.finish(
        run.run_id,
        "failed",
        f"plain text {token} without assignment or URL",
        claim_token=claimed.claim_token,
        failure_class="agent_exit",
    )

    saved_run = store.get_review_run(run.run_id)
    assert saved_run is not None
    assert token not in (saved_run.error or "")
    failures = store.list_failures(run.run_id)
    assert len(failures) == 1
    assert token not in (failures[0].error or "")
    saved_receipt = store.get_effect_receipt(receipt.receipt_id)
    assert saved_receipt is not None
    assert token not in (saved_receipt.error or "")

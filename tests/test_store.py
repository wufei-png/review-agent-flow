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


def test_auto_fix_merge_suppresses_only_the_merged_parent_sha(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    relation = store.record_auto_fix("42", 1, SHA, run.run_id, "42", 9)
    assert relation.parent_mr_iid == 1
    assert store.is_suppressed_parent_sha("42", 1, SHA) is False
    store.mark_auto_fix_merged("42", 9, MERGED_SHA)
    assert store.is_suppressed_parent_sha("42", 1, MERGED_SHA) is True
    assert store.is_suppressed_parent_sha("42", 1, LATER_SHA) is False


def test_auto_fix_merge_before_relation_write_is_preserved(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    store.mark_auto_fix_merged("42", 9, MERGED_SHA)
    run = store.enqueue("42", 1, SHA)
    relation = store.record_auto_fix("42", 1, SHA, run.run_id, "42", 9)
    assert relation.status == "merged"
    assert relation.merged_sha == MERGED_SHA
    assert store.is_suppressed_parent_sha("42", 1, MERGED_SHA) is True


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

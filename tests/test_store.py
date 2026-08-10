import sqlite3

import pytest

from review_agent_flow.autofix_planner import canonical_json
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


def test_sha_identity_is_case_insensitive(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    uppercase_sha = "ABCDEF" * 6 + "ABCD"

    first = store.enqueue("42", 1, uppercase_sha)
    duplicate = store.enqueue("42", 1, uppercase_sha.lower())

    assert duplicate.run_id == first.run_id
    assert first.sha == uppercase_sha.lower()


def test_review_run_schema_and_status_have_no_kind_discriminator(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(review_runs)").fetchall()
        }

    assert "kind" not in columns
    assert "kind" not in store.get_project_bound_review_run_status("42", run.run_id)


def test_policy_snapshot_is_exact_and_immutable_per_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next()
    assert claimed is not None

    snapshot = store.freeze_policy_snapshot(
        run.run_id,
        claimed.claim_token,
        target_branch="main",
        target_sha=OLD_SHA,
        policy_version=4,
        policy_json='{"version":4}',
    )

    assert snapshot is not None
    assert snapshot.target_sha == OLD_SHA
    assert store.freeze_policy_snapshot(
        run.run_id,
        claimed.claim_token,
        target_branch="main",
        target_sha=OLD_SHA,
        policy_version=4,
        policy_json='{"version":4}',
    ) == snapshot
    with pytest.raises(ValueError, match="immutable"):
        store.freeze_policy_snapshot(
            run.run_id,
            claimed.claim_token,
            target_branch="main",
            target_sha=NEW_SHA,
            policy_version=4,
            policy_json='{"version":4}',
        )


def test_agent_task_snapshot_is_exact_and_immutable_per_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next()
    assert claimed is not None

    values = {
        "project_id": "42",
        "mr_iid": 1,
        "title": "Review exact diff",
        "source_project_id": "42",
        "source_branch": "feature",
        "target_branch": "main",
        "base_sha": OLD_SHA,
        "start_sha": NEW_SHA,
        "head_sha": SHA,
    }
    snapshot = store.freeze_agent_task_snapshot(
        run.run_id, claimed.claim_token, **values
    )

    assert snapshot is not None and snapshot.base_sha == OLD_SHA
    assert store.freeze_agent_task_snapshot(
        run.run_id, claimed.claim_token, **values
    ) == snapshot
    assert store.requeue(
        run.run_id, "transient worktree failure", claim_token=claimed.claim_token
    )
    retried_claim = store.claim_next()
    assert retried_claim is not None
    retried = store.freeze_agent_task_snapshot(
        run.run_id,
        retried_claim.claim_token,
        **{**values, "title": "Edited title", "base_sha": LATER_SHA},
    )
    assert retried == snapshot


def test_review_outcome_tracks_durable_notification_delivery(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next()
    assert claimed is not None
    assert store.freeze_requested_outputs(
        run.run_id,
        claimed.claim_token,
        human_review=True,
        ai_review=False,
        auto_fix=False,
        notification_channels=("reviewers",),
    )
    assert store.freeze_effective_run_decision(
        run.run_id,
        claimed.claim_token,
        decision="execute",
        human_review=True,
        ai_review=False,
        auto_fix=False,
        suppressed=False,
        recipients=("alice",),
        notification_channels=("reviewers",),
        expectations=(("notification", "reviewers"),),
    )
    assert store.finish(run.run_id, "succeeded", claim_token=claimed.claim_token)
    assert store.project_review_outcome(run.run_id) == "undelivered"

    delivery = store.enqueue_notifications(
        f"review-run:{run.run_id}:human",
        "Review",
        "Please review",
        None,
        ("alice",),
        (("reviewers", "feishu"),),
    )[0]
    assert store.project_review_outcome(run.run_id) == "pending"
    claimed_delivery = store.claim_notification(delivery.delivery_id)
    assert claimed_delivery is not None
    assert store.finish_notification(
        delivery.delivery_id,
        True,
        claim_token=claimed_delivery.claim_token,
    )
    assert store.project_review_outcome(run.run_id) == "delivered"


def test_review_outcome_distinguishes_degraded_no_effect_and_indeterminate(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "review.db")

    degraded = store.enqueue("42", 1, SHA)
    claimed = store.claim_next()
    assert claimed is not None
    assert store.freeze_requested_outputs(
        degraded.run_id,
        claimed.claim_token,
        human_review=True,
        ai_review=False,
        auto_fix=False,
        notification_channels=("one", "two"),
    )
    assert store.freeze_effective_run_decision(
        degraded.run_id,
        claimed.claim_token,
        decision="execute",
        human_review=True,
        ai_review=False,
        auto_fix=False,
        suppressed=False,
        recipients=("alice",),
        notification_channels=("one", "two"),
        expectations=(("notification", "one"), ("notification", "two")),
    )
    deliveries = store.enqueue_notifications(
        f"review-run:{degraded.run_id}:human",
        "Review",
        "Please review",
        None,
        ("alice",),
        (("one", "feishu"), ("two", "wecom")),
    )
    assert store.finish(degraded.run_id, "succeeded", claim_token=claimed.claim_token)
    for delivery, delivered in zip(deliveries, (True, False), strict=True):
        claimed_delivery = store.claim_notification(delivery.delivery_id, max_attempts=1)
        assert claimed_delivery is not None
        assert store.finish_notification(
            delivery.delivery_id,
            delivered,
            claim_token=claimed_delivery.claim_token,
            max_attempts=1,
        )
    assert store.project_review_outcome(degraded.run_id) == "degraded"

    no_effect = store.enqueue("42", 2, NEW_SHA)
    claimed = store.claim_next()
    assert claimed is not None
    assert store.freeze_requested_outputs(
        no_effect.run_id,
        claimed.claim_token,
        human_review=False,
        ai_review=False,
        auto_fix=False,
        notification_channels=(),
    )
    assert store.freeze_effective_run_decision(
        no_effect.run_id,
        claimed.claim_token,
        decision="execute",
        human_review=False,
        ai_review=False,
        auto_fix=False,
        suppressed=False,
        recipients=(),
        notification_channels=(),
    )
    assert store.finish(no_effect.run_id, "succeeded", claim_token=claimed.claim_token)
    assert store.project_review_outcome(no_effect.run_id) == "no_effect"

    indeterminate = store.enqueue("42", 3, MERGED_SHA)
    claimed = store.claim_next()
    assert claimed is not None
    assert store.freeze_requested_outputs(
        indeterminate.run_id,
        claimed.claim_token,
        human_review=True,
        ai_review=False,
        auto_fix=False,
        notification_channels=("reviewers",),
    )
    assert store.finish(indeterminate.run_id, "failed", claim_token=claimed.claim_token)
    assert store.project_review_outcome(indeterminate.run_id) == "indeterminate"


def test_fresh_schema_has_no_agent_owned_autofix_tables(tmp_path) -> None:
    database = tmp_path / "review.db"
    SQLiteStore(database)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {"auto_fix_intents", "auto_fix_mrs", "auto_fix_merge_events"}.isdisjoint(
        tables
    )
    assert {
        "auto_fix_publication_intents",
        "auto_fix_publication_plans",
        "auto_fix_relations",
    } <= tables


@pytest.mark.parametrize("read_only", [False, True])
def test_legacy_schema_is_rejected_for_writable_and_read_only_access(
    tmp_path, read_only: bool
) -> None:
    database = tmp_path / f"legacy-{read_only}.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_record(id INTEGER PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeError, match="unsupported SQLite schema version 2"):
        SQLiteStore(database, read_only=read_only)


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


def test_eligibility_deferrals_do_not_consume_failure_retry_budget(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)

    for _ in range(4):
        claimed = store.claim_next()
        assert claimed is not None
        assert store.requeue(
            claimed.run_id,
            "waiting for successful pipeline",
            claim_token=claimed.claim_token,
        )

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.attempts == 5
    assert claimed.failure_attempts == 0
    assert store.retry_or_fail(
        run.run_id,
        "gitlab_transport",
        "temporary provider failure",
        delay_seconds=0,
        max_attempts=3,
        claim_token=claimed.claim_token,
    )

    pending = store.get_review_run(run.run_id)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.failure_attempts == 1


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
    claimed_a = store.claim_next(project_id="A")
    assert claimed_a is not None
    assert store.finish(project_a_run.run_id, "succeeded", claim_token=claimed_a.claim_token)
    project_a_receipt = store.create_effect_receipt(
        project_a_run.run_id,
        "test_effect",
        "effect-1",
        state="pending",
    )
    project_b_receipt = store.create_effect_receipt(
        project_b_run.run_id,
        "test_effect",
        "effect-2",
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
        "test_effect",
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
    claimed_run = store.claim_next(project_id="42")
    assert claimed_run is not None
    assert store.finish(run.run_id, "succeeded", claim_token=claimed_run.claim_token)
    receipt = store.create_effect_receipt(
        run.run_id,
        "test_effect",
        "effect-1",
        state="pending",
    )

    first_claim = store.claim_pending_effect_receipts(
        "test_effect", max_attempts=3, project_id="42"
    )
    assert len(first_claim) == 1
    assert first_claim[0].attempt == 2

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE effect_receipts SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", receipt.receipt_id),
        )

    second_claim = store.claim_pending_effect_receipts(
        "test_effect", max_attempts=3, project_id="42"
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
            "test_effect", max_attempts=3, project_id="42"
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
    claimed_run = store.claim_next(project_id="42")
    assert claimed_run is not None
    assert store.finish(run.run_id, "succeeded", claim_token=claimed_run.claim_token)
    receipt = store.create_effect_receipt(
        run.run_id,
        "test_effect",
        "effect-1",
        state="pending",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE effect_receipts SET attempt = ?, next_attempt_at = NULL WHERE id = ?",
            (2, receipt.receipt_id),
        )

    assert (
        store.claim_pending_effect_receipts(
            "test_effect", max_attempts=2, project_id="42"
        )
        == ()
    )
    exhausted = store.get_effect_receipt(receipt.receipt_id)
    assert exhausted is not None
    assert exhausted.state == "not_observed"
    assert exhausted.next_attempt_at is None






def test_invalid_sha_is_rejected_before_enqueue(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    with pytest.raises(ValueError, match="invalid SHA"):
        store.enqueue("42", 1, "abc")
    assert store.claim_next() is None



def test_program_auto_fix_intent_is_immutable_and_lease_fenced(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    assert store.create_auto_fix_publication_intent(run.run_id, None) is None
    claimed = store.claim_next(project_id="42")
    assert claimed is not None

    first = store.create_auto_fix_publication_intent(
        claimed.run_id, claimed.claim_token
    )
    second = store.create_auto_fix_publication_intent(
        claimed.run_id, claimed.claim_token
    )

    assert first is not None
    assert second == first
    assert first.generation == 1
    assert len(first.nonce) == 32
    assert first.state == "pending"


def test_program_auto_fix_plan_freezes_canonical_inputs_and_marker(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, SHA)
    claimed = store.claim_next(project_id="42")
    assert claimed is not None
    intent = store.create_auto_fix_publication_intent(
        claimed.run_id, claimed.claim_token
    )
    assert intent is not None
    receipt = store.create_effect_receipt(
        claimed.run_id,
        "autofix_plan",
        f"autofix-intent:{intent.nonce}",
        attempt=1,
        state="planned",
    )
    payload = {
        "version": 1,
        "review_key": {"project_id": "42", "mr_iid": 1, "sha": SHA},
        "intent": {"generation": 1, "nonce": intent.nonce},
        "candidate_sha256": "c" * 64,
        "verification": {
            "candidate_sha256": "c" * 64,
            "command_sha256": "d" * 64,
        },
        "marker_template": (
            f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
            f"intent={intent.nonce};plan_sha256={{plan_sha256}} -->"
        ),
        "branch": f"autofix/MR-1/sha-{SHA}/intent-{intent.nonce}",
    }

    plan = store.freeze_auto_fix_publication_plan(
        claimed.run_id, receipt.receipt_id, payload, claimed.claim_token
    )

    assert plan is not None
    assert plan.plan_json == canonical_json(payload)
    assert plan.marker.endswith(f"plan_sha256={plan.plan_sha256} -->")
    assert store.get_auto_fix_publication_intent(claimed.run_id).state == "plan_frozen"
    assert store.get_auto_fix_publication_plan(claimed.run_id) == plan
    assert (
        store.freeze_auto_fix_publication_plan(
            claimed.run_id, receipt.receipt_id, payload, claimed.claim_token
        )
        == plan
    )
    changed = dict(payload)
    changed["candidate_sha256"] = "e" * 64
    changed["verification"] = {
        "candidate_sha256": "e" * 64,
        "command_sha256": "d" * 64,
    }
    with pytest.raises(ValueError, match="immutable"):
        store.freeze_auto_fix_publication_plan(
            claimed.run_id, receipt.receipt_id, changed, claimed.claim_token
        )




def test_notification_channel_identity_survives_configuration_reordering(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    first = store.enqueue_notifications(
        "review-run:reorder",
        "Review",
        "Body",
        None,
        (),
        (("feishu-reviewers", "feishu"), ("wecom-reviewers", "wecom")),
    )
    second = store.enqueue_notifications(
        "review-run:reorder",
        "Review",
        "Body",
        None,
        (),
        (("wecom-reviewers", "wecom"), ("feishu-reviewers", "feishu")),
    )
    assert [(item.channel_id, item.delivery_id) for item in second] == [
        ("feishu-reviewers", first[0].delivery_id),
        ("wecom-reviewers", first[1].delivery_id),
    ]


def test_notification_outbox_is_idempotent_per_channel(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    first = store.enqueue_notifications(
        "review-run:1:human",
        "Review requested",
        "Please review",
        "https://mr",
        ("alice",),
        (("feishu-reviewers", "feishu"), ("wecom-reviewers", "wecom")),
    )
    duplicate = store.enqueue_notifications(
        "review-run:1:human",
        "Review requested",
        "Please review",
        "https://mr",
        ("alice",),
        (("feishu-reviewers", "feishu"), ("wecom-reviewers", "wecom")),
    )
    assert [item.delivery_id for item in duplicate] == [item.delivery_id for item in first]
    claimed = store.claim_next_notification()
    assert claimed is not None
    assert claimed.channel_id == "feishu-reviewers"
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
        (("feishu-reviewers", "feishu"),),
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
        (("feishu-reviewers", "feishu"),),
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


def test_expired_claims_cannot_finish_before_reclaim(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    expired = "2000-01-01T00:00:00+00:00"

    effect_run = store.enqueue("42", 2, OLD_SHA)
    claimed_effect_run = store.claim_next(project_id="42")
    assert claimed_effect_run is not None and claimed_effect_run.run_id == effect_run.run_id
    assert store.finish(
        effect_run.run_id, "succeeded", claim_token=claimed_effect_run.claim_token
    )
    receipt = store.create_effect_receipt(
        effect_run.run_id,
        "test_effect",
        "effect-2",
        state="pending",
    )
    claimed_receipt = store.claim_pending_effect_receipts(
        "test_effect", project_id="42"
    )[0]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE effect_receipts SET lease_until = ? WHERE id = ?",
            (expired, receipt.receipt_id),
        )
    assert (
        store.update_effect_receipt(
            receipt.receipt_id,
            state="observed",
            claim_token=claimed_receipt.claim_token,
        )
        is False
    )

    delivery = store.enqueue_notifications(
        "review-run:2:human",
        "Review",
        "Please review",
        None,
        (),
        (("recording", "recording"),),
    )[0]
    claimed_delivery = store.claim_notification(delivery.delivery_id)
    assert claimed_delivery is not None and claimed_delivery.claim_token
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE notification_outbox SET lease_until = ? WHERE id = ?",
            (expired, delivery.delivery_id),
        )
    assert (
        store.finish_notification(
            delivery.delivery_id,
            True,
            claim_token=claimed_delivery.claim_token,
        )
        is False
    )

    cleanup = store.register_worktree(effect_run.run_id, tmp_path / "worktree", OLD_SHA)
    assert store.mark_worktree_cleanup_pending(cleanup.cleanup_id, "retry cleanup")
    claimed_cleanup = store.claim_next_worktree_cleanup()
    assert claimed_cleanup is not None and claimed_cleanup.claim_token
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE worktree_cleanup SET lease_until = ? WHERE id = ?",
            (expired, cleanup.cleanup_id),
        )
    assert (
        store.finish_worktree_cleanup(
            cleanup.cleanup_id,
            True,
            claim_token=claimed_cleanup.claim_token,
        )
        is False
    )


def test_main_worktree_cleanup_cannot_overwrite_claimed_cleanup(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    claimed_run = store.claim_next(project_id="42")
    assert claimed_run is not None
    assert store.finish(run.run_id, "succeeded", claim_token=claimed_run.claim_token)
    cleanup = store.register_worktree(run.run_id, tmp_path / "worktree", SHA)
    claimed_cleanup = store.claim_next_worktree_cleanup()
    assert claimed_cleanup is not None and claimed_cleanup.claim_token

    assert store.mark_worktree_cleaned(cleanup.cleanup_id) is False
    assert store.mark_worktree_cleanup_pending(cleanup.cleanup_id, "late cleanup failure") is False
    current = store.get_worktree_record(cleanup.cleanup_id)
    assert current is not None
    assert current.status == "running"
    assert current.claim_token == claimed_cleanup.claim_token
    assert store.finish_worktree_cleanup(
        cleanup.cleanup_id,
        True,
        claim_token=claimed_cleanup.claim_token,
    )


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


def test_notification_and_worktree_errors_are_redacted(tmp_path) -> None:
    token = "runtime-store-token-456"
    store = SQLiteStore(tmp_path / "review.db", redaction_secrets=(token,))

    delivery = store.enqueue_notifications(
        "review-run:1:human",
        "Review",
        "Please review",
        None,
        ("alice",),
        (("recording", "recording"),),
    )[0]
    claimed_delivery = store.claim_notification(delivery.delivery_id)
    assert claimed_delivery is not None
    assert store.finish_notification(
        delivery.delivery_id,
        False,
        f"provider response access_token={token}",
        claim_token=claimed_delivery.claim_token,
        max_attempts=1,
    )
    saved_delivery = store.get_notification_delivery(delivery.delivery_id)
    assert saved_delivery is not None
    assert token not in (saved_delivery.last_error or "")

    run = store.enqueue("42", 1, SHA)
    claimed_run = store.claim_next(project_id="42")
    assert claimed_run is not None
    assert store.finish(run.run_id, "succeeded", claim_token=claimed_run.claim_token)
    cleanup = store.register_worktree(run.run_id, tmp_path / "worktree", SHA)
    assert store.mark_worktree_cleanup_pending(cleanup.cleanup_id, f"cleanup {token}")
    pending = store.get_worktree_record(cleanup.cleanup_id)
    assert pending is not None
    assert token not in (pending.last_error or "")

    claimed_cleanup = store.claim_next_worktree_cleanup(max_attempts=1)
    assert claimed_cleanup is not None
    assert store.finish_worktree_cleanup(
        cleanup.cleanup_id,
        False,
        f"git stderr token={token}",
        claim_token=claimed_cleanup.claim_token,
        max_attempts=1,
    )
    saved_cleanup = store.get_worktree_record(cleanup.cleanup_id)
    assert saved_cleanup is not None
    assert token not in (saved_cleanup.last_error or "")

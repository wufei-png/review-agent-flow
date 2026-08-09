import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from review_agent_flow.autofix_planner import canonical_json
from review_agent_flow.gitlab import canonical_note_marker
from review_agent_flow.operator_config import RetentionConfig
from review_agent_flow.retention import RetentionManager
from review_agent_flow.store import SQLiteStore

SHA = "a" * 40
OLD = "2000-01-01T00:00:00+00:00"


def _terminal_payloads(tmp_path: Path) -> tuple[SQLiteStore, int, int, str]:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next(project_id="42")
    assert claimed is not None
    marker = canonical_note_marker("42", 1, SHA)
    note_receipt = store.create_effect_receipt(run.run_id, "note_expected", marker, state="planned")
    note = store.create_note_publication_request(
        run.run_id,
        note_receipt.receipt_id,
        "sensitive review body",
        claimed.claim_token,
    )
    assert note is not None
    intent = store.create_auto_fix_publication_intent(run.run_id, claimed.claim_token)
    assert intent is not None
    plan_receipt = store.create_effect_receipt(
        run.run_id,
        "autofix_plan",
        f"autofix-intent:{intent.nonce}",
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
        "branch": f"autofix/MR-1/sha-{SHA}/intent-{intent.nonce}",
        "marker_template": (
            f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
            f"intent={intent.nonce};plan_sha256={{plan_sha256}} -->"
        ),
    }
    plan = store.freeze_auto_fix_publication_plan(
        run.run_id, plan_receipt.receipt_id, payload, claimed.claim_token
    )
    assert plan is not None
    assert store.finish(run.run_id, "succeeded", claim_token=claimed.claim_token)
    with store._connect() as connection:
        connection.execute(
            "UPDATE auto_fix_publication_plans SET state = 'published', "
            "write_phase = 'published', updated_at = ? WHERE id = ?",
            (OLD, plan.plan_id),
        )
        connection.execute(
            "UPDATE note_publication_requests SET state = 'published', "
            "external_note_id = 7, payload_retention_at = ?, updated_at = ? WHERE id = ?",
            (OLD, "2026-01-01T00:00:00+00:00", note.request_id),
        )
        connection.execute(
            """
            INSERT INTO auto_fix_relations(
                intent_id, plan_id, run_id, parent_project_id, parent_mr_iid,
                parent_sha, auto_fix_project_id, auto_fix_mr_iid, source_branch,
                target_branch, marker, plan_sha256, published_commit_sha,
                created_at, updated_at
            ) VALUES (?, ?, ?, '42', 1, ?, '42', 9, ?, 'feature', ?, ?, ?, ?, ?)
            """,
            (
                intent.intent_id,
                plan.plan_id,
                run.run_id,
                SHA,
                payload["branch"],
                plan.marker,
                plan.plan_sha256,
                "f" * 40,
                OLD,
                OLD,
            ),
        )
        relation_id = connection.execute(
            "SELECT id FROM auto_fix_relations WHERE plan_id = ?", (plan.plan_id,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO notification_requests(
                relation_id, generation, request_kind, correlation_id,
                title, body, url, recipients_json, payload_sha256, created_at
            ) VALUES (?, 1, 'publication', 'autofix:retention', ?, ?, ?, ?, ?, ?)
            """,
            (
                relation_id,
                "sensitive title",
                "sensitive notification body",
                "https://sensitive.invalid/item",
                '["secret-recipient"]',
                store._notification_payload_sha256(
                    "sensitive title",
                    "sensitive notification body",
                    "https://sensitive.invalid/item",
                    ("secret-recipient",),
                    (("reviewers", "feishu"),),
                ),
                OLD,
            ),
        )
        connection.execute(
            """
            INSERT INTO notification_outbox(
                correlation_id, channel_id, channel, title, body, url,
                recipients_json, status, created_at, delivered_at
            ) VALUES ('autofix:retention', 'reviewers', 'feishu', ?, ?, ?, ?,
                      'delivered', ?, ?)
            """,
            (
                "sensitive title",
                "sensitive notification body",
                "https://sensitive.invalid/item",
                '["secret-recipient"]',
                OLD,
                OLD,
            ),
        )
    return store, run.run_id, plan.plan_id, marker


def test_retention_requires_plan_backup_but_purges_other_expired_payloads(
    tmp_path: Path,
) -> None:
    store, run_id, plan_id, marker = _terminal_payloads(tmp_path)

    first = RetentionManager(store, RetentionConfig(1, None)).sweep(
        now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    assert first.backed_up_plans == 0
    assert first.purge.plan_payloads == 0
    assert store.get_auto_fix_publication_plan(run_id).plan_json is not None
    with store._connect() as connection:
        note = connection.execute(
            "SELECT body, payload_purged_at FROM note_publication_requests"
        ).fetchone()
        delivery = connection.execute(
            "SELECT correlation_id, title, body, url, recipients_json FROM notification_outbox"
        ).fetchone()
        request = connection.execute(
            "SELECT correlation_id, title, body, url, recipients_json FROM notification_requests"
        ).fetchone()
    assert tuple(note) == (marker, note["payload_purged_at"])
    assert note["payload_purged_at"] is not None
    assert tuple(delivery) == ("autofix:retention", "", "", None, "[]")
    assert tuple(request) == ("autofix:retention", "", "", None, "[]")
    assert store.claim_published_note_reconciliation(request_id=1) is None
    assert store.get_auto_fix_publication_plan(run_id).plan_id == plan_id


def test_retention_backs_up_exact_plan_before_purge_and_preserves_audit(
    tmp_path: Path,
) -> None:
    store, run_id, plan_id, _marker = _terminal_payloads(tmp_path)
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None and plan.plan_json is not None
    backup_dir = tmp_path / "backups"

    result = RetentionManager(store, RetentionConfig(1, backup_dir)).sweep(
        now=datetime(2026, 1, 1, tzinfo=UTC)
    )

    backup = backup_dir / f"{plan.plan_sha256}.json"
    assert result.backed_up_plans == 1
    assert result.purge.plan_payloads == 1
    assert backup.read_text(encoding="utf-8") == plan.plan_json
    assert canonical_json(json.loads(backup.read_text(encoding="utf-8"))) == plan.plan_json
    assert backup.stat().st_mode & 0o777 == 0o600
    purged = store.get_auto_fix_publication_plan(run_id)
    assert purged is not None and purged.plan_id == plan_id and purged.plan_json is None
    assert store.list_sensitive_payload_purges()[-1] == result.purge


def test_retention_rejects_conflicting_existing_digest_backup(tmp_path: Path) -> None:
    store, run_id, _plan_id, _marker = _terminal_payloads(tmp_path)
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    conflict = backup_dir / f"{plan.plan_sha256}.json"
    conflict.write_text("conflict", encoding="utf-8")
    conflict.chmod(0o600)

    with pytest.raises(ValueError, match="conflicts"):
        RetentionManager(store, RetentionConfig(1, backup_dir)).sweep(
            now=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert store.get_auto_fix_publication_plan(run_id).plan_json is not None


def test_retention_rejects_insecure_existing_backup_directory(tmp_path: Path) -> None:
    store, run_id, _plan_id, _marker = _terminal_payloads(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup_dir.chmod(0o755)

    with pytest.raises(ValueError, match="mode 0700"):
        RetentionManager(store, RetentionConfig(1, backup_dir)).sweep(
            now=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert store.get_auto_fix_publication_plan(run_id).plan_json is not None


def test_retention_rejects_insecure_existing_backup_file(tmp_path: Path) -> None:
    store, run_id, _plan_id, _marker = _terminal_payloads(tmp_path)
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None and plan.plan_json is not None
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / f"{plan.plan_sha256}.json"
    backup.write_text(plan.plan_json, encoding="utf-8")
    backup.chmod(0o644)

    with pytest.raises(ValueError, match="mode 0600"):
        RetentionManager(store, RetentionConfig(1, backup_dir)).sweep(
            now=datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert store.get_auto_fix_publication_plan(run_id).plan_json is not None


def test_retention_clears_only_terminal_work_ledger_diagnostics(tmp_path: Path) -> None:
    store, _run_id, _plan_id, _marker = _terminal_payloads(tmp_path)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO worktree_cleanup(run_id, path, sha, status, created_at, "
            "last_error) VALUES (1, '/old/failed', ?, 'failed', ?, 'secret failure')",
            (SHA, OLD),
        )
        connection.execute(
            "INSERT INTO worktree_cleanup(run_id, path, sha, status, created_at, "
            "last_error) VALUES (1, '/active', ?, 'active', ?, 'active detail')",
            (SHA, OLD),
        )

    RetentionManager(store, RetentionConfig(1, None)).sweep(now=datetime(2026, 1, 1, tzinfo=UTC))

    with store._connect() as connection:
        rows = connection.execute(
            "SELECT path, last_error FROM worktree_cleanup ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("/old/failed", None),
        ("/active", "active detail"),
    ]

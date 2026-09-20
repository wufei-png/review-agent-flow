import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_retention import _terminal_payloads

from review_agent_flow import cli
from review_agent_flow.backup import create_backup, verify_backup
from review_agent_flow.operator_config import RetentionConfig
from review_agent_flow.retention import RetentionManager
from review_agent_flow.store import SQLiteStore


def _source(tmp_path: Path) -> tuple[Path, Path]:
    store, _run_id, _plan_id, _marker = _terminal_payloads(tmp_path)
    plan_directory = tmp_path / "plans"
    RetentionManager(store, RetentionConfig(1, plan_directory)).sweep(
        now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    return Path(store.path), plan_directory


def test_backup_captures_consistent_wal_snapshot_and_receipts(tmp_path: Path) -> None:
    database, plans = _source(tmp_path)
    output_parent = tmp_path / "bundles"
    output_parent.mkdir(mode=0o700)
    output = output_parent / "one"
    with sqlite3.connect(database) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO review_runs(project_id, mr_iid, sha, created_at) "
                       "VALUES ('42', 99, ?, '2026-01-01')", ("b" * 40,))
        manifest = create_backup(database, plans, output)
        assert manifest["schema_version"] == 17
        assert len(manifest["plans"]) == 1
        assert (output / "manifest.json").stat().st_mode & 0o777 == 0o600
        assert (output / "store.sqlite3").stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(output / "store.sqlite3") as snapshot:
            assert snapshot.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 1
            assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(database) as live:
        assert live.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 2
    assert json.loads((output / "manifest.json").read_text()) == manifest
    with pytest.raises(FileExistsError):
        create_backup(database, plans, output)


@pytest.mark.parametrize("damage", ["missing", "tampered", "symlink", "file_mode", "dir_mode"])
def test_backup_rejects_unsafe_plan_without_publishing(tmp_path: Path, damage: str) -> None:
    database, plans = _source(tmp_path)
    plan = next(plans.glob("*.json"))
    if damage == "missing":
        plan.unlink()
    elif damage == "tampered":
        plan.write_text("{}")
    elif damage == "symlink":
        target = tmp_path / "target.json"
        plan.rename(target)
        plan.symlink_to(target)
    elif damage == "file_mode":
        plan.chmod(0o644)
    else:
        plans.chmod(0o755)
    parent = tmp_path / "bundles"
    parent.mkdir(mode=0o700)
    output = parent / "one"
    with pytest.raises((ValueError, OSError)):
        create_backup(database, plans, output)
    assert not output.exists()
    assert list(parent.iterdir()) == []


def test_backup_rejects_receipt_outside_configured_root(tmp_path: Path) -> None:
    database, _plans = _source(tmp_path)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    parent = tmp_path / "bundles"
    parent.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="outside"):
        create_backup(database, other, parent / "one")
    assert list(parent.iterdir()) == []


def test_backup_rejects_symlinked_source_database(tmp_path: Path) -> None:
    database, plans = _source(tmp_path)
    link = tmp_path / "linked.db"
    link.symlink_to(database)
    parent = tmp_path / "bundles"
    parent.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="not a regular file"):
        create_backup(link, plans, parent / "one")
    assert list(parent.iterdir()) == []


def _bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    database, plans = _source(tmp_path)
    parent = tmp_path / "bundles"
    parent.mkdir(mode=0o700)
    bundle = parent / "one"
    assert cli.main([
        "backup-store", "--db", str(database), "--plan-backup-dir", str(plans),
        "--output", str(bundle),
    ]) == 0
    return database, plans, bundle


def test_verify_backup_cli_and_empty_directory_recovery(tmp_path: Path, capsys) -> None:
    database, plans = _source(tmp_path)
    store = SQLiteStore(database)
    run = store.enqueue("42", 2, "b" * 40)
    claim = store.claim_next(project_id="42")
    assert claim is not None
    snapshot = store.freeze_policy_snapshot(
        run.run_id, claim.claim_token, target_branch="main", target_sha="c" * 40,
        policy_version=4, policy_json='{"version":4}',
    )
    assert snapshot is not None
    assert store.finish(run.run_id, "succeeded", claim_token=claim.claim_token)
    audit = store.record_operator_recovery_failure(
        recovery_id="backup-rehearsal", action="relation_recovery", project_id="42",
        parent_mr_iid=1, parent_sha="a" * 40, intent_nonce="nonce",
        plan_sha256="d" * 64, child_project_id="42", child_mr_iid=9,
        reason="test", principal="operator", hostname="localhost", tool_version="test",
        diagnostic="rehearsal",
    )
    parent = tmp_path / "bundles"
    parent.mkdir(mode=0o700)
    bundle = parent / "one"
    create_backup(database, plans, bundle)
    assert cli.main(["verify-backup", "--bundle", str(bundle)]) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["status"] == "verified"
    assert report["plan_count"] == 1

    # The fixture's original paths are vacated before restoring into them.
    database.rename(tmp_path / "source-db-away")
    plans.rename(tmp_path / "source-plans-away")
    plans.mkdir(mode=0o700)
    shutil.copyfile(bundle / "store.sqlite3", database)
    database.chmod(0o600)
    manifest = json.loads((bundle / "manifest.json").read_text())
    for entry in manifest["plans"]:
        restored = Path(entry["source_path"])
        shutil.copyfile(bundle / entry["path"], restored)
        restored.chmod(0o600)
    restored_store = SQLiteStore(database, read_only=True)
    assert restored_store.get_policy_snapshot(run.run_id) == snapshot
    assert restored_store.get_project_bound_review_run_status("42", run.run_id)
    assert restored_store.get_project_ops_status("42")["runs"]["pending"] == 0
    assert restored_store.list_operator_recoveries() == (audit,)
    with restored_store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM review_runs WHERE status = 'succeeded'"
        ).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM auto_fix_relations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
        receipt = connection.execute(
            "SELECT backup_path FROM publication_plan_backups"
        ).fetchone()[0]
    assert receipt == manifest["plans"][0]["source_path"]
    assert Path(receipt).read_bytes() == (bundle / manifest["plans"][0]["path"]).read_bytes()
    assert verify_backup(bundle)["status"] == "verified"


@pytest.mark.parametrize(
    "damage", ["missing", "payload", "digest", "file_mode", "directory_mode", "symlink", "mapping"]
)
def test_verify_backup_rejects_damaged_bundle(tmp_path: Path, damage: str) -> None:
    _database, _plans, bundle = _bundle(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    plan = bundle / manifest["plans"][0]["path"]
    if damage == "missing":
        plan.unlink()
    elif damage == "payload":
        plan.write_text("{}")
    elif damage == "digest":
        manifest["database"]["sha256"] = "0" * 64
    elif damage == "file_mode":
        plan.chmod(0o644)
    elif damage == "directory_mode":
        (bundle / "plans").chmod(0o755)
    elif damage == "symlink":
        target = tmp_path / "target.json"
        plan.rename(target)
        plan.symlink_to(target)
    else:
        manifest["plans"][0]["source_path"] = "/different/path.json"
    if damage in {"digest", "mapping"}:
        from review_agent_flow.autofix_planner import canonical_json

        manifest_path.write_text(canonical_json(manifest))
        manifest_path.chmod(0o600)
    with pytest.raises((OSError, ValueError, sqlite3.Error)):
        verify_backup(bundle)

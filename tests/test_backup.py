import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_retention import _terminal_payloads

from review_agent_flow.backup import create_backup
from review_agent_flow.operator_config import RetentionConfig
from review_agent_flow.retention import RetentionManager


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

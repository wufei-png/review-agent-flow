"""Private, consistent SQLite and canonical Publication Plan recovery bundles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .autofix_planner import canonical_json
from .retention import RetentionManager
from .store import SCHEMA_VERSION

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "store.sqlite3"


def _private_directory(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ValueError(f"unsafe backup directory: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError(f"backup directory must have mode 0700: {path}")


def _private_file(path: Path) -> bytes:
    return RetentionManager._read_private_file(path)


def _write_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _snapshot(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ValueError("source SQLite database is missing")
    uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as live, sqlite3.connect(
        destination
    ) as snapshot:
        live.backup(snapshot)
    os.chmod(destination, 0o600)
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())


def _receipts(database: Path) -> list[sqlite3.Row]:
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite snapshot failed integrity_check")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported SQLite schema version {version}")
        return connection.execute(
            "SELECT b.plan_id, b.plan_sha256, b.backup_path, p.plan_json "
            "FROM publication_plan_backups b "
            "JOIN auto_fix_publication_plans p ON p.id = b.plan_id "
            "AND p.plan_sha256 = b.plan_sha256 ORDER BY b.plan_id"
        ).fetchall()


def _plan_payload(payload: bytes, digest: str) -> None:
    if _digest(payload) != digest:
        raise ValueError("Publication Plan digest mismatch")
    try:
        parsed = json.loads(payload)
        canonical = canonical_json(parsed).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Publication Plan is not canonical JSON") from exc
    if payload != canonical:
        raise ValueError("Publication Plan is not canonical JSON")


def create_backup(source: Path, plan_directory: Path, destination: Path) -> dict:
    """Create one verified bundle without changing the active Store or Plan files."""
    source = source.absolute()
    plan_directory = plan_directory.absolute()
    destination = destination.absolute()
    _private_directory(plan_directory)
    _private_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        _private_directory(temporary)
        database = temporary / DATABASE_NAME
        _snapshot(source, database)
        plan_entries = []
        rows = _receipts(database)
        plans = temporary / "plans"
        plans.mkdir(mode=0o700)
        for row in rows:
            original = Path(row["backup_path"])
            digest = row["plan_sha256"]
            if (
                not original.is_absolute()
                or original.parent != plan_directory
                or original.name != f"{digest}.json"
            ):
                raise ValueError("Publication Plan receipt path is outside the configured directory")
            payload = _private_file(original)
            _plan_payload(payload, digest)
            retained = row["plan_json"]
            if retained is not None and retained.encode("utf-8") != payload:
                raise ValueError("Publication Plan receipt conflicts with retained Store payload")
            relative = f"plans/{digest}.json"
            _write_file(temporary / relative, payload)
            plan_entries.append({
                "plan_id": row["plan_id"],
                "plan_sha256": digest,
                "source_path": str(original),
                "path": relative,
                "sha256": _digest(payload),
                "bytes": len(payload),
            })
        database_payload = _private_file(database)
        manifest = {
            "format_version": 1,
            "application_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "snapshot_at": datetime.now(UTC).isoformat(),
            "database": {
                "path": DATABASE_NAME,
                "sha256": _digest(database_payload),
                "bytes": len(database_payload),
            },
            "plans": plan_entries,
        }
        _write_file(temporary / MANIFEST_NAME, canonical_json(manifest).encode("utf-8"))
        RetentionManager._fsync_directory(plans)
        RetentionManager._fsync_directory(temporary)
        # Same-parent rename makes the completed bundle visible in one operation.
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        os.rename(temporary, destination)
        RetentionManager._fsync_directory(destination.parent)
        return manifest
    except BaseException:
        shutil.rmtree(temporary)
        raise

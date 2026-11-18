"""Private, consistent SQLite and canonical Publication Plan recovery bundles."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
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


def _file_fingerprint(path: Path) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError(f"unsafe backup file: {path}")
        digest = hashlib.sha256()
        length = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            length += len(chunk)
        return digest.hexdigest(), length
    finally:
        os.close(descriptor)


def _snapshot(source: Path, destination: Path) -> None:
    try:
        metadata = source.lstat()
    except FileNotFoundError as exc:
        raise ValueError("source SQLite database is missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("source SQLite database is not a regular file")
    uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    with (
        sqlite3.connect(uri, uri=True, timeout=30) as live,
        sqlite3.connect(destination) as snapshot,
    ):
        live.backup(snapshot)
        snapshot.execute("PRAGMA journal_mode = DELETE")
    os.chmod(destination, 0o600)
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())


def _publish_directory(source: Path, destination: Path) -> None:
    """Atomically rename a directory only when the destination is absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)
    else:
        raise OSError("atomic no-replace directory rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _receipts(database: Path) -> list[sqlite3.Row]:
    uri = f"file:{quote(str(database), safe='/')}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite snapshot failed integrity_check")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported SQLite schema version {version}")
        rows = connection.execute(
            "SELECT b.plan_id, b.plan_sha256, b.backup_path, p.id AS stored_plan_id, "
            "p.plan_json "
            "FROM publication_plan_backups b "
            "LEFT JOIN auto_fix_publication_plans p ON p.id = b.plan_id "
            "AND p.plan_sha256 = b.plan_sha256 ORDER BY b.plan_id"
        ).fetchall()
        if any(row["stored_plan_id"] is None for row in rows):
            raise ValueError("Publication Plan receipt has no matching Store Plan")
        return rows


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
    _private_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    published = False
    temporary_identity = temporary.stat().st_dev, temporary.stat().st_ino
    try:
        _private_directory(temporary)
        database = temporary / DATABASE_NAME
        snapshot_at = datetime.now(UTC).isoformat()
        _snapshot(source, database)
        plan_entries = []
        rows = _receipts(database)
        if rows:
            _private_directory(plan_directory)
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
                raise ValueError(
                    "Publication Plan receipt path is outside the configured directory"
                )
            payload = _private_file(original)
            _plan_payload(payload, digest)
            retained = row["plan_json"]
            if retained is not None and retained.encode("utf-8") != payload:
                raise ValueError("Publication Plan receipt conflicts with retained Store payload")
            relative = f"plans/{digest}.json"
            _write_file(temporary / relative, payload)
            plan_entries.append(
                {
                    "plan_id": row["plan_id"],
                    "plan_sha256": digest,
                    "source_path": str(original),
                    "path": relative,
                    "sha256": _digest(payload),
                    "bytes": len(payload),
                }
            )
        database_sha256, database_bytes = _file_fingerprint(database)
        manifest = {
            "format_version": 1,
            "application_version": __version__,
            "schema_version": SCHEMA_VERSION,
            "snapshot_at": snapshot_at,
            "database": {
                "path": DATABASE_NAME,
                "sha256": database_sha256,
                "bytes": database_bytes,
            },
            "plans": plan_entries,
        }
        _write_file(temporary / MANIFEST_NAME, canonical_json(manifest).encode("utf-8"))
        RetentionManager._fsync_directory(plans)
        RetentionManager._fsync_directory(temporary)
        # Same-parent no-replace rename makes the completed bundle visible atomically.
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        _publish_directory(temporary, destination)
        published = True
        RetentionManager._fsync_directory(destination.parent)
        return manifest
    except BaseException:
        try:
            if published:
                destination_metadata = destination.lstat()
                if (destination_metadata.st_dev, destination_metadata.st_ino) == temporary_identity:
                    shutil.rmtree(destination)
                    RetentionManager._fsync_directory(destination.parent)
            else:
                shutil.rmtree(temporary)
        except OSError:
            # Keep the original failure, especially a post-publication durability error.
            pass
        raise


def verify_backup(bundle: Path) -> dict:
    """Check a closed bundle and its receipt mapping without opening providers."""
    bundle = bundle.absolute()
    _private_directory(bundle)
    _private_directory(bundle / "plans")
    raw = _private_file(bundle / MANIFEST_NAME)
    try:
        manifest = json.loads(raw)
        if raw != canonical_json(manifest).encode("utf-8"):
            raise ValueError("backup manifest is not canonical JSON")
        if set(manifest) != {
            "format_version",
            "application_version",
            "schema_version",
            "snapshot_at",
            "database",
            "plans",
        }:
            raise ValueError("backup manifest fields are invalid")
        if type(manifest["format_version"]) is not int or manifest["format_version"] != 1:
            raise ValueError("unsupported backup format")
        if (
            type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != SCHEMA_VERSION
        ):
            raise ValueError("unsupported backup schema")
        if (
            not isinstance(manifest["application_version"], str)
            or not manifest["application_version"]
        ):
            raise ValueError("backup application version is invalid")
        timestamp = datetime.fromisoformat(manifest["snapshot_at"])
        if timestamp.tzinfo is None:
            raise ValueError("backup snapshot time has no timezone")
        database_entry = manifest["database"]
        if set(database_entry) != {"path", "sha256", "bytes"}:
            raise ValueError("backup database fields are invalid")
        if database_entry["path"] != DATABASE_NAME:
            raise ValueError("backup database path is invalid")
        if not isinstance(manifest["plans"], list):
            raise TypeError("backup Plan list is invalid")
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid backup manifest: {exc}") from exc

    database = bundle / DATABASE_NAME
    if _file_fingerprint(database) != (database_entry["sha256"], database_entry["bytes"]):
        raise ValueError("backup SQLite file digest or length mismatch")
    rows = _receipts(database)
    if len(rows) != len(manifest["plans"]):
        raise ValueError("backup receipt count mismatch")
    expected_files = {MANIFEST_NAME, DATABASE_NAME, "plans"}
    expected_plans: set[str] = set()
    for row, entry in zip(rows, manifest["plans"], strict=True):
        if not isinstance(entry, dict) or set(entry) != {
            "plan_id",
            "plan_sha256",
            "source_path",
            "path",
            "sha256",
            "bytes",
        }:
            raise ValueError("backup Plan manifest entry is invalid")
        digest = row["plan_sha256"]
        name = f"{digest}.json"
        original = Path(row["backup_path"])
        if (
            type(entry["plan_id"]) is not int
            or entry["plan_id"] != row["plan_id"]
            or entry["plan_sha256"] != digest
            or entry["sha256"] != digest
            or entry["source_path"] != row["backup_path"]
            or not original.is_absolute()
            or original.name != name
            or entry["path"] != f"plans/{name}"
        ):
            raise ValueError("backup Plan receipt mapping mismatch")
        path = bundle / "plans" / name
        payload = _private_file(path)
        _plan_payload(payload, digest)
        if len(payload) != entry["bytes"]:
            raise ValueError("backup Plan length mismatch")
        retained = row["plan_json"]
        if retained is not None and retained.encode("utf-8") != payload:
            raise ValueError("backup Plan conflicts with retained Store payload")
        expected_plans.add(name)
    if {path.name for path in bundle.iterdir()} != expected_files:
        raise ValueError("backup bundle contains unexpected entries")
    if {path.name for path in (bundle / "plans").iterdir()} != expected_plans:
        raise ValueError("backup Plan directory contains unexpected entries")
    return {
        "status": "verified",
        "application_version": manifest["application_version"],
        "schema_version": manifest["schema_version"],
        "snapshot_at": manifest["snapshot_at"],
        "plan_count": len(rows),
    }

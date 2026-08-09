"""Operator-owned backup and retention for sensitive persisted payloads."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .operator_config import RetentionConfig
from .store import SensitivePayloadPurgeResult, SQLiteStore


@dataclass(frozen=True)
class RetentionSweepResult:
    backed_up_plans: int
    purge: SensitivePayloadPurgeResult


class RetentionManager:
    """Back up recoverable Plans before purging expired sensitive content."""

    def __init__(self, store: SQLiteStore, config: RetentionConfig) -> None:
        self.store = store
        self.config = config

    @staticmethod
    def _validate_private_directory(directory: Path) -> None:
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Publication Plan backup directory is not a directory")
        if metadata.st_uid != os.geteuid():
            raise ValueError("Publication Plan backup directory has an unsafe owner")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("Publication Plan backup directory must have mode 0700")

    @staticmethod
    def _read_private_file(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError("Publication Plan backup path is not a safe regular file") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Publication Plan backup path is not a regular file")
            if metadata.st_uid != os.geteuid():
                raise ValueError("Publication Plan backup file has an unsafe owner")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ValueError("Publication Plan backup file must have mode 0600")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            os.fsync(descriptor)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_immutable_backup(directory: Path, digest: str, payload: str) -> Path:
        raw = payload.encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Publication Plan backup digest mismatch")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        RetentionManager._validate_private_directory(directory)
        destination = directory / f"{digest}.json"
        if destination.exists():
            if RetentionManager._read_private_file(destination) != raw:
                raise ValueError("existing Publication Plan backup conflicts with its digest")
            RetentionManager._fsync_directory(directory)
            return destination
        temporary = directory / f".{digest}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if RetentionManager._read_private_file(destination) != raw:
                    raise ValueError("concurrent Publication Plan backup conflicts with its digest")
        finally:
            temporary.unlink(missing_ok=True)
        if RetentionManager._read_private_file(destination) != raw:
            raise ValueError("Publication Plan backup conflicts with its digest")
        RetentionManager._fsync_directory(directory)
        return destination

    def sweep(self, *, now: datetime | None = None) -> RetentionSweepResult:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("retention sweep time must be timezone-aware")
        cutoff = current - timedelta(days=self.config.sensitive_payload_days)
        backed_up = 0
        backup_dir = self.config.publication_plan_backup_dir
        if backup_dir is not None:
            for candidate in self.store.list_publication_plan_backup_candidates(cutoff):
                path = self._write_immutable_backup(
                    backup_dir,
                    candidate.plan_sha256,
                    candidate.plan_json,
                )
                self.store.record_publication_plan_backup(candidate, str(path))
                backed_up += 1
        purge = self.store.purge_sensitive_payloads(cutoff)
        return RetentionSweepResult(backed_up, purge)

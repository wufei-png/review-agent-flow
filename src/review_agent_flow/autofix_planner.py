"""Capture, verify and canonically describe a Program-owned Auto-fix candidate."""

from __future__ import annotations

import hashlib
import os
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .gitlab import is_valid_sha
from .publication_plan import canonical_json
from .runner import LeaseLostError

MAX_AUTOFIX_ACTIONS = 100
MAX_AUTOFIX_FILE_BYTES = 1024 * 1024
MAX_AUTOFIX_TOTAL_BYTES = 5 * 1024 * 1024
GIT_COMMAND_TIMEOUT_SECONDS = 30
MAX_GIT_INSPECTION_OUTPUT_BYTES = MAX_AUTOFIX_TOTAL_BYTES + (1024 * 1024)


class DiffEnvelopeError(ValueError):
    """Raised when a workspace candidate is outside the accepted text envelope."""


@dataclass(frozen=True)
class DiffAction:
    action: str
    file_path: str
    content: str | None = None
    execute_filemode: bool | None = None

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"action": self.action, "file_path": self.file_path}
        if self.content is not None:
            value["content"] = self.content
        if self.execute_filemode is not None:
            value["execute_filemode"] = self.execute_filemode
        return value


@dataclass(frozen=True)
class DiffEnvelope:
    base_sha: str
    actions: tuple[DiffAction, ...]
    candidate_sha256: str
    total_content_bytes: int

    def canonical_value(self) -> dict[str, object]:
        return {
            "version": 1,
            "base_sha": self.base_sha,
            "actions": [action.as_dict() for action in self.actions],
        }


@dataclass(frozen=True)
class VerificationEvidence:
    command_sha256: str
    candidate_sha256: str
    returncode: int
    duration_ms: int

    def as_dict(self) -> dict[str, object]:
        return {
            "command_sha256": self.command_sha256,
            "candidate_sha256": self.candidate_sha256,
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
        }


def _git(worktree: Path, *arguments: str) -> bytes:
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            subprocess.run(
                ["git", "-C", str(worktree), *arguments],
                check=True,
                stdout=stdout,
                stderr=stderr,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            if stdout.tell() > MAX_GIT_INSPECTION_OUTPUT_BYTES:
                raise DiffEnvelopeError("git inspection output exceeds the bounded envelope")
            stdout.seek(0)
            return stdout.read()
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiffEnvelopeError("unable to inspect the Auto-fix workspace with git") from exc


def _path_from_git(value: bytes) -> str:
    try:
        path = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiffEnvelopeError("Auto-fix paths must be UTF-8") from exc
    parsed = PurePosixPath(path)
    if (
        not path
        or str(parsed) != path
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise DiffEnvelopeError("Auto-fix path is not a canonical repository-relative path")
    if "\\" in path:
        raise DiffEnvelopeError("Auto-fix paths must use repository separators")
    return path


def _changed_paths(worktree: Path) -> tuple[str, ...]:
    raw = _git(
        worktree,
        "-c",
        "status.renames=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    entries = raw.split(b"\0")
    paths: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise DiffEnvelopeError("git returned an unsupported workspace status record")
        status_code = entry[:2]
        if b"U" in status_code or status_code in {b"AA", b"DD"}:
            raise DiffEnvelopeError("Auto-fix workspace contains an unmerged path")
        paths.add(_path_from_git(entry[3:]))
    return tuple(sorted(paths))


def _baseline_entry(worktree: Path, path: str) -> tuple[str, str, str] | None:
    raw = _git(worktree, "ls-tree", "-z", "HEAD", "--", path)
    if not raw:
        return None
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise DiffEnvelopeError("git returned an ambiguous baseline path")
    metadata, returned_path = records[0].split(b"\t", 1)
    if _path_from_git(returned_path) != path:
        raise DiffEnvelopeError("git baseline path does not match the candidate path")
    fields = metadata.decode("ascii").split(" ")
    if len(fields) != 3:
        raise DiffEnvelopeError("git returned invalid baseline metadata")
    return fields[0], fields[1], fields[2]


def _read_baseline_blob(worktree: Path, object_id: str) -> bytes:
    try:
        size = int(_git(worktree, "cat-file", "-s", object_id).decode("ascii").strip())
    except (UnicodeError, ValueError) as exc:
        raise DiffEnvelopeError("git returned an invalid baseline blob size") from exc
    if size > MAX_AUTOFIX_FILE_BYTES:
        raise DiffEnvelopeError("Auto-fix baseline file exceeds the per-file bound")
    value = _git(worktree, "cat-file", "blob", object_id)
    if len(value) != size:
        raise DiffEnvelopeError("git baseline blob size changed during capture")
    return value


def _read_regular_file(path: Path) -> tuple[bytes, bool]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DiffEnvelopeError("Auto-fix candidate must contain readable regular files") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DiffEnvelopeError("Auto-fix candidate rejects symlinks and special files")
        chunks: list[bytes] = []
        remaining = MAX_AUTOFIX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_AUTOFIX_FILE_BYTES:
            raise DiffEnvelopeError("Auto-fix candidate exceeds the per-file bound")
        return content, bool(metadata.st_mode & 0o111)
    finally:
        os.close(descriptor)


def _text(content: bytes, path: str) -> str:
    if b"\0" in content:
        raise DiffEnvelopeError(
            f"Auto-fix candidate rejects binary content at path: {path}"
        )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiffEnvelopeError(
            f"Auto-fix candidate content must be UTF-8 at path: {path}"
        ) from exc


def capture_diff_envelope(worktree: Path, expected_sha: str) -> DiffEnvelope:
    """Capture the filesystem candidate relative to the exact detached HEAD."""

    if not is_valid_sha(expected_sha):
        raise DiffEnvelopeError("Auto-fix base SHA is invalid")
    head = _git(worktree, "rev-parse", "HEAD").decode("ascii").strip()
    if head.lower() != expected_sha.lower():
        raise DiffEnvelopeError("Auto-fix Agent changed the workspace HEAD")

    actions: list[DiffAction] = []
    total_bytes = 0
    for path in _changed_paths(worktree):
        baseline = _baseline_entry(worktree, path)
        candidate_path = worktree.joinpath(*PurePosixPath(path).parts)
        try:
            metadata = candidate_path.lstat()
        except FileNotFoundError:
            metadata = None

        if metadata is None:
            if baseline is None:
                continue
            baseline_mode, baseline_type, object_id = baseline
            if baseline_type != "blob" or baseline_mode not in {"100644", "100755"}:
                raise DiffEnvelopeError("Auto-fix cannot delete a symlink or gitlink")
            _text(_read_baseline_blob(worktree, object_id), path)
            actions.append(DiffAction("delete", path))
            continue

        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise DiffEnvelopeError("Auto-fix candidate rejects symlinks, gitlinks and special files")
        current, executable = _read_regular_file(candidate_path)
        current_text = _text(current, path)
        if baseline is None:
            total_bytes += len(current)
            if total_bytes > MAX_AUTOFIX_TOTAL_BYTES:
                raise DiffEnvelopeError("Auto-fix candidate exceeds the aggregate content bound")
            actions.append(DiffAction("create", path, current_text, executable))
            continue
        baseline_mode, baseline_type, object_id = baseline
        if baseline_type != "blob" or baseline_mode not in {"100644", "100755"}:
            raise DiffEnvelopeError("Auto-fix cannot modify a symlink or gitlink")
        original = _read_baseline_blob(worktree, object_id)
        original_executable = baseline_mode == "100755"
        if current == original and executable == original_executable:
            continue
        if current != original:
            total_bytes += len(current)
            if total_bytes > MAX_AUTOFIX_TOTAL_BYTES:
                raise DiffEnvelopeError("Auto-fix candidate exceeds the aggregate content bound")
            actions.append(DiffAction("update", path, current_text, executable))
        else:
            actions.append(DiffAction("chmod", path, execute_filemode=executable))

    if len(actions) > MAX_AUTOFIX_ACTIONS:
        raise DiffEnvelopeError("Auto-fix candidate exceeds the action count bound")
    canonical_value = {
        "version": 1,
        "base_sha": expected_sha.lower(),
        "actions": [action.as_dict() for action in actions],
    }
    digest = hashlib.sha256(canonical_json(canonical_value).encode("utf-8")).hexdigest()
    return DiffEnvelope(expected_sha.lower(), tuple(actions), digest, total_bytes)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.poll() is None:
                process.kill()
    elif process.poll() is None:
        process.kill()
    process.wait()


def run_verifier(
    command: Sequence[str],
    worktree: Path,
    environment: Mapping[str, str],
    candidate_sha256: str,
    *,
    timeout_seconds: int,
    heartbeat: Callable[[], bool] | None = None,
    heartbeat_interval_seconds: float = 30.0,
) -> VerificationEvidence:
    """Run one Operator-owned verifier without accepting verifier output as authority."""

    if not command:
        raise DiffEnvelopeError("Auto-fix requires an Operator verifier")
    if timeout_seconds <= 0 or heartbeat_interval_seconds <= 0:
        raise ValueError("verifier timeouts must be positive")
    command_value = tuple(command)
    command_digest = hashlib.sha256(
        canonical_json(list(command_value)).encode("utf-8")
    ).hexdigest()
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(command_value),
            cwd=worktree,
            env=dict(environment),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise DiffEnvelopeError("Operator verifier could not be started") from exc
    deadline = started + timeout_seconds
    interval = min(heartbeat_interval_seconds, float(timeout_seconds))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate(process)
            raise DiffEnvelopeError("Operator verifier timed out")
        try:
            returncode = process.wait(timeout=min(interval, remaining))
            break
        except subprocess.TimeoutExpired:
            if heartbeat is not None:
                try:
                    heartbeat_ok = heartbeat()
                except Exception as exc:
                    _terminate(process)
                    raise LeaseLostError(
                        "Review Run lease heartbeat failed during Auto-fix verification"
                    ) from exc
                if not heartbeat_ok:
                    _terminate(process)
                    raise LeaseLostError(
                        "Review Run lease was lost during Auto-fix verification"
                    )
    if returncode != 0:
        raise DiffEnvelopeError(f"Operator verifier exited with code {returncode}")
    return VerificationEvidence(
        command_digest,
        candidate_sha256,
        returncode,
        int((time.monotonic() - started) * 1000),
    )


def build_publication_plan_payload(
    *,
    project_id: str,
    mr_iid: int,
    sha: str,
    generation: int,
    nonce: str,
    proposal_summary: str,
    envelope: DiffEnvelope,
    verification: VerificationEvidence,
    target_branch: str,
    parent_title: str,
    initial_readiness: str,
    ci_trigger: str,
) -> dict[str, object]:
    branch = f"autofix/MR-{mr_iid}/sha-{sha}/intent-{nonce}"
    marker_template = (
        f"<!-- reviewflow:v1;project={project_id};mr={mr_iid};sha={sha};"
        f"effect=autofix;intent={nonce};plan_sha256={{plan_sha256}} -->"
    )
    commit_message = f"ReviewFlow Auto-fix for !{mr_iid} at {sha}"
    if ci_trigger == "manual":
        commit_message += "\n\n[skip ci]"
    title = f"[ReviewFlow Auto-Fix] {parent_title}"
    if initial_readiness == "draft":
        title = f"Draft: {title}"
    return {
        "version": 1,
        "review_key": {"project_id": project_id, "mr_iid": mr_iid, "sha": sha},
        "intent": {"generation": generation, "nonce": nonce},
        "proposal_summary": proposal_summary,
        "candidate_sha256": envelope.candidate_sha256,
        "actions": [action.as_dict() for action in envelope.actions],
        "verification": {
            "command_sha256": verification.command_sha256,
            "candidate_sha256": verification.candidate_sha256,
        },
        "branch": branch,
        "marker_template": marker_template,
        "commit_message": commit_message,
        "title": title,
        "target_branch": target_branch,
        "initial_readiness": initial_readiness,
        "ci_trigger": ci_trigger,
    }

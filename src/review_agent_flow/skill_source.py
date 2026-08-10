"""Bounded loading of trusted Skill bytes from Operator or exact Git sources."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path, PurePosixPath

from .gitlab import normalize_sha

MAX_AGENT_SKILL_BYTES = 64 * 1024
GIT_SKILL_TIMEOUT_SECONDS = 30


class SkillSourceError(ValueError):
    """Raised when a selected Skill cannot become a bounded trusted input."""


def validate_skill_content(content: object) -> str:
    """Return a strict bounded UTF-8 Skill suitable for one CLI argument."""

    if not isinstance(content, str) or not content or "\x00" in content:
        raise SkillSourceError("Agent Skill must be non-empty UTF-8 text without NUL bytes")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_AGENT_SKILL_BYTES:
        raise SkillSourceError(f"Agent Skill exceeds {MAX_AGENT_SKILL_BYTES} bytes")
    return content


def load_operator_skill(path: Path) -> str:
    """Read one Operator-owned regular file without following a symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SkillSourceError("Operator Skill must be a readable regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SkillSourceError("Operator Skill must be a regular file")
        chunks: list[bytes] = []
        remaining = MAX_AGENT_SKILL_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > MAX_AGENT_SKILL_BYTES:
        raise SkillSourceError(f"Agent Skill exceeds {MAX_AGENT_SKILL_BYTES} bytes")
    try:
        return validate_skill_content(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SkillSourceError("Agent Skill must be UTF-8 text") from exc


def _git(repository: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            timeout=GIT_SKILL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillSourceError("unable to read the Project Skill Git object") from exc
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SkillSourceError(
            f"unable to read the Project Skill Git object: {detail or 'git failed'}"
        )
    return completed.stdout


def load_project_skill(repository: Path, revision: str, path: str) -> str | None:
    """Read one regular blob at an exact commit without touching the worktree."""

    revision = normalize_sha(revision)
    parsed = PurePosixPath(path)
    if (
        not path
        or str(parsed) != path
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise SkillSourceError("Project Skill path must be canonical and repository-relative")
    listing = _git(repository, "ls-tree", "-z", "--full-tree", revision, "--", path)
    if not listing:
        return None
    records = [record for record in listing.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise SkillSourceError("Project Skill path did not resolve to exactly one Git object")
    metadata, returned_path = records[0].split(b"\t", 1)
    try:
        returned = returned_path.decode("utf-8")
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError) as exc:
        raise SkillSourceError("Project Skill has invalid Git metadata") from exc
    if returned != path:
        raise SkillSourceError("Project Skill Git path does not match the selected path")
    if mode not in {"100644", "100755"} or object_type != "blob":
        raise SkillSourceError("Project Skill must be a regular Git file, not a symlink")
    try:
        size = int(_git(repository, "cat-file", "-s", object_id).decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise SkillSourceError("Project Skill Git blob has an invalid size") from exc
    if size > MAX_AGENT_SKILL_BYTES:
        raise SkillSourceError(f"Agent Skill exceeds {MAX_AGENT_SKILL_BYTES} bytes")
    raw = _git(repository, "cat-file", "blob", object_id)
    if len(raw) != size:
        raise SkillSourceError("Project Skill Git blob size changed while reading")
    try:
        return validate_skill_content(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SkillSourceError("Agent Skill must be UTF-8 text") from exc

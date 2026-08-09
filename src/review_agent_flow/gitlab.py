"""Narrow GitLab interfaces used by the workflow core."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_NOTE_MARKER_RE = re.compile(
    r"^<!-- reviewflow:v1;project=[A-Za-z0-9_.:-]+;mr=[1-9][0-9]*;"
    r"sha=(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64});effect=note -->$"
)


class GitLabIdentityConflict(ValueError):
    """Raised when a GitLab resource conflicts with the configured Bot identity."""


def canonical_note_marker(project_id: str, mr_iid: int, sha: str) -> str:
    """Return the Program-owned Note identity for one exact ReviewKey."""

    if type(project_id) is not str or _PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ValueError("project_id must use marker-safe characters")
    if type(mr_iid) is not int or mr_iid <= 0:
        raise ValueError("mr_iid must be a positive integer")
    sha = normalize_sha(sha)
    return (
        f"<!-- reviewflow:v1;project={project_id};mr={mr_iid};"
        f"sha={sha};effect=note -->"
    )


def has_exact_note_marker(body: object, marker: object) -> bool:
    """Require one complete canonical marker line and reject all near matches."""

    if (
        not isinstance(body, str)
        or not isinstance(marker, str)
        or _NOTE_MARKER_RE.fullmatch(marker) is None
    ):
        return False
    return sum(line == marker for line in body.splitlines()) == 1


def is_valid_sha(value: object) -> bool:
    """Return whether *value* is an exact Git SHA accepted by ReviewFlow."""

    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def normalize_sha(value: object) -> str:
    """Return the canonical lowercase form of an accepted Git SHA."""

    if not is_valid_sha(value):
        raise ValueError("invalid SHA: expected 40 or 64 hexadecimal characters")
    return str(value).lower()


@dataclass(frozen=True)
class MergeRequest:
    project_id: str
    iid: int
    title: str
    author_username: str
    source_branch: str
    target_branch: str
    sha: str
    web_url: str
    draft: bool = False
    description: str = ""
    state: str = "opened"
    merged_sha: str | None = None
    source_project_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_project_id is None:
            object.__setattr__(self, "source_project_id", self.project_id)


@dataclass(frozen=True)
class Pipeline:
    project_id: str
    pipeline_id: int
    sha: str
    status: str


@dataclass(frozen=True)
class Note:
    project_id: str
    mr_iid: int
    note_id: int
    body: str


@dataclass(frozen=True)
class NoteMarkerLookup:
    """Trusted Review Bot Notes that exactly or conflictingly contain one marker."""

    exact: tuple[Note, ...]
    conflicts: tuple[Note, ...]


class NoteObserver(Protocol):
    """Capability for observing Program-owned GitLab Notes."""

    def find_note_by_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> Note | None: ...

    def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool: ...

    def lookup_note_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> NoteMarkerLookup: ...

    def get_trusted_note(
        self, project_id: str, mr_iid: int, note_id: int
    ) -> Note | None: ...


@dataclass(frozen=True)
class CreatedMergeRequest:
    project_id: str
    iid: int
    title: str
    source_branch: str
    target_branch: str
    web_url: str
    description: str = ""
    state: str = "opened"
    merged_sha: str | None = None
    author_username: str = ""
    draft: bool = False


@dataclass(frozen=True)
class CreatedCommit:
    project_id: str
    sha: str
    branch: str


@dataclass(frozen=True)
class RepositoryAction:
    """One semantic repository change observed at a GitLab commit."""

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
class RepositoryCommitObservation:
    """Bounded remote evidence used only by explicit Operator recovery."""

    project_id: str
    sha: str
    parent_ids: tuple[str, ...]
    actions: tuple[RepositoryAction, ...]


@dataclass(frozen=True)
class RepositoryFile:
    """One repository file read at an exact resolved branch commit."""

    project_id: str
    file_path: str
    ref_sha: str
    content: str | None


class GitLabClient(Protocol):
    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest: ...

    def get_latest_pipeline(self, project_id: str, sha: str) -> Pipeline | None: ...

    def get_repository_file(
        self, project_id: str, file_path: str, branch: str
    ) -> RepositoryFile: ...

    def create_note(self, project_id: str, mr_iid: int, body: str) -> Note: ...

    def find_note_by_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> Note | None: ...

    def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool: ...

    def lookup_note_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> NoteMarkerLookup: ...

    def get_trusted_note(
        self, project_id: str, mr_iid: int, note_id: int
    ) -> Note | None: ...

    def create_branch(self, project_id: str, branch: str, ref: str) -> None: ...

    def create_repository_commit(
        self,
        project_id: str,
        branch: str,
        start_sha: str,
        commit_message: str,
        actions: tuple[dict[str, object], ...],
        *,
        force: bool = False,
    ) -> CreatedCommit: ...

    def create_merge_request(
        self,
        project_id: str,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str,
        *,
        draft: bool = False,
    ) -> CreatedMergeRequest: ...

    def find_merge_request_by_source_branch(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        marker: str | None = None,
    ) -> CreatedMergeRequest | None: ...

    def observe_repository_commit(
        self, project_id: str, commit_sha: str
    ) -> RepositoryCommitObservation: ...

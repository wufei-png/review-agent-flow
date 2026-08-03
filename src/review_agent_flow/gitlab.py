"""Narrow GitLab interfaces used by the workflow core."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_NOTE_MARKER_RE = re.compile(r"^<!-- review-run:[1-9][0-9]*:note -->$")


def canonical_note_marker(run_id: int) -> str:
    """Return the exact standalone token agents must put in a review Note."""

    if type(run_id) is not int or run_id <= 0:
        raise ValueError("run_id must be a positive integer")
    return f"<!-- review-run:{run_id}:note -->"


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


class NoteObserver(Protocol):
    """Optional capability for observing Agent-owned GitLab Notes."""

    def find_note_by_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> Note | None: ...

    def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool: ...


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


class GitLabClient(Protocol):
    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest: ...

    def get_latest_pipeline(self, project_id: str, sha: str) -> Pipeline | None: ...

    def create_note(self, project_id: str, mr_iid: int, body: str) -> None: ...

    def find_note_by_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> Note | None: ...

    def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool: ...

    def create_branch(self, project_id: str, branch: str, ref: str) -> None: ...

    def create_merge_request(
        self,
        project_id: str,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str,
    ) -> CreatedMergeRequest: ...

    def find_merge_request_by_source_branch(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        marker: str | None = None,
    ) -> CreatedMergeRequest | None: ...

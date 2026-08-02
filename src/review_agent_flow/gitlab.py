"""Narrow GitLab interfaces used by the workflow core."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


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
    state: str = "opened"


@dataclass(frozen=True)
class Pipeline:
    project_id: str
    pipeline_id: int
    sha: str
    status: str


@dataclass(frozen=True)
class CreatedMergeRequest:
    project_id: str
    iid: int
    title: str
    source_branch: str
    target_branch: str
    web_url: str


class GitLabClient(Protocol):
    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest: ...

    def get_latest_pipeline(self, project_id: str, sha: str) -> Pipeline | None: ...

    def create_note(self, project_id: str, mr_iid: int, body: str) -> None: ...

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
        self, project_id: str, source_branch: str, target_branch: str
    ) -> CreatedMergeRequest | None: ...

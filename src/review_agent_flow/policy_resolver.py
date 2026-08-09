"""Resolve untrusted Project Policy from an exact target-branch commit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .gitlab import GitLabClient
from .policy import ReviewPolicy, parse_policy

DEFAULT_PROJECT_POLICY_PATH = ".gitlab/reviewflow.yml"


@dataclass(frozen=True)
class ResolvedProjectPolicy:
    target_branch: str
    target_sha: str
    policy: ReviewPolicy


class ProjectPolicyResolver(Protocol):
    def resolve(self, project_id: str, target_branch: str) -> ResolvedProjectPolicy: ...


class GitLabTargetBranchPolicyResolver:
    """Read policy through GitLab after resolving the target branch to a commit."""

    def __init__(
        self,
        gitlab: GitLabClient,
        policy_path: str = DEFAULT_PROJECT_POLICY_PATH,
    ) -> None:
        if not policy_path or policy_path.startswith("/") or ".." in policy_path.split("/"):
            raise ValueError("Project Policy path must be a repository-relative safe path")
        self.gitlab = gitlab
        self.policy_path = policy_path

    def resolve(self, project_id: str, target_branch: str) -> ResolvedProjectPolicy:
        file = self.gitlab.get_repository_file(project_id, self.policy_path, target_branch)
        policy = ReviewPolicy() if file.content is None else parse_policy(file.content)
        return ResolvedProjectPolicy(target_branch, file.ref_sha, policy)

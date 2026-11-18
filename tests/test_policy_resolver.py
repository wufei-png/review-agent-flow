from review_agent_flow.gitlab import RepositoryFile
from review_agent_flow.policy_resolver import GitLabTargetBranchPolicyResolver


class PolicyGitLab:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.calls: list[tuple[str, str, str]] = []

    def get_repository_file(self, project_id: str, file_path: str, branch: str) -> RepositoryFile:
        self.calls.append((project_id, file_path, branch))
        return RepositoryFile(project_id, file_path, "b" * 40, self.content)


def test_resolver_reads_policy_at_exact_target_branch_commit() -> None:
    gitlab = PolicyGitLab(
        """
version: 4
pipeline_gate: true
human:
  enabled: false
notifications:
  channels: [reviewers]
"""
    )

    resolved = GitLabTargetBranchPolicyResolver(gitlab).resolve("42", "main")

    assert gitlab.calls == [("42", ".gitlab/reviewflow.yml", "main")]
    assert resolved.target_sha == "b" * 40
    assert resolved.policy.pipeline_gate is True
    assert resolved.policy.human.enabled is False
    assert resolved.policy.notifications.channels == ("reviewers",)


def test_missing_target_branch_policy_uses_safe_defaults() -> None:
    resolved = GitLabTargetBranchPolicyResolver(PolicyGitLab(None)).resolve("42", "main")

    assert resolved.policy.human.enabled is True
    assert resolved.policy.ai.enabled is False

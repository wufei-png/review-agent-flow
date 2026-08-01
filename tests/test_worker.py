import subprocess
import sys
from pathlib import Path

import pytest

from review_agent_flow.gitlab import CreatedMergeRequest, MergeRequest, Pipeline
from review_agent_flow.notifications import FanoutNotifier, NotificationEvent, NotificationResult
from review_agent_flow.policy import AIPolicy, HumanPolicy, ReviewPolicy
from review_agent_flow.store import SQLiteStore
from review_agent_flow.worker import ReviewWorker
from review_agent_flow.worktree import GitWorktreeManager


class FakeGitLab:
    def __init__(self, mr: MergeRequest):
        self.mr = mr

    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest:
        return self.mr

    def get_latest_pipeline(self, project_id: str, sha: str) -> Pipeline:
        return Pipeline(project_id, 1, sha, "success")

    def create_note(self, project_id: str, mr_iid: int, body: str) -> None:
        pass

    def create_branch(self, project_id: str, branch: str, ref: str) -> None:
        pass

    def create_merge_request(
        self, project_id: str, title: str, source_branch: str, target_branch: str, description: str
    ) -> CreatedMergeRequest:
        return CreatedMergeRequest(project_id, 2, title, source_branch, target_branch, "url")

    def find_merge_request_by_source_branch(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        marker: str | None = None,
    ) -> CreatedMergeRequest | None:
        return None


class RecordingNotifier(FanoutNotifier):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[NotificationEvent] = []
        self.attempts = 0

    def channel_specs(self) -> tuple[tuple[str, str], ...]:
        return (("recording", "recording"),)

    def notify_channel(self, event: NotificationEvent, channel_id: str) -> NotificationResult:
        self.events.append(event)
        self.attempts += 1
        return NotificationResult(channel_id, True)


class FlakyNotifier(RecordingNotifier):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def notify_channel(self, event: NotificationEvent, channel_id: str) -> NotificationResult:
        self.events.append(event)
        self.attempts += 1
        if self.attempts <= self.failures:
            return NotificationResult(channel_id, False, "temporary outage")
        return NotificationResult(channel_id, True)


def _git_repository(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_worker_runs_agent_in_exact_sha_worktree_and_cleans_it(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, sha)
    notifier = RecordingNotifier()
    policy = ReviewPolicy(
        human=HumanPolicy(enabled=True, reviewers=("alice",)),
        ai=AIPolicy(
            enabled=True,
            command=(sys.executable, "-c", "import os; assert os.getcwd(); print('reviewed')"),
        ),
    )
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        policy,
        repository,
        "https://gitlab",
        project_id="42",
        notifier=notifier,
    )
    result = worker.process_one()
    assert result is not None
    assert result.status == "succeeded"
    assert result.agent_result is not None
    assert result.agent_result.stdout.strip() == "reviewed"
    assert not (tmp_path / "worktrees" / "run-1").exists()
    assert notifier.events[0].recipients == ("alice",)


def test_worker_abandons_auto_fix_intent_when_no_child_mr_is_found(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                auto_fix=True,
                command=(sys.executable, "-c", "print('reviewed')"),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "succeeded"
    intent = store.get_auto_fix_intent(run.run_id)
    assert intent is not None and intent.status == "abandoned"


def test_worker_retries_agent_for_auto_fix_when_only_note_exists(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")

    class RecoveringGitLab(FakeGitLab):
        def __init__(self, merge_request: MergeRequest) -> None:
            super().__init__(merge_request)
            self.lookups = 0

        def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool:
            return True

        def find_merge_request_by_source_branch(
            self,
            project_id: str,
            source_branch: str,
            target_branch: str,
            marker: str | None = None,
        ) -> CreatedMergeRequest | None:
            self.lookups += 1
            if self.lookups == 1:
                return None
            return CreatedMergeRequest(
                project_id,
                2,
                "[ReviewFlow Auto-Fix] Improve validation",
                source_branch,
                target_branch,
                "auto-fix-url",
                marker or "",
            )

    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        RecoveringGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                auto_fix=True,
                command=(sys.executable, "-c", "print('recovered')"),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "succeeded"
    assert result.agent_result is not None
    assert result.agent_result.stdout.strip() == "recovered"
    intent = store.get_auto_fix_intent(run.run_id)
    assert intent is not None and intent.status == "created"


def test_auto_fix_lookup_failure_requeues_without_running_agent(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")

    class FailingLookupGitLab(FakeGitLab):
        def find_merge_request_by_source_branch(
            self,
            project_id: str,
            source_branch: str,
            target_branch: str,
            marker: str | None = None,
        ) -> CreatedMergeRequest | None:
            raise RuntimeError("GitLab unavailable")

    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        FailingLookupGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                auto_fix=True,
                command=(sys.executable, "-c", "raise SystemExit('Agent must not run')"),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
        retry_delay_seconds=0,
    )

    result = worker.process_one()

    assert result is not None and result.status == "deferred"
    assert result.agent_result is None
    assert store.get_auto_fix_intent(run.run_id).status == "planned"
    assert store.claim_next().run_id == run.run_id


def test_worker_requires_project_binding(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="project_id"):
        ReviewWorker(
            SQLiteStore(tmp_path / "review.db"),
            object(),
            object(),
            ReviewPolicy(),
            tmp_path,
            "https://gitlab",
        )


def test_worktree_registration_failure_does_not_create_an_untracked_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")

    class FailingStore(SQLiteStore):
        def register_worktree(self, run_id: int, path: Path, sha: str):
            raise RuntimeError("database unavailable")

    class RecordingManager(GitWorktreeManager):
        def __init__(self, repository: Path, root: Path) -> None:
            super().__init__(repository, root)
            self.removed: list[Path] = []

        def remove(self, path: Path) -> None:
            self.removed.append(path)
            super().remove(path)

    store = FailingStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worktrees = RecordingManager(repository, tmp_path / "worktrees")
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        worktrees,
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                auto_fix=True,
                command=(sys.executable, "-c", "print('reviewed')"),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "failed"
    assert worktrees.removed == []
    assert not worktrees.path_for("1").exists()
    assert store.get_auto_fix_intent(run.run_id).status == "abandoned"


def test_preexisting_worktree_is_not_deleted_on_path_conflict(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")
    worktrees = GitWorktreeManager(repository, tmp_path / "worktrees")
    existing = worktrees.create(sha, "1")
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        worktrees,
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                auto_fix=True,
                command=(sys.executable, "-c", "print('reviewed')"),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "failed"
    assert existing.exists()
    assert store.get_worktree_record(1) is None
    assert store.get_auto_fix_intent(1).status == "abandoned"


def test_worktree_cleanup_failure_is_retried_without_rerunning_agent(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, sha)

    class FailOnceManager(GitWorktreeManager):
        def __init__(self, repository: Path, root: Path) -> None:
            super().__init__(repository, root)
            self.failures = 1

        def remove(self, path: Path) -> None:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("temporary cleanup failure")
            super().remove(path)

    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        FailOnceManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                command=(sys.executable, "-c", "print('reviewed')"),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
        retry_delay_seconds=0,
    )
    result = worker.process_one()
    assert result is not None and result.status == "succeeded"
    assert "cleanup pending" in result.detail
    record = store.get_worktree_record(1)
    assert record is not None and record.status == "pending_cleanup"
    assert worker.process_worktree_cleanup() == 1
    assert store.get_worktree_record(1).status == "cleaned"


def test_worker_uses_packaged_skill_when_project_skill_is_missing(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, sha)
    policy = ReviewPolicy(
        ai=AIPolicy(
            enabled=True,
            command=(
                sys.executable,
                "-c",
                "import os; print(os.environ['REVIEW_AGENT_FLOW_SKILL'])",
            ),
            skill_path=".gitlab/review-agent/missing.md",
        )
    )
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        policy,
        repository,
        "https://gitlab",
        project_id="42",
    )
    result = worker.process_one()
    assert result is not None
    assert result.status == "succeeded"
    assert result.agent_result is not None
    assert result.agent_result.stdout.strip().endswith("defaults/review-agent/SKILL.md")


def test_worker_requeues_draft_mr_before_human_or_ai_paths(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest(
        "42", 1, "Draft change", "alice", "feature", "main", sha, "https://mr", draft=True
    )
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    notifier = RecordingNotifier()
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(human=HumanPolicy(enabled=True, reviewers=("alice",))),
        repository,
        "https://gitlab",
        project_id="42",
        notifier=notifier,
        retry_delay_seconds=3600,
    )
    result = worker.process_one()
    assert result == result.__class__(run.run_id, "deferred", detail="draft merge request")
    assert notifier.events == []


def test_notification_failure_does_not_fail_review_run_and_is_retried(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    notifier = FlakyNotifier(failures=1)
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(human=HumanPolicy(enabled=True, reviewers=("alice",))),
        repository,
        "https://gitlab",
        project_id="42",
        notifier=notifier,
        retry_delay_seconds=0,
    )
    result = worker.process_one()
    assert result is not None
    assert result.status == "succeeded"
    assert notifier.attempts == 1
    assert worker.process_notifications() == 1
    assert notifier.attempts == 2
    assert store.get_notification_delivery(1).status == "delivered"
    assert store.get_notification_delivery(1).correlation_id == f"review-run:{run.run_id}:human"


def test_notification_channels_are_isolated_and_terminal_failure_is_audited(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, sha)

    class MixedNotifier(FanoutNotifier):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def channel_specs(self) -> tuple[tuple[str, str], ...]:
            return (("failing", "feishu"), ("working", "wecom"))

        def notify_channel(self, event: NotificationEvent, channel_id: str) -> NotificationResult:
            self.calls.append(channel_id)
            if channel_id == "failing":
                return NotificationResult(channel_id, False, "permanent outage")
            return NotificationResult(channel_id, True)

    notifier = MixedNotifier()
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(human=HumanPolicy(enabled=True, reviewers=("alice",))),
        repository,
        "https://gitlab",
        project_id="42",
        notifier=notifier,
        retry_delay_seconds=0,
    )
    result = worker.process_one()
    assert result is not None and result.status == "succeeded"
    assert notifier.calls == ["failing", "working"]
    assert worker.process_notifications(limit=2) == 2
    assert store.get_notification_delivery(1).status == "failed"
    assert store.get_notification_delivery(2).status == "delivered"

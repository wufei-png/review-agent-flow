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
        self, project_id: str, source_branch: str, target_branch: str
    ) -> CreatedMergeRequest | None:
        return None


class RecordingNotifier(FanoutNotifier):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[NotificationEvent] = []
        self.attempts = 0

    def channel_specs(self) -> tuple[tuple[int, str], ...]:
        return ((0, "recording"),)

    def notify_channel(self, event: NotificationEvent, channel_index: int) -> NotificationResult:
        self.events.append(event)
        self.attempts += 1
        return NotificationResult("recording", True)


class FlakyNotifier(RecordingNotifier):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def notify_channel(self, event: NotificationEvent, channel_index: int) -> NotificationResult:
        self.events.append(event)
        self.attempts += 1
        if self.attempts <= self.failures:
            return NotificationResult("recording", False, "temporary outage")
        return NotificationResult("recording", True)


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
        notifier=notifier,
        project_id="42",
    )
    result = worker.process_one()
    assert result is not None
    assert result.status == "succeeded"
    assert result.agent_result is not None
    assert result.agent_result.stdout.strip() == "reviewed"
    assert not (tmp_path / "worktrees" / "run-1").exists()
    assert notifier.events[0].recipients == ("alice",)


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
        notifier=notifier,
        retry_delay_seconds=3600,
        project_id="42",
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
        notifier=notifier,
        retry_delay_seconds=0,
        project_id="42",
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
            self.calls: list[int] = []

        def channel_specs(self) -> tuple[tuple[int, str], ...]:
            return ((0, "failing"), (1, "working"))

        def notify_channel(self, event: NotificationEvent, channel_index: int) -> NotificationResult:
            self.calls.append(channel_index)
            if channel_index == 0:
                return NotificationResult("failing", False, "permanent outage")
            return NotificationResult("working", True)

    notifier = MixedNotifier()
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(human=HumanPolicy(enabled=True, reviewers=("alice",))),
        repository,
        "https://gitlab",
        notifier=notifier,
        retry_delay_seconds=0,
        project_id="42",
    )
    result = worker.process_one()
    assert result is not None and result.status == "succeeded"
    assert notifier.calls == [0, 1]
    assert worker.process_notifications(limit=2) == 2
    assert store.get_notification_delivery(1).status == "failed"
    assert store.get_notification_delivery(2).status == "delivered"


def test_worker_cancels_stale_run_and_requeues_current_head_without_side_effects(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    old_sha = "a" * 40
    current_sha = "b" * 40
    mr = MergeRequest(
        "42", 1, "Improve validation", "alice", "feature", "main", current_sha, "https://mr"
    )
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, old_sha)
    notifier = RecordingNotifier()
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            human=HumanPolicy(enabled=True, reviewers=("alice",)),
            ai=AIPolicy(enabled=True, command=(sys.executable, "-c", "raise SystemExit(99)")),
        ),
        repository,
        "https://gitlab",
        notifier=notifier,
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None
    assert result.status == "cancelled"
    assert "stale run SHA" in result.detail
    assert notifier.events == []
    requeued = store.claim_next(project_id="42")
    assert requeued is not None
    assert requeued.run_id != run.run_id
    assert requeued.sha == current_sha


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_worker_does_not_start_or_notify_for_closed_or_merged_mr(
    state: str, tmp_path: Path
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = "c" * 40
    mr = MergeRequest(
        "42",
        1,
        "Closed change",
        "alice",
        "feature",
        "main",
        sha,
        "https://mr",
        state=state,
    )
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    notifier = RecordingNotifier()
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            human=HumanPolicy(enabled=True, reviewers=("alice",)),
            ai=AIPolicy(enabled=True, command=(sys.executable, "-c", "raise SystemExit(99)")),
        ),
        repository,
        "https://gitlab",
        notifier=notifier,
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None
    assert result.run_id == run.run_id
    assert result.status == "cancelled"
    assert notifier.events == []
    assert not (tmp_path / "worktrees" / "run-1").exists()


def test_worker_claims_only_its_bound_project(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = "d" * 40
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("99", 1, sha)
    worker = ReviewWorker(
        store,
        FakeGitLab(
            MergeRequest("42", 1, "Different project", "alice", "feature", "main", sha, "https://mr")
        ),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(),
        repository,
        "https://gitlab",
        project_id="42",
    )

    assert worker.process_one() is None
    pending = store.claim_next(project_id="99")
    assert pending is not None

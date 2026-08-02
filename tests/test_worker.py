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


class RetryableGitLab(FakeGitLab):
    def __init__(self, mr: MergeRequest, failures: int = 1, status_code: int = 503):
        super().__init__(mr)
        self.failures = failures
        self.status_code = status_code

    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest:
        if self.failures:
            self.failures -= 1
            error = RuntimeError(f"GitLab HTTP {self.status_code}: super-secret-token")
            error.status_code = self.status_code
            raise error
        return self.mr


class EventualAutoFixGitLab(FakeGitLab):
    def __init__(self, mr: MergeRequest):
        super().__init__(mr)
        self.auto_fix: CreatedMergeRequest | None = None

    def find_merge_request_by_source_branch(
        self, project_id: str, source_branch: str, target_branch: str
    ) -> CreatedMergeRequest | None:
        return self.auto_fix


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


def test_retryable_gitlab_failure_requeues_then_fails_at_attempt_cap(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = "e" * 40
    mr = MergeRequest("42", 1, "Retry me", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        RetryableGitLab(mr, failures=2, status_code=503),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(),
        repository,
        "https://gitlab",
        gitlab_token="super-secret-token",
        project_id="42",
        max_attempts=2,
        retry_delay_seconds=0,
    )

    first = worker.process_one()
    assert first is not None and first.status == "deferred"
    pending = store.get_review_run(run.run_id)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.failure_class == "gitlab_server_error"
    assert pending.retryable is True
    assert "super-secret-token" not in (pending.error or "")
    assert len(store.list_failures(run.run_id)) == 1

    second = worker.process_one()
    assert second is not None and second.status == "failed"
    terminal = store.get_review_run(run.run_id)
    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.attempts == 2
    assert terminal.failure_class == "gitlab_server_error"
    assert len(store.list_failures(run.run_id)) == 2
    assert worker.process_one() is None


def test_agent_nonzero_exit_is_terminal_and_recorded_without_retry(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Agent failure", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    token = "runtime-agent-token-123"
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                command=(
                    sys.executable,
                    "-c",
                    "import os, sys; sys.stderr.write(os.environ['GITLAB_TOKEN']); sys.exit(7)",
                ),
            )
        ),
        repository,
        "https://gitlab",
        gitlab_token=token,
        project_id="42",
        retry_delay_seconds=0,
    )

    result = worker.process_one()
    assert result is not None and result.status == "failed"
    saved = store.get_review_run(run.run_id)
    assert saved is not None
    assert saved.status == "failed"
    assert saved.failure_class == "agent_exit"
    assert saved.retryable is False
    assert token not in (result.agent_result.stderr if result.agent_result else "")
    assert token not in (saved.error or "")
    failures = store.list_failures(run.run_id)
    assert len(failures) == 1
    assert token not in (failures[0].error or "")
    assert worker.process_one() is None
    agent_receipt = store.list_effect_receipts(run.run_id, "agent_execution")
    assert len(agent_receipt) == 1
    assert agent_receipt[0].state == "failed"
    assert token not in (agent_receipt[0].error or "")


def test_agent_receipts_and_eventual_merged_autofix_reconciliation(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Auto-fix", "alice", "feature", "main", sha, "https://mr")
    gitlab = EventualAutoFixGitLab(mr)
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        gitlab,
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
        gitlab_token="super-secret-token",
        project_id="42",
        retry_delay_seconds=0,
    )

    first = worker.process_one()
    assert first is not None and first.status == "succeeded"
    receipts = store.list_effect_receipts(run.run_id)
    assert {receipt.effect_kind for receipt in receipts} == {
        "agent_execution",
        "note_expected",
        "autofix_expected",
        "autofix_reconciliation",
    }
    assert next(receipt for receipt in receipts if receipt.effect_kind == "agent_execution").state == "succeeded"
    assert next(receipt for receipt in receipts if receipt.effect_kind == "autofix_reconciliation").state == "pending"

    gitlab.auto_fix = CreatedMergeRequest(
        "42",
        9,
        "[ReviewFlow Auto-Fix] Auto-fix",
        "autofix/MR-1/run-1",
        "feature",
        "https://gitlab/merge_requests/9?access_token=super-secret-token",
        state="merged",
        merged_sha="f" * 40,
    )
    reconciled = worker.process_one()
    assert reconciled is not None and reconciled.status == "succeeded"
    relation = store.record_auto_fix(
        "42", 1, sha, run.run_id, "42", 9, status="merged", merged_sha="f" * 40
    )
    assert relation.status == "merged"
    assert store.is_suppressed_parent_sha("42", 1, "f" * 40) is True
    reconciliation = store.list_effect_receipts(run.run_id, "autofix_reconciliation")
    assert reconciliation[0].state == "observed"
    assert "super-secret-token" not in (reconciliation[0].detail or "")


def test_closed_autofix_is_reconciled_without_parent_sha_suppression(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Closed Auto-fix", "alice", "feature", "main", sha, "https://mr")
    gitlab = EventualAutoFixGitLab(mr)
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        gitlab,
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(enabled=True, auto_fix=True, command=(sys.executable, "-c", "print('ok')"))
        ),
        repository,
        "https://gitlab",
        project_id="42",
        retry_delay_seconds=0,
    )
    assert worker.process_one() is not None
    gitlab.auto_fix = CreatedMergeRequest(
        "42",
        10,
        "[ReviewFlow Auto-Fix] Closed",
        "autofix/MR-1/run-1",
        "feature",
        "https://gitlab/merge_requests/10",
        state="closed",
    )
    result = worker.process_one()
    assert result is not None and result.status == "succeeded"
    assert store.is_suppressed_parent_sha("42", 1, sha) is False

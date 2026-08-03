import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from review_agent_flow.gitlab import (
    CreatedMergeRequest,
    MergeRequest,
    Note,
    Pipeline,
    canonical_note_marker,
)
from review_agent_flow.notifications import FanoutNotifier, NotificationEvent, NotificationResult
from review_agent_flow.policy import AIPolicy, HumanPolicy, ReviewPolicy
from review_agent_flow.runner import AgentResult
from review_agent_flow.store import SQLiteStore
from review_agent_flow.worker import ReviewWorker
from review_agent_flow.worktree import GitWorktreeManager, WorktreeError


class ProjectId(str):
    pass


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


class ObservedNoteGitLab(FakeGitLab):
    def __init__(
        self,
        mr: MergeRequest,
        body_template: str = "{marker}\nActionable finding",
        note_id: object = 314,
        returned_project_id: object | None = None,
        returned_mr_iid: object | None = None,
    ) -> None:
        super().__init__(mr)
        self.body_template = body_template
        self.note_id = note_id
        self.returned_project_id = returned_project_id
        self.returned_mr_iid = returned_mr_iid
        self.note_markers: list[str] = []

    def find_note_by_marker(
        self, project_id: str, mr_iid: int, marker: str
    ) -> Note:
        self.note_markers.append(marker)
        return Note(
            project_id if self.returned_project_id is None else self.returned_project_id,
            mr_iid if self.returned_mr_iid is None else self.returned_mr_iid,
            self.note_id,
            self.body_template.format(marker=marker),
        )


class FailingNoteObserverGitLab(FakeGitLab):
    def find_note_by_marker(self, project_id: str, mr_iid: int, marker: str) -> Note:
        raise RuntimeError("Note observer API unavailable")


class EventualAutoFixGitLab(FakeGitLab):
    def __init__(self, mr: MergeRequest):
        super().__init__(mr)
        self.auto_fix: CreatedMergeRequest | None = None
        self.get_calls = 0
        self.find_calls = 0

    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest:
        self.get_calls += 1
        return super().get_merge_request(project_id, mr_iid)

    def find_merge_request_by_source_branch(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        marker: str | None = None,
    ) -> CreatedMergeRequest | None:
        self.find_calls += 1
        return self.auto_fix


class AutoFixNotificationGitLab(FakeGitLab):
    def __init__(self, parent: MergeRequest, auto_fix: MergeRequest) -> None:
        super().__init__(auto_fix)
        self.parent = parent

    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest:
        return self.parent if mr_iid == self.parent.iid else self.mr


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
    gitlab = FakeGitLab(mr)
    assert gitlab.create_note("42", 1, "legacy note") is None
    assert not hasattr(gitlab, "find_note_by_marker")
    worker = ReviewWorker(
        store,
        gitlab,
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
    note_receipt = store.list_effect_receipts(1, "note_expected")[0]
    assert note_receipt.state == "not_observed"
    assert note_receipt.external_identity is None


def test_agent_note_receipt_observes_native_id_without_equating_agent_success_to_note_success(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Observe Note", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    gitlab = ObservedNoteGitLab(mr)
    worker = ReviewWorker(
        store,
        gitlab,
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                command=(
                    sys.executable,
                    "-c",
                    "import os; print(os.environ['REVIEW_AGENT_FLOW_NOTE_MARKER'])",
                ),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "succeeded"
    assert result.agent_result is not None
    marker = canonical_note_marker(run.run_id)
    assert result.agent_result.stdout.strip() == marker
    assert gitlab.note_markers == [marker]
    note_receipt = store.list_effect_receipts(run.run_id, "note_expected")[0]
    assert note_receipt.state == "observed"
    assert note_receipt.external_identity == "gitlab:42!1#note-314"
    assert store.list_effect_receipts(run.run_id, "agent_execution")[0].state == "succeeded"


def test_note_marker_observation_reuses_historical_receipt_key(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Stable Note receipt key", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    historical_key = f"review-run:{run.run_id}:note"
    existing = store.create_effect_receipt(
        run.run_id,
        "note_expected",
        historical_key,
        state="planned",
    )
    gitlab = ObservedNoteGitLab(mr)
    worker = ReviewWorker(
        store,
        gitlab,
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                command=(
                    sys.executable,
                    "-c",
                    "import os; print(os.environ['REVIEW_AGENT_FLOW_NOTE_MARKER'])",
                ),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "succeeded"
    receipts = store.list_effect_receipts(run.run_id, "note_expected")
    assert len(receipts) == 1
    assert receipts[0].receipt_id == existing.receipt_id
    assert receipts[0].dedupe_key == historical_key
    assert receipts[0].state == "observed"
    assert gitlab.note_markers == [canonical_note_marker(run.run_id)]


@pytest.mark.parametrize(
    ("body_template", "note_id"),
    [
        ("prefix {marker}", 314),
        ("{marker} suffix", 314),
        ("{marker}\n{marker}", 314),
        ("{marker}", 0),
        ("{marker}", -1),
        ("{marker}", True),
        ("{marker}", 1.0),
        ("{marker}", "1"),
    ],
)
def test_worker_keeps_noncanonical_or_invalid_observed_note_not_observed(
    body_template: str, note_id: object, tmp_path: Path
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Reject malformed Note", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        ObservedNoteGitLab(mr, body_template=body_template, note_id=note_id),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(ai=AIPolicy(enabled=True, command=(sys.executable, "-c", "print('reviewed')"))),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "succeeded"
    note_receipt = store.list_effect_receipts(run.run_id, "note_expected")[0]
    assert note_receipt.state == "not_observed"
    assert note_receipt.external_identity is None


def test_worker_keeps_note_observer_exception_not_observed(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Observer outage", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        FailingNoteObserverGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(ai=AIPolicy(enabled=True, command=(sys.executable, "-c", "print('reviewed')"))),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "succeeded"
    note_receipt = store.list_effect_receipts(run.run_id, "note_expected")[0]
    assert note_receipt.state == "not_observed"
    assert note_receipt.external_identity is None
    assert "observer API unavailable" in (note_receipt.error or "")


@pytest.mark.parametrize(
    ("returned_project_id", "returned_mr_iid"),
    [
        ("99", 1),
        (True, 1),
        (1.0, 1),
        ("1", 1),
        (ProjectId("42"), 1),
        ("42", 2),
        ("42", True),
        ("42", 1.0),
        ("42", "1"),
        ("42", ProjectId("1")),
        ("42", 0),
        ("42", -1),
    ],
)
def test_worker_rejects_note_from_wrong_project_or_mr(
    returned_project_id: object, returned_mr_iid: object, tmp_path: Path
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Reject misbound Note", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        ObservedNoteGitLab(
            mr,
            returned_project_id=returned_project_id,
            returned_mr_iid=returned_mr_iid,
        ),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(ai=AIPolicy(enabled=True, command=(sys.executable, "-c", "print('reviewed')"))),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "succeeded"
    note_receipt = store.list_effect_receipts(run.run_id, "note_expected")[0]
    assert note_receipt.state == "not_observed"
    assert note_receipt.external_identity is None


def test_worker_keeps_auto_fix_intent_while_child_mr_reconciliation_is_pending(
    tmp_path: Path,
) -> None:
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
    assert intent is not None and intent.status == "planned"


def test_worker_retries_agent_for_auto_fix_when_only_note_exists(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Improve validation", "alice", "feature", "main", sha, "https://mr")

    class RecoveringGitLab(FakeGitLab):
        def __init__(self, merge_request: MergeRequest) -> None:
            super().__init__(merge_request)
            self.lookups = 0
            self.note_markers: list[str] = []

        def has_note_marker(self, project_id: str, mr_iid: int, marker: str) -> bool:
            self.note_markers.append(marker)
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
    gitlab = RecoveringGitLab(mr)
    worker = ReviewWorker(
        store,
        gitlab,
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
    assert gitlab.note_markers == [canonical_note_marker(run.run_id)]
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


def test_retryable_pre_agent_failure_keeps_auto_fix_intent(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Retry worktree", "alice", "feature", "main", sha, "https://mr")

    class FailingCreateManager(GitWorktreeManager):
        def create(self, sha: str, worktree_id: str) -> Path:
            raise WorktreeError("temporary checkout transport failure")

    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        FailingCreateManager(repository, tmp_path / "worktrees"),
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
    intent = store.get_auto_fix_intent(run.run_id)
    assert intent is not None and intent.status == "planned"
    saved = store.get_review_run(run.run_id)
    assert saved is not None and saved.status == "pending"


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


def test_worker_does_not_reconcile_another_project_receipt(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = "f" * 40
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("99", 1, sha)
    receipt = store.create_effect_receipt(
        run.run_id,
        "autofix_reconciliation",
        "autofix/MR-1/run-1",
        state="pending",
    )
    gitlab = EventualAutoFixGitLab(
        MergeRequest("99", 1, "Other project", "alice", "feature", "main", sha, "https://mr")
    )
    worker = ReviewWorker(
        store,
        gitlab,
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(),
        repository,
        "https://gitlab",
        project_id="42",
    )

    assert worker.reconcile_auto_fixes() is None
    assert gitlab.get_calls == 0
    assert gitlab.find_calls == 0
    saved = store.get_effect_receipt(receipt.receipt_id)
    assert saved is not None
    assert saved.state == "pending"


def test_worker_rejects_cross_project_claim_before_gitlab_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = "f" * 40
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("99", 1, sha)
    receipt = store.create_effect_receipt(
        run.run_id,
        "autofix_reconciliation",
        "autofix/MR-1/run-1",
        state="pending",
    )
    gitlab = EventualAutoFixGitLab(
        MergeRequest("99", 1, "Other project", "alice", "feature", "main", sha, "https://mr")
    )
    monkeypatch.setattr(
        store,
        "claim_pending_effect_receipts",
        lambda *args, **kwargs: (receipt,),
    )
    worker = ReviewWorker(
        store,
        gitlab,
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.reconcile_auto_fixes()

    assert result is not None and result.status == "failed"
    assert gitlab.get_calls == 0
    assert gitlab.find_calls == 0
    saved = store.get_effect_receipt(receipt.receipt_id)
    assert saved is not None
    assert saved.state == "failed"


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


@pytest.mark.parametrize("heartbeat_mode", ["false", "exception"])
def test_worker_lease_loss_is_requeued_and_bounded(
    heartbeat_mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Heartbeat failure", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(
            ai=AIPolicy(
                enabled=True,
                command=(sys.executable, "-c", "import time; time.sleep(2)"),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
        lease_seconds=1,
        agent_timeout_seconds=5,
        max_attempts=2,
        retry_delay_seconds=0,
    )
    calls_by_attempt: dict[int, int] = {}

    def heartbeat(claimed_run) -> bool:
        calls_by_attempt[claimed_run.attempts] = calls_by_attempt.get(claimed_run.attempts, 0) + 1
        if calls_by_attempt[claimed_run.attempts] == 1:
            return True
        if heartbeat_mode == "false":
            return False
        raise RuntimeError("heartbeat backend unavailable")

    monkeypatch.setattr(worker, "_heartbeat", heartbeat)

    first = worker.process_one()
    assert first is not None and first.status == "deferred"
    pending = store.get_review_run(run.run_id)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.failure_class == "lease_lost"
    assert pending.retryable is True
    assert pending.claim_token is None
    assert len(store.list_failures(run.run_id)) == 1

    second = worker.process_one()
    assert second is not None and second.status == "failed"
    terminal = store.get_review_run(run.run_id)
    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.failure_class == "lease_lost"
    assert terminal.retryable is True
    assert terminal.claim_token is None
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


def test_failed_agent_keeps_autofix_reconciliation_for_eventual_merge(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Auto-fix after failure", "alice", "feature", "main", sha, "https://mr")
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
                command=(
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('agent crashed'); sys.exit(7)",
                ),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
        retry_delay_seconds=0,
    )

    failed = worker.process_one()
    assert failed is not None and failed.status == "failed"
    receipts = store.list_effect_receipts(run.run_id)
    reconciliation = next(
        receipt for receipt in receipts if receipt.effect_kind == "autofix_reconciliation"
    )
    expected = next(receipt for receipt in receipts if receipt.effect_kind == "autofix_expected")
    assert reconciliation.state == "pending"
    assert reconciliation.dedupe_key == "autofix/MR-1/run-1"
    assert expected.state == "not_observed"

    merged_sha = "f" * 40
    gitlab.auto_fix = CreatedMergeRequest(
        "42",
        9,
        "[ReviewFlow Auto-Fix] Recovered",
        "autofix/MR-1/run-1",
        "feature",
        "https://gitlab/merge_requests/9",
        state="merged",
        merged_sha=merged_sha,
    )
    reconciled = worker.process_one()

    assert reconciled is not None and reconciled.status == "succeeded"
    assert store.is_suppressed_parent_sha("42", 1, merged_sha) is True
    saved_reconciliation = store.list_effect_receipts(
        run.run_id, "autofix_reconciliation"
    )[0]
    assert saved_reconciliation.state == "observed"


def test_agent_success_does_not_write_after_lease_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Lease fenced Agent", "alice", "feature", "main", sha, "https://mr")
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    run = store.enqueue("42", 1, sha)
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(ai=AIPolicy(enabled=True, auto_fix=True, command=(sys.executable, "-c", "print('unused')"))),
        repository,
        "https://gitlab",
        project_id="42",
        retry_delay_seconds=0,
    )

    def finish_after_expiry(claimed_run, merge_request, worktree, *_agent_context):
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE review_runs SET lease_until = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", claimed_run.run_id),
            )
        return AgentResult(0, "", "")

    monkeypatch.setattr(worker, "_run_agent", finish_after_expiry)

    result = worker.process_one()

    assert result is not None and result.status == "deferred"
    saved = store.get_review_run(run.run_id)
    assert saved is not None
    assert saved.status == "pending"
    assert saved.failure_class == "lease_lost"
    assert saved.status != "succeeded"
    assert store.list_effect_receipts(run.run_id, "autofix_reconciliation")[0].state == "pending"
    assert store.list_effect_receipts(run.run_id, "agent_execution")[0].state == "not_observed"
    with sqlite3.connect(database) as connection:
        relation_count = connection.execute("SELECT COUNT(*) FROM auto_fix_mrs").fetchone()[0]
    assert relation_count == 0


def test_agent_start_failure_closes_running_and_expected_receipts(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Agent startup failure", "alice", "feature", "main", sha, "https://mr")
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
                command=("review-agent-flow-command-does-not-exist",),
            )
        ),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "failed"
    saved = store.get_review_run(run.run_id)
    assert saved is not None
    assert saved.failure_class == "agent_configuration"
    receipts = {
        receipt.effect_kind: receipt
        for receipt in store.list_effect_receipts(run.run_id)
    }
    assert receipts["agent_execution"].state == "failed"
    assert receipts["note_expected"].state == "not_observed"
    assert receipts["autofix_expected"].state == "not_observed"


def test_partial_agent_effect_preparation_closes_created_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Receipt preparation failure", "alice", "feature", "main", sha, "https://mr")
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
    original_create = store.create_effect_receipt

    def create_effect_receipt(*args, **kwargs):
        effect_kind = args[1] if len(args) > 1 else kwargs["effect_kind"]
        if effect_kind == "note_expected":
            raise RuntimeError("note receipt write failed")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(store, "create_effect_receipt", create_effect_receipt)
    worker = ReviewWorker(
        store,
        FakeGitLab(mr),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(ai=AIPolicy(enabled=True, command=(sys.executable, "-c", "print('unused')"))),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "failed"
    saved = store.get_review_run(run.run_id)
    assert saved is not None
    assert saved.status == "failed"
    receipts = store.list_effect_receipts(run.run_id)
    assert len(receipts) == 1
    assert receipts[0].effect_kind == "agent_execution"
    assert receipts[0].state == "failed"


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
    intent = store.get_auto_fix_intent(run.run_id)
    assert intent is not None and intent.status == "planned"

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


def test_autofix_reconciliation_stops_after_bounded_not_observed_attempts(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Auto-fix bounded", "alice", "feature", "main", sha, "https://mr")
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
        project_id="42",
        retry_delay_seconds=0,
        reconciliation_max_attempts=2,
    )

    first = worker.process_one()
    assert first is not None and first.status == "succeeded"
    assert gitlab.find_calls == 1
    pending = store.list_effect_receipts(run.run_id, "autofix_reconciliation")[0]
    assert pending.state == "pending"

    exhausted = worker.process_one()
    assert exhausted is not None and exhausted.status == "succeeded"
    terminal = store.list_effect_receipts(run.run_id, "autofix_reconciliation")[0]
    assert terminal.state == "not_observed"
    assert terminal.attempt == 2
    assert terminal.next_attempt_at is None
    intent = store.get_auto_fix_intent(run.run_id)
    assert intent is not None and intent.status == "abandoned"
    assert gitlab.find_calls == 2

    assert worker.process_one() is None
    assert gitlab.find_calls == 2


@pytest.mark.parametrize(
    ("mismatch", "project_id", "source_branch", "target_branch"),
    [
        ("project", "99", "autofix/MR-1/run-1", "feature"),
        ("run", "42", "autofix/MR-1/run-999", "feature"),
        ("source", "42", "autofix/MR-2/run-1", "feature"),
        ("target", "42", "autofix/MR-1/run-1", "main"),
    ],
)
def test_reconciliation_rejects_mismatched_autofix_identity(
    mismatch: str,
    project_id: str,
    source_branch: str,
    target_branch: str,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    parent = MergeRequest("42", 1, "Auto-fix identity", "alice", "feature", "main", sha, "https://mr")
    gitlab = EventualAutoFixGitLab(parent)
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
        project_id="42",
        retry_delay_seconds=0,
    )

    first = worker.process_one()
    assert first is not None and first.status == "succeeded"
    gitlab.auto_fix = CreatedMergeRequest(
        project_id,
        9,
        f"[ReviewFlow Auto-Fix] {mismatch}",
        source_branch,
        target_branch,
        "https://gitlab/merge_requests/9",
    )

    rejected = worker.process_one()

    assert rejected is not None and rejected.status == "failed"
    reconciliation = store.list_effect_receipts(run.run_id, "autofix_reconciliation")[0]
    assert reconciliation.state == "failed"
    assert mismatch in rejected.detail or "Auto-fix" in rejected.detail
    assert store.is_suppressed_parent_sha("42", 1, "f" * 40) is False
    with sqlite3.connect(tmp_path / "review.db") as connection:
        relation_count = connection.execute("SELECT COUNT(*) FROM auto_fix_mrs").fetchone()[0]
    assert relation_count == 0


def test_autofix_run_rejects_wrong_branch_run_id(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = "a" * 40
    auto_fix = MergeRequest(
        "42",
        9,
        "Auto-fix notification",
        "alice",
        "autofix/MR-1/run-999",
        "feature",
        sha,
        "https://mr",
    )
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 9, sha, kind="autofix")
    worker = ReviewWorker(
        store,
        FakeGitLab(auto_fix),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.status == "failed"
    assert store.get_review_run(run.run_id).failure_class == "autofix_identity"
    identity_receipt = store.list_effect_receipts(run.run_id, "autofix_identity")[0]
    assert identity_receipt.state == "failed"
    with sqlite3.connect(tmp_path / "review.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM auto_fix_mrs").fetchone()[0] == 0


def test_autofix_child_uses_persisted_parent_run_id_for_identity(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    parent_sha = "a" * 40
    child_sha = "b" * 40
    parent = MergeRequest(
        "42", 1, "Parent", "alice", "feature", "main", parent_sha, "https://parent"
    )
    auto_fix = MergeRequest(
        "42",
        9,
        "Auto-fix",
        "review-bot",
        "autofix/MR-1/run-1",
        "feature",
        child_sha,
        "https://auto-fix",
    )
    store = SQLiteStore(tmp_path / "review.db")
    parent_run = store.enqueue("42", 1, parent_sha)
    claimed_parent = store.claim_next(project_id="42")
    assert claimed_parent is not None
    assert store.finish(parent_run.run_id, "succeeded", claim_token=claimed_parent.claim_token)
    store.record_auto_fix("42", 1, parent_sha, parent_run.run_id, "42", 9)
    child_run = store.enqueue("42", 9, child_sha, kind="autofix")
    worker = ReviewWorker(
        store,
        AutoFixNotificationGitLab(parent, auto_fix),
        GitWorktreeManager(repository, tmp_path / "worktrees"),
        ReviewPolicy(),
        repository,
        "https://gitlab",
        project_id="42",
    )

    result = worker.process_one()

    assert result is not None and result.run_id == child_run.run_id
    assert result.status == "succeeded"
    saved = store.get_review_run(child_run.run_id)
    assert saved is not None and saved.status == "succeeded"


def test_closed_autofix_is_reconciled_without_parent_sha_suppression(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sha = _git_repository(repository)
    mr = MergeRequest("42", 1, "Closed Auto-fix", "alice", "feature", "main", sha, "https://mr")
    gitlab = EventualAutoFixGitLab(mr)
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, sha)
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
        merged_sha="f" * 40,
    )
    result = worker.process_one()
    assert result is not None and result.status == "succeeded"
    relation = store.record_auto_fix(
        "42",
        1,
        sha,
        run.run_id,
        "42",
        10,
        status="closed",
        merged_sha="f" * 40,
    )
    assert relation.status == "closed"
    assert relation.merged_sha is None
    assert store.is_suppressed_parent_sha("42", 1, sha) is False
    assert store.is_suppressed_parent_sha("42", 1, "f" * 40) is False

import sqlite3
from pathlib import Path

from review_agent_flow.auto_fix_reconciler import AutoFixReconciler
from review_agent_flow.autofix_publisher import AutoFixPublisher
from review_agent_flow.gitlab import CreatedCommit, CreatedMergeRequest, MergeRequest
from review_agent_flow.notifications import FanoutNotifier, NotificationEvent, NotificationResult
from review_agent_flow.operator_config import AutoFixPublicationConfig
from review_agent_flow.store import SQLiteStore

SHA = "a" * 40


class Channel:
    channel_id = "autofix-alerts"
    channel = "feishu"

    def notify(self, event: NotificationEvent) -> NotificationResult:
        return NotificationResult(self.channel_id, True)


class FakeGitLab:
    def __init__(self, parent: MergeRequest) -> None:
        self.parent = parent
        self.get_failures = 0
        self.commit_error: Exception | None = None
        self.mr_error: Exception | None = None
        self.found_merge_request: CreatedMergeRequest | None = None
        self.child_lookup: MergeRequest | None = None
        self.commit_payloads: list[dict[str, object]] = []
        self.mr_payloads: list[dict[str, object]] = []

    def get_merge_request(self, project_id: str, mr_iid: int) -> MergeRequest:
        if self.get_failures:
            self.get_failures -= 1
            raise RuntimeError("temporary parent lookup failure")
        if self.child_lookup is not None and mr_iid == self.child_lookup.iid:
            return self.child_lookup
        return self.parent

    def create_repository_commit(
        self,
        project_id: str,
        branch: str,
        start_sha: str,
        commit_message: str,
        actions: tuple[dict[str, object], ...],
        *,
        force: bool = False,
    ) -> CreatedCommit:
        self.commit_payloads.append(
            {
                "project_id": project_id,
                "branch": branch,
                "start_sha": start_sha,
                "commit_message": commit_message,
                "actions": actions,
                "force": force,
            }
        )
        if self.commit_error is not None:
            raise self.commit_error
        return CreatedCommit(project_id, "f" * 40, branch)

    def create_merge_request(
        self,
        project_id: str,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str,
        *,
        draft: bool = False,
    ) -> CreatedMergeRequest:
        self.mr_payloads.append(
            {
                "title": title,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "description": description,
                "draft": draft,
            }
        )
        if self.mr_error is not None:
            raise self.mr_error
        return CreatedMergeRequest(
            project_id,
            9,
            title,
            source_branch,
            target_branch,
            "https://gitlab/merge_requests/9",
            description=description,
            author_username="review-bot",
            draft=draft,
        )

    def find_merge_request_by_source_branch(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        marker: str | None = None,
    ) -> CreatedMergeRequest | None:
        return self.found_merge_request


def _frozen_plan(tmp_path: Path) -> tuple[SQLiteStore, int, int, str]:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next(project_id="42")
    assert claimed is not None
    intent = store.create_auto_fix_publication_intent(
        claimed.run_id, claimed.claim_token
    )
    assert intent is not None
    receipt = store.create_effect_receipt(
        claimed.run_id,
        "autofix_plan",
        f"autofix-intent:{intent.nonce}",
        state="planned",
    )
    branch = f"autofix/MR-1/sha-{SHA}/intent-{intent.nonce}"
    payload = {
        "version": 1,
        "review_key": {"project_id": "42", "mr_iid": 1, "sha": SHA},
        "intent": {"generation": 1, "nonce": intent.nonce},
        "proposal_summary": "Apply fix",
        "candidate_sha256": "c" * 64,
        "actions": [
            {
                "action": "update",
                "file_path": "README.md",
                "content": "fixed\n",
                "execute_filemode": False,
            }
        ],
        "verification": {
            "candidate_sha256": "c" * 64,
            "command_sha256": "d" * 64,
        },
        "branch": branch,
        "marker_template": (
            f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
            f"intent={intent.nonce};plan_sha256={{plan_sha256}} -->"
        ),
        "commit_message": f"ReviewFlow Auto-fix for !1 at {SHA}\n\n[skip ci]",
        "title": "Draft: [ReviewFlow Auto-Fix] Parent",
        "target_branch": "feature",
        "initial_readiness": "draft",
        "ci_trigger": "manual",
    }
    plan = store.freeze_auto_fix_publication_plan(
        claimed.run_id, receipt.receipt_id, payload, claimed.claim_token
    )
    assert plan is not None
    assert store.finish(claimed.run_id, "succeeded", claim_token=claimed.claim_token)
    return store, run.run_id, plan.plan_id, branch


def _publisher(store: SQLiteStore, gitlab: FakeGitLab) -> AutoFixPublisher:
    return AutoFixPublisher(
        store,
        gitlab,
        "42",
        AutoFixPublicationConfig(True, frozenset({"42"}), "draft", "manual"),
        FanoutNotifier((Channel(),)),
        retry_delay_seconds=0,
    )


def test_publisher_uses_exact_commit_contract_and_atomically_requests_notification(
    tmp_path: Path,
) -> None:
    store, run_id, plan_id, branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )

    result = _publisher(store, gitlab).process(plan_id=plan_id)

    assert result is not None and result.status == "published"
    assert gitlab.commit_payloads == [
        {
            "project_id": "42",
            "branch": branch,
            "start_sha": SHA,
            "commit_message": f"ReviewFlow Auto-fix for !1 at {SHA}\n\n[skip ci]",
            "actions": (
                {
                    "action": "update",
                    "file_path": "README.md",
                    "content": "fixed\n",
                    "execute_filemode": False,
                },
            ),
            "force": False,
        }
    ]
    assert gitlab.mr_payloads[0]["draft"] is True
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None and plan.state == "published"
    assert plan.plan_json is None
    assert plan.candidate_sha256 == "c" * 64
    assert plan.verification_json
    assert plan.branch_commit_sha == "f" * 40
    relation = store.get_program_auto_fix_relation(plan_id)
    assert relation is not None and relation.source_branch == branch
    request = store.get_auto_fix_notification_request(relation.relation_id)
    assert request is not None
    assert request.recipients == ("alice",)
    with sqlite3.connect(tmp_path / "review.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_requests").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
    receipt = store.list_effect_receipts(run_id, "autofix_plan")[0]
    assert receipt.state == "published"


def test_lost_lease_before_commit_prevents_external_write(
    tmp_path: Path, monkeypatch
) -> None:
    store, _run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    monkeypatch.setattr(
        store,
        "extend_auto_fix_publication_lease",
        lambda _plan_id, _claim_token, _lease_seconds: False,
    )

    result = _publisher(store, gitlab).process(plan_id=plan_id)

    assert result is not None and result.status == "deferred"
    assert gitlab.commit_payloads == []
    assert gitlab.mr_payloads == []


def test_lost_lease_before_merge_request_stops_after_commit(
    tmp_path: Path, monkeypatch
) -> None:
    store, run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    lease_results = iter((True, False))
    monkeypatch.setattr(
        store,
        "extend_auto_fix_publication_lease",
        lambda _plan_id, _claim_token, _lease_seconds: next(lease_results),
    )

    result = _publisher(store, gitlab).process(plan_id=plan_id)

    assert result is not None and result.status == "deferred"
    assert len(gitlab.commit_payloads) == 1
    assert gitlab.mr_payloads == []
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None and plan.branch_commit_sha == "f" * 40
    assert store.get_program_auto_fix_relation(plan_id) is None


def test_prewrite_lookup_failure_retries_without_external_write(tmp_path: Path) -> None:
    store, _run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    gitlab.get_failures = 1
    publisher = _publisher(store, gitlab)

    first = publisher.process(plan_id=plan_id)
    second = publisher.process(plan_id=plan_id)

    assert first is not None and first.status == "deferred"
    assert second is not None and second.status == "published"
    assert len(gitlab.commit_payloads) == 1


def test_commit_error_stops_normal_retries_as_uncertain(tmp_path: Path) -> None:
    store, run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    gitlab.commit_error = RuntimeError("response lost")
    publisher = _publisher(store, gitlab)

    first = publisher.process(plan_id=plan_id)
    second = publisher.process(plan_id=plan_id)

    assert first is not None and first.status == "needs_reconciliation"
    assert second is None
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None and plan.write_uncertain is True
    assert store.get_auto_fix_publication_intent(run_id).state == "needs_reconciliation"
    assert store.list_effect_receipts(run_id, "autofix_plan")[0].state == (
        "needs_reconciliation"
    )
    assert len(gitlab.commit_payloads) == 1
    assert store.get_program_auto_fix_relation(plan_id) is None


def test_stale_parent_supersedes_plan_without_gitlab_write(tmp_path: Path) -> None:
    store, run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", "b" * 40, "https://parent")
    )

    result = _publisher(store, gitlab).process(plan_id=plan_id)

    assert result is not None and result.status == "superseded"
    assert gitlab.commit_payloads == []
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None and plan.state == "superseded"
    assert store.get_auto_fix_publication_intent(run_id).state == "superseded"
    assert store.list_effect_receipts(run_id, "autofix_plan")[0].state == "superseded"


def test_reconciler_recovers_missing_merge_request_after_confirmed_commit(
    tmp_path: Path,
) -> None:
    store, run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    gitlab.mr_error = RuntimeError("MR response lost")
    first = _publisher(store, gitlab).process(plan_id=plan_id)
    assert first is not None and first.status == "needs_reconciliation"
    gitlab.mr_error = None
    reconciler = AutoFixReconciler(
        store,
        gitlab,
        "42",
        run_lease_seconds=60,
        retry_delay_seconds=0,
        notifier=FanoutNotifier((Channel(),)),
    )

    absence = reconciler.process()
    results = reconciler.process()

    assert absence and absence[0].status == "deferred"
    assert results and results[0].status == "succeeded"
    assert len(gitlab.commit_payloads) == 1
    assert len(gitlab.mr_payloads) == 2
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None and plan.state == "published" and plan.plan_json is None
    assert store.get_program_auto_fix_relation(plan_id) is not None
    assert store.list_effect_receipts(run_id, "autofix_plan")[0].state == "published"


def test_reconciler_does_not_reissue_an_uncertain_unrecorded_commit(
    tmp_path: Path,
) -> None:
    store, run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    gitlab.commit_error = RuntimeError("commit response lost")
    first = _publisher(store, gitlab).process(plan_id=plan_id)
    assert first is not None and first.status == "needs_reconciliation"
    gitlab.commit_error = None
    reconciler = AutoFixReconciler(
        store,
        gitlab,
        "42",
        run_lease_seconds=60,
        retry_delay_seconds=0,
    )

    results = reconciler.process()

    assert results and results[0].status == "deferred"
    assert len(gitlab.commit_payloads) == 1
    assert gitlab.mr_payloads == []
    plan = store.get_auto_fix_publication_plan(run_id)
    assert plan is not None and plan.state == "needs_reconciliation"


def test_reconciler_records_relation_tree_drift_and_merge_sha(tmp_path: Path) -> None:
    store, _run_id, plan_id, branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    published = _publisher(store, gitlab).process(plan_id=plan_id)
    assert published is not None and published.status == "published"
    relation = store.get_program_auto_fix_relation(plan_id)
    assert relation is not None
    merged_sha = "b" * 40
    gitlab.child_lookup = MergeRequest(
        "42",
        9,
        "User renamed title",
        "review-bot",
        f"{branch}-changed",
        "feature",
        "e" * 40,
        "https://gitlab/merge_requests/9",
        description=f"context\n{relation.marker}",
        state="merged",
        merged_sha=merged_sha,
    )
    reconciler = AutoFixReconciler(
        store,
        gitlab,
        "42",
        run_lease_seconds=60,
        relation_refresh_seconds=300,
    )

    results = reconciler.process()

    assert results and results[0].status == "succeeded"
    current = store.get_program_auto_fix_relation(plan_id)
    assert current is not None and current.status == "merged"
    assert current.merged_sha == merged_sha
    assert current.divergence == "source_branch,tree"
    assert store.is_suppressed_parent_sha("42", 1, merged_sha) is True


def test_reconciler_rejects_missing_mr_recovery_after_parent_head_advances(
    tmp_path: Path,
) -> None:
    store, _run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    gitlab.mr_error = RuntimeError("MR response lost")
    first = _publisher(store, gitlab).process(plan_id=plan_id)
    assert first is not None and first.status == "needs_reconciliation"
    gitlab.mr_error = None
    gitlab.parent = MergeRequest(
        "42", 1, "Parent", "alice", "feature", "main", "b" * 40, "https://parent"
    )
    reconciler = AutoFixReconciler(
        store,
        gitlab,
        "42",
        run_lease_seconds=60,
        retry_delay_seconds=0,
    )

    absence = reconciler.process()
    blocked = reconciler.process()

    assert absence and absence[0].status == "deferred"
    assert blocked and blocked[0].status == "blocked"
    assert len(gitlab.mr_payloads) == 1


def test_relation_lookup_failure_preserves_webhook_divergence(tmp_path: Path) -> None:
    store, _run_id, plan_id, branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    assert _publisher(store, gitlab).process(plan_id=plan_id).status == "published"
    relation = store.get_program_auto_fix_relation(plan_id)
    assert relation is not None
    store.observe_program_auto_fix_webhook(
        "42",
        relation.auto_fix_mr_iid,
        status="opened",
        merged_sha=None,
        source_branch=f"{branch}-changed",
        target_branch=relation.target_branch,
        description=relation.marker,
        source_sha=relation.published_commit_sha,
    )
    reconciler = AutoFixReconciler(
        store,
        gitlab,
        "42",
        run_lease_seconds=60,
        retry_delay_seconds=0,
    )

    results = reconciler.process()

    assert results and results[0].status == "deferred"
    current = store.get_program_auto_fix_relation(plan_id)
    assert current is not None and current.divergence == "source_branch"


def test_relation_refresh_cannot_overwrite_concurrent_confirmed_merge(
    tmp_path: Path,
) -> None:
    store, _run_id, plan_id, _branch = _frozen_plan(tmp_path)
    gitlab = FakeGitLab(
        MergeRequest("42", 1, "Parent", "alice", "feature", "main", SHA, "https://parent")
    )
    assert _publisher(store, gitlab).process(plan_id=plan_id).status == "published"
    claimed = store.claim_program_auto_fix_relation(project_id="42")
    assert claimed is not None
    merged_sha = "b" * 40
    store.observe_program_auto_fix_webhook(
        "42",
        claimed.auto_fix_mr_iid,
        status="merged",
        merged_sha=merged_sha,
        source_branch=claimed.source_branch,
        target_branch=claimed.target_branch,
        description=claimed.marker,
    )

    persisted = store.finish_program_auto_fix_relation_reconciliation(
        claimed.relation_id,
        claimed.reconcile_claim_token,
        status="opened",
        merged_sha=None,
        source_branch=claimed.source_branch,
        target_branch=claimed.target_branch,
        description=claimed.marker,
        source_sha=claimed.published_commit_sha,
    )

    assert persisted is True
    current = store.get_program_auto_fix_relation(plan_id)
    assert current is not None and current.status == "merged"
    assert current.merged_sha == merged_sha

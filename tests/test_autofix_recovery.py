import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from review_agent_flow.autofix_planner import canonical_json
from review_agent_flow.autofix_recovery import (
    AutoFixRecoveryError,
    AutoFixRecoveryService,
    OperatorContext,
)
from review_agent_flow.gitlab import (
    CreatedMergeRequest,
    MergeRequest,
    RepositoryAction,
    RepositoryCommitObservation,
)
from review_agent_flow.store import SQLiteStore

PARENT_SHA = "a" * 40
COMMIT_SHA = "f" * 40
MERGE_SHA = "e" * 40
NONCE = "b" * 32


def _plan() -> tuple[str, str, str, str, tuple[dict[str, object], ...]]:
    actions = (
        {
            "action": "update",
            "file_path": "README.md",
            "content": "fixed\n",
            "execute_filemode": False,
        },
    )
    candidate = canonical_json({"version": 1, "base_sha": PARENT_SHA, "actions": list(actions)})
    candidate_sha256 = hashlib.sha256(candidate.encode()).hexdigest()
    branch = f"autofix/MR-1/sha-{PARENT_SHA}/intent-{NONCE}"
    marker_template = (
        f"<!-- reviewflow:v1;project=42;mr=1;sha={PARENT_SHA};effect=autofix;"
        f"intent={NONCE};plan_sha256={{plan_sha256}} -->"
    )
    payload = {
        "version": 1,
        "review_key": {"project_id": "42", "mr_iid": 1, "sha": PARENT_SHA},
        "intent": {"generation": 1, "nonce": NONCE},
        "proposal_summary": "Apply fix",
        "candidate_sha256": candidate_sha256,
        "actions": list(actions),
        "verification": {
            "candidate_sha256": candidate_sha256,
            "command_sha256": "d" * 64,
        },
        "branch": branch,
        "marker_template": marker_template,
        "commit_message": f"ReviewFlow Auto-fix for !1 at {PARENT_SHA}\n\n[skip ci]",
        "title": "Draft: [ReviewFlow Auto-Fix] Parent",
        "target_branch": "feature",
        "initial_readiness": "draft",
        "ci_trigger": "manual",
    }
    plan_json = canonical_json(payload)
    plan_sha256 = hashlib.sha256(plan_json.encode()).hexdigest()
    return (
        plan_json,
        plan_sha256,
        branch,
        marker_template.replace("{plan_sha256}", plan_sha256),
        actions,
    )


class FakeRecoveryGitLab:
    def __init__(self) -> None:
        plan_json, plan_sha256, branch, marker, actions = _plan()
        del plan_json, plan_sha256
        self.parent = MergeRequest(
            "42", 1, "Parent", "alice", "feature", "main", "c" * 40, "https://parent"
        )
        self.child = MergeRequest(
            "42",
            9,
            "Draft: [ReviewFlow Auto-Fix] Parent",
            "review-bot",
            branch,
            "feature",
            COMMIT_SHA,
            "https://child",
            draft=True,
            description=marker,
        )
        self.unique: CreatedMergeRequest | None = CreatedMergeRequest(
            "42",
            9,
            self.child.title,
            branch,
            "feature",
            self.child.web_url,
            description=marker,
            author_username="review-bot",
            draft=True,
        )
        self.observation = RepositoryCommitObservation(
            "42",
            COMMIT_SHA,
            (PARENT_SHA,),
            tuple(
                RepositoryAction(
                    str(action["action"]),
                    str(action["file_path"]),
                    str(action["content"]),
                    bool(action["execute_filemode"]),
                )
                for action in actions
            ),
        )

    def get_merge_request(self, _project_id: str, mr_iid: int) -> MergeRequest:
        return self.parent if mr_iid == 1 else self.child

    def find_merge_request_by_source_branch(
        self,
        _project_id: str,
        _source_branch: str,
        _target_branch: str,
        _marker: str | None = None,
    ) -> CreatedMergeRequest | None:
        return self.unique

    def observe_repository_commit(
        self, _project_id: str, _commit_sha: str
    ) -> RepositoryCommitObservation:
        return self.observation


def _context(recovery_id: str) -> OperatorContext:
    return OperatorContext(recovery_id, "restore lost Store", "operator", "host", "0.1.0")


def _recover(
    store: SQLiteStore,
    gitlab: FakeRecoveryGitLab,
    *,
    recovery_id: str = "recover-1",
):
    plan_json, plan_sha256, _branch, _marker, _actions = _plan()
    return AutoFixRecoveryService(store, gitlab, "review-bot").recover_relation(
        context=_context(recovery_id),
        project_id="42",
        parent_mr_iid=1,
        parent_sha=PARENT_SHA,
        intent_nonce=NONCE,
        plan_json=plan_json,
        plan_sha256=plan_sha256,
        child_mr_iid=9,
    )


def test_operator_recovery_validates_remote_plan_and_does_not_request_notification(
    tmp_path: Path,
) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    relation = _recover(store, FakeRecoveryGitLab())

    assert relation.parent_sha == PARENT_SHA
    assert relation.published_commit_sha == COMMIT_SHA
    assert store.get_auto_fix_notification_request(relation.relation_id) is None
    audit = store.list_operator_recoveries()
    assert len(audit) == 1
    assert audit[0].result == "succeeded"
    assert audit[0].relation_id == relation.relation_id
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_operator_recovery_allows_presentation_text_around_one_exact_marker(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    gitlab = FakeRecoveryGitLab()
    description = f"Operator context\n{gitlab.child.description}\nHuman notes"
    gitlab.child = replace(gitlab.child, description=description)
    assert gitlab.unique is not None
    gitlab.unique = replace(gitlab.unique, description=description)

    assert _recover(store, gitlab).relation_id > 0


def test_invalid_plan_attempt_is_rejected_and_audited(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    plan_json, plan_sha256, _branch, _marker, _actions = _plan()

    with pytest.raises(AutoFixRecoveryError, match="digest"):
        AutoFixRecoveryService(store, FakeRecoveryGitLab(), "review-bot").recover_relation(
            context=_context("invalid-plan"),
            project_id="42",
            parent_mr_iid=1,
            parent_sha=PARENT_SHA,
            intent_nonce=NONCE,
            plan_json=plan_json.replace("Apply fix", "Different fix"),
            plan_sha256=plan_sha256,
            child_mr_iid=9,
        )

    assert store.list_operator_recoveries()[0].result == "failed"


@pytest.mark.parametrize("conflict", ["bot", "unique", "parent", "actions"])
def test_operator_recovery_rejects_each_remote_authority_conflict_and_audits(
    tmp_path: Path, conflict: str
) -> None:
    store = SQLiteStore(tmp_path / f"{conflict}.db")
    gitlab = FakeRecoveryGitLab()
    if conflict == "bot":
        gitlab.child = replace(gitlab.child, author_username="alice")
    elif conflict == "unique":
        gitlab.unique = None
    elif conflict == "parent":
        gitlab.observation = replace(gitlab.observation, parent_ids=("c" * 40,))
    else:
        gitlab.observation = replace(
            gitlab.observation,
            actions=(RepositoryAction("delete", "README.md"),),
        )

    with pytest.raises(AutoFixRecoveryError):
        _recover(store, gitlab)

    assert store.get_program_auto_fix_relation_by_child("42", 9) is None
    audit = store.list_operator_recoveries()
    assert len(audit) == 1 and audit[0].result == "failed"
    assert audit[0].diagnostic


def test_notification_resend_is_separate_immutable_generation_and_idempotent(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    gitlab = FakeRecoveryGitLab()
    relation = _recover(store, gitlab)
    service = AutoFixRecoveryService(store, gitlab, "review-bot")

    first = service.resend_notification(
        context=_context("resend-1"),
        project_id="42",
        child_mr_iid=9,
        channels=(("alerts", "feishu"),),
    )
    repeated = service.resend_notification(
        context=_context("resend-1"),
        project_id="42",
        child_mr_iid=9,
        channels=(("alerts", "feishu"),),
    )
    second = service.resend_notification(
        context=_context("resend-2"),
        project_id="42",
        child_mr_iid=9,
        channels=(("alerts", "feishu"),),
    )

    assert first.request_id == repeated.request_id
    assert (first.generation, second.generation) == (1, 2)
    assert first.request_kind == second.request_kind == "operator_resend"
    assert store.get_auto_fix_notification_request(relation.relation_id, 1) == first
    assert store.get_auto_fix_notification_request(relation.relation_id, 2) == second
    with sqlite3.connect(tmp_path / "review.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_requests").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2
    assert [audit.action for audit in store.list_operator_recoveries()] == [
        "relation_recovery",
        "notification_resend",
        "notification_resend",
    ]


def test_notification_resend_replay_identity_survives_payload_purge(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    gitlab = FakeRecoveryGitLab()
    _recover(store, gitlab)
    service = AutoFixRecoveryService(store, gitlab, "review-bot")
    context = _context("resend-after-purge")
    original = service.resend_notification(
        context=context,
        project_id="42",
        child_mr_iid=9,
        channels=(("alerts", "feishu"),),
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE notification_outbox SET status = 'delivered', delivered_at = ?",
            ("2026-08-10T00:00:00+00:00",),
        )
    store.purge_sensitive_payloads(datetime(2030, 1, 1, tzinfo=UTC))

    repeated = service.resend_notification(
        context=context,
        project_id="42",
        child_mr_iid=9,
        channels=(("alerts", "feishu"),),
    )
    assert repeated == original

    gitlab.child = replace(gitlab.child, title="changed presentation")
    with pytest.raises(ValueError, match="immutable"):
        service.resend_notification(
            context=context,
            project_id="42",
            child_mr_iid=9,
            channels=(("alerts", "feishu"),),
        )


def test_recovery_id_cannot_be_rebound_to_different_operator_context(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    gitlab = FakeRecoveryGitLab()
    _recover(store, gitlab)
    plan_json, plan_sha256, _branch, _marker, _actions = _plan()

    with pytest.raises(ValueError, match="already bound"):
        AutoFixRecoveryService(store, gitlab, "review-bot").recover_relation(
            context=OperatorContext("recover-1", "different reason", "operator", "host", "0.1.0"),
            project_id="42",
            parent_mr_iid=1,
            parent_sha=PARENT_SHA,
            intent_nonce=NONCE,
            plan_json=plan_json,
            plan_sha256=plan_sha256,
            child_mr_iid=9,
        )

    assert len(store.list_operator_recoveries()) == 1


def test_notification_resend_rejects_current_identity_drift_and_audits(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    gitlab = FakeRecoveryGitLab()
    _recover(store, gitlab)
    gitlab.child = replace(gitlab.child, source_branch="hijacked")

    with pytest.raises(AutoFixRecoveryError):
        AutoFixRecoveryService(store, gitlab, "review-bot").resend_notification(
            context=_context("resend-failed"),
            project_id="42",
            child_mr_iid=9,
            channels=(("alerts", "feishu"),),
        )

    assert store.get_auto_fix_notification_request(1) is None
    assert store.list_operator_recoveries()[-1].result == "failed"

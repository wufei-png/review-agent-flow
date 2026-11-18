import sqlite3

import pytest
from fastapi.testclient import TestClient

from review_agent_flow.store import SQLiteStore
from review_agent_flow.web import StartupConfigurationError, create_app

SHA = "a" * 40
MERGED_SHA = "b" * 40
PIPELINE_SHA = "c" * 40


def _published_relation(
    store: SQLiteStore, *, child_iid: int = 9, target_branch: str = "feature/validation"
):
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next(project_id="42")
    assert claimed is not None
    intent = store.create_auto_fix_publication_intent(run.run_id, claimed.claim_token)
    assert intent is not None
    receipt = store.create_effect_receipt(
        run.run_id,
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
        "verification": {"candidate_sha256": "c" * 64, "command_sha256": "d" * 64},
        "branch": branch,
        "marker_template": (
            f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
            f"intent={intent.nonce};plan_sha256={{plan_sha256}} -->"
        ),
        "commit_message": "fix",
        "title": "Draft: [ReviewFlow Auto-Fix] Parent",
        "target_branch": target_branch,
        "initial_readiness": "draft",
        "ci_trigger": "manual",
    }
    plan = store.freeze_auto_fix_publication_plan(
        run.run_id, receipt.receipt_id, payload, claimed.claim_token
    )
    assert plan is not None
    assert store.finish(run.run_id, "succeeded", claim_token=claimed.claim_token)
    claimed_plan = store.claim_auto_fix_publication_plan(plan_id=plan.plan_id, project_id="42")
    assert claimed_plan is not None
    assert store.mark_auto_fix_write_started(
        plan.plan_id, claimed_plan.claim_token, operation="commit"
    )
    assert store.record_auto_fix_branch_commit(plan.plan_id, claimed_plan.claim_token, "f" * 40)
    assert store.mark_auto_fix_write_started(
        plan.plan_id, claimed_plan.claim_token, operation="merge_request"
    )
    assert store.record_auto_fix_merge_request(plan.plan_id, claimed_plan.claim_token, child_iid)
    completed = store.complete_auto_fix_publication(
        plan.plan_id,
        claimed_plan.claim_token,
        auto_fix_project_id="42",
        auto_fix_mr_iid=child_iid,
        source_branch=branch,
        target_branch=target_branch,
        marker=plan.marker,
        title="Auto-fix",
        body="ready",
        url="https://child",
        recipients=("alice",),
        channels=(),
    )
    assert completed is not None
    return completed[0]


def test_project_status_exposes_published_plan_and_relation_without_private_payload(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    relation = _published_relation(store)
    snapshot = store.get_project_bound_review_run_status("42", relation.run_id)
    assert snapshot is not None
    assert snapshot["effects"]["auto_fix_plan"]["state"] == "published"
    assert snapshot["effects"]["auto_fix_plan"]["external_mr_iid"] == 9
    assert snapshot["effects"]["auto_fix_relation"]["auto_fix_mr_iid"] == 9
    assert snapshot["effects"]["auto_fix_relation"]["next_action"] == "none"
    assert store.get_project_bound_review_run_status("99", relation.run_id) is None


def test_ops_status_counts_plan_reconciliation_block_and_relation_divergence(tmp_path) -> None:
    database = tmp_path / "review.db"
    store = SQLiteStore(database)
    relation = _published_relation(store)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE auto_fix_publication_plans SET state = 'running', "
            "write_phase = 'commit_write_started', lease_until = ? WHERE id = ?",
            ("9999-01-01T00:00:00+00:00", relation.plan_id),
        )
    active = store.get_project_bound_review_run_status("42", relation.run_id)
    assert active is not None
    assert active["effects"]["auto_fix_plan"]["next_action"] == "wait_for_worker"
    assert store.get_project_ops_status("42")["auto_fix"]["reconciliation"] == 0
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE auto_fix_publication_plans SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", relation.plan_id),
        )
    expired = store.get_project_bound_review_run_status("42", relation.run_id)
    assert expired is not None
    assert expired["effects"]["auto_fix_plan"]["next_action"] == "wait_for_reconciliation"
    assert store.get_project_ops_status("42")["auto_fix"]["reconciliation"] == 1
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE auto_fix_publication_plans SET state = 'needs_reconciliation' WHERE id = ?",
            (relation.plan_id,),
        )
        connection.execute(
            "UPDATE auto_fix_relations SET divergence = 'source_branch' WHERE id = ?",
            (relation.relation_id,),
        )
    summary = store.get_project_ops_status("42")
    assert summary["auto_fix"]["reconciliation"] == 1
    assert summary["auto_fix"]["relation_divergence"] == 1
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE auto_fix_publication_plans SET state = 'blocked' WHERE id = ?",
            (relation.plan_id,),
        )
    summary = store.get_project_ops_status("42")
    assert summary["auto_fix"]["blocked"] == 1
    assert summary["auto_fix"]["reconciliation"] == 0


def test_webhook_configuration_is_required(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    with pytest.raises(RuntimeError, match="GITLAB_WEBHOOK_SECRET"):
        create_app(store, project_id="42")
    with pytest.raises(RuntimeError, match="REVIEW_AGENT_FLOW_PROJECT_ID"):
        create_app(store, webhook_secret="secret")


def test_gitlab_webhook_is_idempotent_and_secret_checked(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    payload = {
        "project": {"id": 42},
        "object_attributes": {
            "action": "update",
            "iid": 1,
            "title": "Improve validation",
            "source_branch": "feature/validation",
            "target_branch": "main",
            "last_commit": {"id": SHA},
            "work_in_progress": False,
        },
    }
    headers = {"X-Gitlab-Token": "secret", "X-Gitlab-Event": "Merge Request Hook"}
    first = client.post("/webhooks/gitlab", json=payload, headers=headers)
    duplicate = client.post("/webhooks/gitlab", json=payload, headers=headers)
    unauthorized_sha = "d" * 40
    unauthorized_payload = {
        **payload,
        "object_attributes": {
            **payload["object_attributes"],
            "iid": 2,
            "last_commit": {"id": unauthorized_sha},
        },
    }
    unauthorized = client.post(
        "/webhooks/gitlab",
        json=unauthorized_payload,
        headers={"X-Gitlab-Token": "bad"},
    )
    assert first.status_code == 200
    assert duplicate.json()["run_id"] == first.json()["run_id"]
    assert unauthorized.status_code == 401
    assert store.wake_run("42", 2, unauthorized_sha) is None


def test_auto_fix_merge_marks_only_the_resulting_sha(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    relation = _published_relation(store)
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    payload = {
        "project": {"id": 42},
        "object_attributes": {
            "action": "merge",
            "iid": 9,
            "title": "[ReviewFlow Auto-Fix] Improve validation",
            "source_branch": relation.source_branch,
            "target_branch": "feature/validation",
            "last_commit": {"id": SHA},
            "merge_commit_sha": MERGED_SHA,
            "description": relation.marker,
            "work_in_progress": False,
        },
    }
    response = client.post("/webhooks/gitlab", json=payload, headers={"X-Gitlab-Token": "secret"})
    assert response.status_code == 200
    assert response.json()["kind"] == "autofix_merged"
    assert store.is_suppressed_parent_sha("42", 1, MERGED_SHA) is True


def test_draft_child_webhook_wakes_existing_relation_without_changing_identity(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    relation = _published_relation(store)
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))

    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {
                "action": "update",
                "iid": relation.auto_fix_mr_iid,
                "title": "Draft: renamed",
                "source_branch": relation.source_branch,
                "target_branch": relation.target_branch,
                "last_commit": {"id": relation.published_commit_sha},
                "description": f"context\n{relation.marker}",
                "draft": True,
            },
        },
        headers={"X-Gitlab-Token": "secret"},
    )

    assert response.json()["accepted"] is True
    assert response.json()["kind"] == "autofix"
    current = store.get_program_auto_fix_relation(relation.plan_id)
    assert current is not None and current.reconcile_requested is True
    assert current.divergence is None


def test_closing_draft_child_records_closed_before_draft_presentation(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    relation = _published_relation(store)
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))

    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {
                "action": "close",
                "iid": relation.auto_fix_mr_iid,
                "title": "Draft: closed",
                "source_branch": relation.source_branch,
                "target_branch": relation.target_branch,
                "last_commit": {"id": relation.published_commit_sha},
                "description": relation.marker,
                "draft": True,
            },
        },
        headers={"X-Gitlab-Token": "secret"},
    )

    assert response.json()["kind"] == "autofix"
    current = store.get_program_auto_fix_relation(relation.plan_id)
    assert current is not None and current.status == "closed"


def test_auto_fix_shaped_branch_without_relation_never_creates_review_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {
                "action": "update",
                "iid": 7,
                "title": "[ReviewFlow Auto-Fix] spoof",
                "source_branch": f"autofix/MR-1/sha-{SHA}/intent-{'d' * 32}",
                "target_branch": "feature/validation",
                "last_commit": {"id": SHA},
            },
        },
        headers={"X-Gitlab-Token": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["kind"] == "autofix"
    assert store.claim_next() is None


def test_auto_fix_marker_cannot_establish_relation_without_local_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    nonce = "d" * 32
    branch = f"autofix/MR-1/sha-{SHA}/intent-{nonce}"
    marker = (
        f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
        f"intent={nonce};plan_sha256={'e' * 64} -->"
    )
    payload = {
        "project": {"id": 42},
        "object_attributes": {
            "action": "update",
            "iid": 9,
            "title": "[ReviewFlow Auto-Fix] Improve validation",
            "source_branch": branch,
            "target_branch": "feature/validation",
            "last_commit": {"id": MERGED_SHA},
            "description": marker,
        },
    }
    headers = {"X-Gitlab-Token": "secret"}
    response = client.post("/webhooks/gitlab", json=payload, headers=headers)

    assert response.json()["accepted"] is False
    assert response.json()["kind"] == "autofix"
    assert "run_id" not in response.json()
    assert store.get_program_auto_fix_relation_by_child("42", 9) is None


def test_auto_fix_marker_on_arbitrary_branch_cannot_create_review_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    nonce = "d" * 32
    marker = (
        f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
        f"intent={nonce};plan_sha256={'e' * 64} -->"
    )

    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {
                "action": "update",
                "iid": 10,
                "title": "Spoof",
                "source_branch": "ordinary-looking-branch",
                "target_branch": "feature/validation",
                "last_commit": {"id": SHA},
                "description": marker,
            },
        },
        headers={"X-Gitlab-Token": "secret"},
    )

    assert response.json()["accepted"] is False
    assert response.json()["kind"] == "autofix"
    assert store.claim_next() is None


def test_pipeline_hook_wakes_existing_deferred_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, PIPELINE_SHA)
    claimed = store.claim_next()
    assert claimed is not None
    assert store.requeue(claimed.run_id, "waiting", 3600, claimed.claim_token)
    client = TestClient(
        create_app(store, webhook_secret="secret", project_id="42", pipeline_gate=True)
    )
    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {"status": "success", "sha": PIPELINE_SHA},
            "merge_request": {"iid": 1, "source_branch": "feature/one"},
        },
        headers={"X-Gitlab-Event": "Pipeline Hook", "X-Gitlab-Token": "secret"},
    )
    assert response.json()["status"] == "woken"
    assert store.claim_next().run_id == run.run_id


def test_pipeline_hook_does_not_create_run_when_gate_is_disabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(
        create_app(store, webhook_secret="secret", project_id="42", pipeline_gate=False)
    )
    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {"status": "success", "sha": PIPELINE_SHA},
            "merge_request": {"iid": 1, "source_branch": "feature/one"},
        },
        headers={"X-Gitlab-Event": "Pipeline Hook", "X-Gitlab-Token": "secret"},
    )
    assert response.json()["accepted"] is False
    assert store.claim_next() is None


def test_pipeline_hook_accepts_each_ready_merge_request_in_a_list(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, PIPELINE_SHA)
    store.enqueue("42", 3, PIPELINE_SHA)
    client = TestClient(
        create_app(store, webhook_secret="secret", project_id="42", pipeline_gate=True)
    )
    payload = {
        "project": {"id": 42},
        "object_attributes": {"status": "success", "sha": PIPELINE_SHA},
        "merge_request": [
            {"iid": 1, "title": "Ready", "source_branch": "feature/one"},
            {"iid": 2, "title": "Draft", "draft": True, "source_branch": "feature/two"},
            {"iid": 3, "title": "Ready too", "source_branch": "feature/three"},
        ],
    }
    response = client.post(
        "/webhooks/gitlab",
        json=payload,
        headers={"X-Gitlab-Event": "Pipeline Hook", "X-Gitlab-Token": "secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert [item["status"] for item in body["decisions"] if item["accepted"]] == [
        "woken",
        "woken",
    ]
    assert body["decisions"][1]["accepted"] is False


def test_create_app_requires_secret_and_bound_project(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("GITLAB_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("REVIEW_AGENT_FLOW_PROJECT_ID", raising=False)
    store = SQLiteStore(tmp_path / "review.db")
    with pytest.raises(StartupConfigurationError, match="GITLAB_WEBHOOK_SECRET"):
        create_app(store, project_id="42")

    with pytest.raises(StartupConfigurationError, match="REVIEW_AGENT_FLOW_PROJECT_ID"):
        create_app(store, webhook_secret="secret")


def test_explicit_empty_secret_does_not_enable_anonymous_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "environment-secret")
    store = SQLiteStore(tmp_path / "review.db")
    with pytest.raises(StartupConfigurationError, match="GITLAB_WEBHOOK_SECRET"):
        create_app(store, webhook_secret="", project_id="42")


def test_webhook_rejects_a_different_project_without_enqueueing(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 99},
            "object_attributes": {
                "action": "update",
                "iid": 1,
                "last_commit": {"id": SHA},
                "work_in_progress": False,
            },
        },
        headers={"X-Gitlab-Token": "secret", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert response.status_code == 403
    assert store.claim_next() is None


def test_webhook_rejects_project_mismatch_for_ignored_event(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 99},
            "object_attributes": {
                "action": "close",
                "iid": 1,
                "last_commit": {"id": SHA},
                "work_in_progress": False,
            },
        },
        headers={"X-Gitlab-Token": "secret", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert response.status_code == 403
    assert store.claim_next() is None


def test_webhook_does_not_enqueue_a_closed_parent_merge_request(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))

    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {
                "action": "close",
                "iid": 1,
                "last_commit": {"id": SHA},
                "work_in_progress": False,
            },
        },
        headers={"X-Gitlab-Token": "secret", "X-Gitlab-Event": "Merge Request Hook"},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["kind"] == "close"
    assert store.claim_next() is None


def test_webhook_rejects_invalid_sha_without_enqueueing(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {
                "action": "update",
                "iid": 1,
                "last_commit": {"id": "abc"},
                "merge_commit_sha": MERGED_SHA,
            },
        },
        headers={"X-Gitlab-Token": "secret", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert store.claim_next() is None


def test_webhook_does_not_enqueue_merge_commit_as_normal_head(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {
                "action": "update",
                "iid": 1,
                "merge_commit_sha": MERGED_SHA,
            },
        },
        headers={"X-Gitlab-Token": "secret", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert store.claim_next() is None

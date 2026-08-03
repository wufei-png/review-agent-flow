import pytest
from fastapi.testclient import TestClient

from review_agent_flow.store import SQLiteStore, auto_fix_intent_marker
from review_agent_flow.web import StartupConfigurationError, create_app

SHA = "a" * 40
MERGED_SHA = "b" * 40
PIPELINE_SHA = "c" * 40


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
    unauthorized = client.post("/webhooks/gitlab", json=payload, headers={"X-Gitlab-Token": "bad"})
    assert first.status_code == 200
    assert duplicate.json()["run_id"] == first.json()["run_id"]
    assert unauthorized.status_code == 401


def test_auto_fix_merge_marks_only_the_resulting_sha(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    store.record_auto_fix("42", 1, SHA, run.run_id, "42", 9)
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    payload = {
        "project": {"id": 42},
        "object_attributes": {
            "action": "merge",
            "iid": 9,
            "title": "[ReviewFlow Auto-Fix] Improve validation",
            "source_branch": "autofix/MR-1/run-1",
            "target_branch": "feature/validation",
            "last_commit": {"id": SHA},
            "merge_commit_sha": MERGED_SHA,
            "work_in_progress": False,
        },
    }
    response = client.post(
        "/webhooks/gitlab", json=payload, headers={"X-Gitlab-Token": "secret"}
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "autofix_merged"
    assert store.is_suppressed_parent_sha("42", 1, MERGED_SHA) is True


def test_auto_fix_looking_title_and_branch_are_normal_without_intent(tmp_path) -> None:
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
                "source_branch": "autofix/MR-1/run-999",
                "target_branch": "feature/validation",
                "last_commit": {"id": SHA},
            },
        },
        headers={"X-Gitlab-Token": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "normal"


def test_auto_fix_intent_requires_description_marker(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    run = store.enqueue("42", 1, SHA)
    store.create_auto_fix_intent(
        "42",
        1,
        SHA,
        run.run_id,
        "autofix/MR-1/run-1",
        "feature/validation",
        "[ReviewFlow Auto-Fix] Improve validation",
    )
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    payload = {
        "project": {"id": 42},
        "object_attributes": {
            "action": "update",
            "iid": 9,
            "title": "[ReviewFlow Auto-Fix] Improve validation",
            "source_branch": "autofix/MR-1/run-1",
            "target_branch": "feature/validation",
            "last_commit": {"id": MERGED_SHA},
            "description": "marker omitted",
        },
    }
    headers = {"X-Gitlab-Token": "secret"}
    unmarked = client.post("/webhooks/gitlab", json=payload, headers=headers)
    marked_payload = {
        **payload,
        "object_attributes": {
            **payload["object_attributes"],
            "description": auto_fix_intent_marker(run.run_id, SHA),
        },
    }
    marked = client.post("/webhooks/gitlab", json=marked_payload, headers=headers)

    assert unmarked.json()["kind"] == "normal"
    assert marked.json()["kind"] == "autofix"


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

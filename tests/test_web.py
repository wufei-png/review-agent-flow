from fastapi.testclient import TestClient

from review_agent_flow.store import SQLiteStore
from review_agent_flow.web import create_app


def test_gitlab_webhook_is_idempotent_and_secret_checked(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store, webhook_secret="secret"))
    payload = {
        "project": {"id": 42},
        "object_attributes": {
            "action": "update",
            "iid": 1,
            "title": "Improve validation",
            "source_branch": "feature/validation",
            "target_branch": "main",
            "last_commit": {"id": "abc"},
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
    run = store.enqueue("42", 1, "parent-sha")
    store.record_auto_fix("42", 1, "parent-sha", run.run_id, "42", 9)
    client = TestClient(create_app(store))
    payload = {
        "project": {"id": 42},
        "object_attributes": {
            "action": "merge",
            "iid": 9,
            "title": "[ReviewFlow Auto-Fix] Improve validation",
            "source_branch": "autofix/MR-1/run-1",
            "target_branch": "feature/validation",
            "last_commit": {"id": "auto-fix-head"},
            "merge_commit_sha": "merged-parent-sha",
            "work_in_progress": False,
        },
    }
    response = client.post("/webhooks/gitlab", json=payload)
    assert response.status_code == 200
    assert response.json()["kind"] == "autofix_merged"
    assert store.is_suppressed_parent_sha("42", 1, "merged-parent-sha") is True


def test_pipeline_hook_accepts_each_ready_merge_request_in_a_list(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    client = TestClient(create_app(store))
    payload = {
        "project": {"id": 42},
        "object_attributes": {"status": "success", "sha": "abc"},
        "merge_request": [
            {"iid": 1, "title": "Ready", "source_branch": "feature/one"},
            {"iid": 2, "title": "Draft", "draft": True, "source_branch": "feature/two"},
            {"iid": 3, "title": "Ready too", "source_branch": "feature/three"},
        ],
    }
    response = client.post(
        "/webhooks/gitlab",
        json=payload,
        headers={"X-Gitlab-Event": "Pipeline Hook"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert [item["run_id"] for item in body["decisions"] if item["accepted"]] == [1, 2]
    assert body["decisions"][1]["accepted"] is False

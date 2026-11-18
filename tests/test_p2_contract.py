import json
import logging

from fastapi.testclient import TestClient

from review_agent_flow import cli
from review_agent_flow.store import SQLiteStore
from review_agent_flow.structured_logging import log_event
from review_agent_flow.web import LiveProbeResult, create_app

SHA = "a" * 40
OTHER_SHA = "b" * 40


def test_health_readiness_and_explicit_probe_are_separate(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    calls: list[str] = []

    def live_probe(project_id: str) -> LiveProbeResult:
        calls.append(project_id)
        return LiveProbeResult(True)

    client = TestClient(
        create_app(store, webhook_secret="secret", project_id="42", live_probe=live_probe)
    )

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json() == {"status": "alive"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["checks"]["sqlite"]["status"] == "ok"
    assert calls == []

    assert client.get("/probes/gitlab").status_code == 401
    assert (
        client.get(
            "/probes/gitlab",
            headers={"X-ReviewFlow-Internal-Token": "wrong"},
        ).status_code
        == 401
    )
    assert calls == []
    live = client.get(
        "/probes/gitlab",
        headers={"X-ReviewFlow-Internal-Token": "secret"},
    )
    assert live.status_code == 200
    assert live.json() == {"status": "ready", "probe": "gitlab"}
    assert calls == ["42"]


def test_unconfigured_live_probe_and_failed_storage_never_look_ready(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    runtime_token = "opaque-readiness-runtime-token"
    client = TestClient(create_app(store, webhook_secret=runtime_token, project_id="42"))
    probe = client.get("/probes/gitlab")
    assert probe.status_code == 401
    assert (
        client.get(
            "/probes/gitlab",
            headers={"X-ReviewFlow-Internal-Token": "wrong"},
        ).status_code
        == 401
    )
    probe = client.get(
        "/probes/gitlab",
        headers={"X-ReviewFlow-Internal-Token": runtime_token},
    )
    assert probe.status_code == 503
    assert probe.json() == {
        "status": "not_ready",
        "probe": "gitlab",
        "reason": "not_configured",
    }

    store.readiness_probe = lambda: {
        "status": "not_ready",
        "diagnostic": f"sqlite failed with {runtime_token}",
    }
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["checks"]["sqlite"]["status"] == "not_ready"
    assert runtime_token not in ready.text


def test_project_bound_status_is_read_only_and_credential_free(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db", redaction_secrets=("gitlab-token",))
    run = store.enqueue("42", 1, SHA)
    claimed = store.claim_next(project_id="42")
    assert claimed is not None
    receipt = store.create_effect_receipt(
        run.run_id,
        "agent_execution",
        "review-run:1:agent",
        state="running",
        detail="Agent started",
    )
    store.finish(
        run.run_id,
        "failed",
        "agent failed with gitlab-token",
        claim_token=claimed.claim_token,
        failure_class="agent_exit",
    )
    other = store.enqueue("99", 2, OTHER_SHA)
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))

    query_calls: list[tuple[str, int]] = []
    original_query = store.get_project_bound_review_run_status

    def query(project_id: str, run_id: int):
        query_calls.append((project_id, run_id))
        return original_query(project_id, run_id)

    store.get_project_bound_review_run_status = query
    assert client.get(f"/review-runs/{run.run_id}").status_code == 401
    assert (
        client.get(
            f"/review-runs/{run.run_id}",
            headers={"X-ReviewFlow-Internal-Token": "wrong"},
        ).status_code
        == 401
    )
    assert query_calls == []
    response = client.get(
        f"/review-runs/{run.run_id}",
        headers={"X-ReviewFlow-Internal-Token": "secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["attempt"] == 1
    assert body["failure"]["failure_class"] == "agent_exit"
    assert body["effect_receipts"][0]["receipt_id"] == receipt.receipt_id
    assert "claim_token" not in response.text
    assert claimed.claim_token not in response.text
    assert "gitlab-token" not in response.text

    project_scoped = client.get(
        f"/projects/42/review-runs/{run.run_id}",
        headers={"X-ReviewFlow-Internal-Token": "secret"},
    )
    assert project_scoped.status_code == 200
    assert (
        client.get(
            f"/review-runs/{other.run_id}",
            headers={"X-ReviewFlow-Internal-Token": "secret"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/projects/99/review-runs/{run.run_id}",
            headers={"X-ReviewFlow-Internal-Token": "secret"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/review-runs/999999",
            headers={"X-ReviewFlow-Internal-Token": "secret"},
        ).status_code
        == 404
    )


def test_status_cli_is_read_only_and_safe_for_unknown_runs(tmp_path, capsys) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    store.enqueue("42", 1, SHA)
    assert (
        cli.main(
            [
                "status",
                "--project-id",
                "42",
                "--db",
                str(tmp_path / "review.db"),
                "--run-id",
                "1",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["project_id"] == "42"
    assert (
        cli.main(
            [
                "status",
                "--project-id",
                "42",
                "--db",
                str(tmp_path / "review.db"),
                "--run-id",
                "99",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {"status": "not_found"}


def test_webhook_and_worker_events_are_json_and_redacted(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    store = SQLiteStore(tmp_path / "review.db", redaction_secrets=("gitlab-token",))
    client = TestClient(create_app(store, webhook_secret="secret", project_id="42"))
    response = client.post(
        "/webhooks/gitlab",
        json={
            "project": {"id": 42},
            "object_attributes": {
                "action": "update",
                "iid": 1,
                "last_commit": {"id": SHA},
            },
        },
        headers={
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event": "Merge Request Hook",
            "X-Request-ID": "correlation-1",
        },
    )
    assert response.status_code == 200
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name.startswith("review_agent_flow")
    ]
    decision = next(record for record in records if record["event"] == "webhook.decision")
    assert decision["correlation_id"] == "correlation-1"
    assert decision["run_id"] == 1
    assert decision["project_id"] == "42"
    assert decision["mr_iid"] == 1
    assert decision["status"] == "pending"
    assert all("payload" not in record for record in records)
    assert all("secret" not in record.get("diagnostic", "") for record in records)


def test_structured_logger_redacts_explicit_runtime_secrets(caplog) -> None:
    caplog.set_level(logging.INFO)
    runtime_token = "opaque-runtime-token-without-a-pattern"
    log_event(
        logging.getLogger("review_agent_flow.test_runtime_secret"),
        logging.INFO,
        event="runtime_secret_test",
        status="failed",
        correlation_id=runtime_token,
        diagnostic=f"provider failed with {runtime_token}",
        detail=runtime_token,
        secrets=(runtime_token,),
    )
    record = next(
        record
        for record in caplog.records
        if record.name == "review_agent_flow.test_runtime_secret"
    )
    assert runtime_token not in record.getMessage()
    body = json.loads(record.getMessage())
    assert body["correlation_id"] == "[REDACTED]"
    assert body["diagnostic"] == "provider failed with [REDACTED]"
    assert body["detail"] == "[REDACTED]"

from pathlib import Path

from review_agent_flow.operator_config import MAX_BACKEND_TIMEOUT_SECONDS

ROOT = Path(__file__).parents[1]


def test_systemd_reference_separates_api_and_worker_authority() -> None:
    api = (ROOT / "deploy/systemd/review-agent-flow-api.service").read_text()
    worker = (ROOT / "deploy/systemd/review-agent-flow-worker.service").read_text()

    assert "User=reviewflow-api\n" in api
    assert "User=reviewflow-worker\n" in worker
    assert "Group=reviewflow-shared\n" in api
    assert "Group=reviewflow-shared\n" in worker
    assert "UMask=0007\n" in api
    assert "UMask=0007\n" in worker
    assert "ReadWritePaths=/var/lib/review-agent-flow/shared\n" in api
    assert (
        "ReadWritePaths=/var/lib/review-agent-flow/shared /var/lib/review-agent-flow-worker\n"
    ) in worker
    assert "/var/lib/review-agent-flow-worker" not in api


def test_systemd_worker_stop_budget_covers_agent_and_verifier() -> None:
    worker = (ROOT / "deploy/systemd/review-agent-flow-worker.service").read_text()
    expected_seconds = MAX_BACKEND_TIMEOUT_SECONDS * 2 + 100

    assert f"TimeoutStopSec={expected_seconds}s\n" in worker

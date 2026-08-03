import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from review_agent_flow import cli
from review_agent_flow.policy import ReviewPolicy
from review_agent_flow.worker import WorkerResult


def test_worker_resolves_relative_policy_from_repository(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    captured = {}

    def fake_load_policy(path):
        captured["policy_path"] = path
        return ReviewPolicy()

    class FakeWorker:
        def __init__(self, **kwargs):
            captured["worker"] = kwargs

        def process_one(self):
            return None

        def run_forever(self, poll_interval):
            raise AssertionError("test must use --once")

    monkeypatch.setattr(cli, "load_policy", fake_load_policy)
    monkeypatch.setattr(cli, "SQLiteStore", lambda path: object())
    monkeypatch.setattr(cli, "PythonGitLabClient", lambda url, token: object())
    monkeypatch.setattr(cli, "GitWorktreeManager", lambda repository, root: object())
    monkeypatch.setattr(cli, "ReviewWorker", FakeWorker)
    monkeypatch.setattr(cli, "notifiers_from_policy", lambda channels: object())

    args = SimpleNamespace(
        gitlab_url="https://gitlab",
        gitlab_token="token",
        project_id="42",
        repository=str(repository),
        policy=None,
        db=None,
        worktrees=None,
        agent_timeout=1800,
        fetch_timeout=60,
        poll_interval=5.0,
        once=True,
    )
    assert cli._run_worker(args) == 0
    assert captured["policy_path"] == repository / ".gitlab/reviewflow.yml"
    assert captured["worker"]["project_id"] == "42"


def test_worker_once_emits_json_run_status(monkeypatch, tmp_path: Path, capsys) -> None:
    repository = tmp_path / "project"
    repository.mkdir()

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def process_one(self):
            return WorkerResult(7, "succeeded", detail="done")

        def run_forever(self, poll_interval):
            raise AssertionError("test must use --once")

    monkeypatch.setattr(cli, "load_policy", lambda path: ReviewPolicy())
    monkeypatch.setattr(cli, "SQLiteStore", lambda path: object())
    monkeypatch.setattr(cli, "PythonGitLabClient", lambda url, token: object())
    monkeypatch.setattr(cli, "GitWorktreeManager", lambda repository, root: object())
    monkeypatch.setattr(cli, "ReviewWorker", FakeWorker)
    monkeypatch.setattr(cli, "notifiers_from_policy", lambda channels: object())

    args = SimpleNamespace(
        gitlab_url="https://gitlab",
        gitlab_token="token",
        project_id="42",
        repository=str(repository),
        policy=None,
        db=None,
        worktrees=None,
        agent_timeout=1800,
        poll_interval=5.0,
        once=True,
    )

    assert cli._run_worker(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "agent_result": None,
        "detail": "done",
        "run_id": 7,
        "status": "succeeded",
    }


def test_worker_entry_requires_a_bound_project(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    args = SimpleNamespace(
        gitlab_url="https://gitlab",
        gitlab_token="token",
        project_id=None,
        repository=str(repository),
        policy=None,
        db=None,
        worktrees=None,
        agent_timeout=1800,
        poll_interval=5.0,
        once=True,
    )
    monkeypatch.delenv("REVIEW_AGENT_FLOW_PROJECT_ID", raising=False)
    with pytest.raises(SystemExit, match="REVIEW_AGENT_FLOW_PROJECT_ID"):
        cli._run_worker(args)


def test_status_does_not_create_missing_database(tmp_path: Path, capsys) -> None:
    database = tmp_path / "missing-review.db"
    args = SimpleNamespace(project_id="42", run_id=1, db=str(database))

    assert cli._run_status(args) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "not_found"}
    assert not database.exists()

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from review_agent_flow import cli
from review_agent_flow.policy import ReviewPolicy
from review_agent_flow.worker import WorkerResult


class FakeNotificationDispatcher:
    def __init__(self) -> None:
        self.batch_calls = 0

    def process_channel_batch(self) -> int:
        self.batch_calls += 1
        return 0


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
            self.notification_dispatcher = FakeNotificationDispatcher()
            captured["notification_dispatcher"] = self.notification_dispatcher

        def process_one(self):
            return None

        def reconcile_auto_fixes(self, limit):
            return None

        def process_worktree_cleanup(self, limit):
            return 0

        def run_forever(self, poll_interval):
            raise AssertionError("test must use --once")

    monkeypatch.setattr(cli, "load_policy", fake_load_policy)
    monkeypatch.setattr(cli, "SQLiteStore", lambda path: object())
    monkeypatch.setenv("GITLAB_REVIEW_BOT_USERNAME", "review-bot")
    monkeypatch.setattr(cli, "PythonGitLabClient", lambda url, token, username: object())
    monkeypatch.setattr(cli, "GitWorktreeManager", lambda repository, root: object())
    monkeypatch.setattr(cli, "ReviewWorker", FakeWorker)
    monkeypatch.setattr(cli, "notifiers_from_policy", lambda channels: object())
    monkeypatch.setattr(
        cli,
        "load_operator_config",
        lambda path: SimpleNamespace(
            autofix_publication=SimpleNamespace(enabled=False, allowed_projects=frozenset())
        ),
    )
    monkeypatch.setattr(cli, "resolve_backend", lambda *args, **kwargs: None)

    args = SimpleNamespace(
        gitlab_url="https://gitlab",
        gitlab_token="token",
        project_id="42",
        repository=str(repository),
        policy=None,
        operator_config=str(tmp_path / "operator.yml"),
        db=None,
        worktrees=None,
        fetch_timeout=60,
        poll_interval=5.0,
        once=True,
    )
    assert cli._run_worker(args) == 0
    assert captured["policy_path"] == repository / ".gitlab/reviewflow.yml"
    assert captured["worker"]["project_id"] == "42"
    assert captured["notification_dispatcher"].batch_calls == 1


def test_worker_once_emits_json_run_status(monkeypatch, tmp_path: Path, capsys) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            self.notification_dispatcher = FakeNotificationDispatcher()
            captured["notification_dispatcher"] = self.notification_dispatcher

        def process_one(self):
            return WorkerResult(7, "succeeded", detail="done")

        def reconcile_auto_fixes(self, limit):
            return None

        def process_worktree_cleanup(self, limit):
            return 0

        def run_forever(self, poll_interval):
            raise AssertionError("test must use --once")

    monkeypatch.setattr(cli, "load_policy", lambda path: ReviewPolicy())
    monkeypatch.setattr(cli, "SQLiteStore", lambda path: object())
    monkeypatch.setenv("GITLAB_REVIEW_BOT_USERNAME", "review-bot")
    monkeypatch.setattr(cli, "PythonGitLabClient", lambda url, token, username: object())
    monkeypatch.setattr(cli, "GitWorktreeManager", lambda repository, root: object())
    monkeypatch.setattr(cli, "ReviewWorker", FakeWorker)
    monkeypatch.setattr(cli, "notifiers_from_policy", lambda channels: object())
    monkeypatch.setattr(
        cli,
        "load_operator_config",
        lambda path: SimpleNamespace(
            autofix_publication=SimpleNamespace(enabled=False, allowed_projects=frozenset())
        ),
    )
    monkeypatch.setattr(cli, "resolve_backend", lambda *args, **kwargs: None)

    args = SimpleNamespace(
        gitlab_url="https://gitlab",
        gitlab_token="token",
        project_id="42",
        repository=str(repository),
        policy=None,
        operator_config=str(tmp_path / "operator.yml"),
        db=None,
        worktrees=None,
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
    assert captured["notification_dispatcher"].batch_calls == 1


def test_worker_entry_requires_a_bound_project(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    args = SimpleNamespace(
        gitlab_url="https://gitlab",
        gitlab_token="token",
        project_id=None,
        repository=str(repository),
        policy=None,
        operator_config=None,
        db=None,
        worktrees=None,
        poll_interval=5.0,
        once=True,
    )
    monkeypatch.delenv("REVIEW_AGENT_FLOW_PROJECT_ID", raising=False)
    with pytest.raises(SystemExit, match="REVIEW_AGENT_FLOW_PROJECT_ID"):
        cli._run_worker(args)


def test_worker_requires_external_operator_configuration(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    args = SimpleNamespace(
        gitlab_url="https://gitlab",
        gitlab_token="token",
        project_id="42",
        repository=str(repository),
        policy=None,
        operator_config=None,
        db=None,
        worktrees=None,
        fetch_timeout=60,
        poll_interval=5.0,
        once=True,
    )
    monkeypatch.delenv("REVIEW_AGENT_FLOW_OPERATOR_CONFIG", raising=False)
    with pytest.raises(SystemExit, match="REVIEW_AGENT_FLOW_OPERATOR_CONFIG"):
        cli._run_worker(args)

    inside = repository / "operator.yml"
    args.operator_config = str(inside)
    with pytest.raises(SystemExit, match="outside the reviewed repository"):
        cli._run_worker(args)


def test_status_does_not_create_missing_database(tmp_path: Path, capsys) -> None:
    database = tmp_path / "missing-review.db"
    args = SimpleNamespace(project_id="42", run_id=1, db=str(database))

    assert cli._run_status(args) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "not_found"}
    assert not database.exists()


def test_cli_routes_explicit_relation_recovery(monkeypatch) -> None:
    captured = {}

    def run(args):
        captured["args"] = args
        return 7

    monkeypatch.setattr(cli, "_run_recover_auto_fix_relation", run)

    result = cli.main(
        [
            "recover-autofix-relation",
            "--parent-mr-iid",
            "1",
            "--parent-sha",
            "a" * 40,
            "--intent-nonce",
            "b" * 32,
            "--plan-sha256",
            "c" * 64,
            "--plan-file",
            "/trusted/plan.json",
            "--child-mr-iid",
            "9",
            "--reason",
            "restore Store",
        ]
    )

    assert result == 7
    assert captured["args"].child_mr_iid == 9
    assert captured["args"].reason == "restore Store"


def test_cli_routes_separate_notification_resend(monkeypatch) -> None:
    captured = {}

    def run(args):
        captured["args"] = args
        return 8

    monkeypatch.setattr(cli, "_run_resend_auto_fix_notification", run)

    result = cli.main(
        [
            "resend-autofix-notification",
            "--child-mr-iid",
            "9",
            "--policy",
            "/trusted/policy.yml",
            "--reason",
            "resend approved",
        ]
    )

    assert result == 8
    assert captured["args"].policy == "/trusted/policy.yml"
    assert captured["args"].reason == "resend approved"

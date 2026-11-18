import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from review_agent_flow import cli
from review_agent_flow.gitlab import MergeRequest
from review_agent_flow.worker import WorkerResult


class FakeNotificationDispatcher:
    def __init__(self) -> None:
        self.batch_calls = 0
        self.stop_event = None

    def process_channel_batch(self, stop_event=None) -> int:
        self.batch_calls += 1
        self.stop_event = stop_event
        return 0


def test_worker_configures_target_branch_policy_resolver(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    captured = {}

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

        def process_retention(self):
            return None

        def run_forever(self, poll_interval):
            raise AssertionError("test must use --once")

        @contextmanager
        def runtime_ownership(self, *, stop_event):
            captured["runtime_stop_event"] = stop_event
            yield lambda: None

    monkeypatch.setattr(cli, "SQLiteStore", lambda path: object())
    monkeypatch.setenv("GITLAB_REVIEW_BOT_USERNAME", "review-bot")
    monkeypatch.setattr(cli, "PythonGitLabClient", lambda url, token, username: object())
    monkeypatch.setattr(cli, "GitWorktreeManager", lambda repository, root: object())
    monkeypatch.setattr(cli, "ReviewWorker", FakeWorker)
    monkeypatch.setattr(cli, "notifiers_from_operator", lambda channels: object())
    monkeypatch.setattr(cli, "resolve_notification_channels", lambda *args: ())
    monkeypatch.setattr(
        cli,
        "GitLabTargetBranchPolicyResolver",
        lambda gitlab, path: captured.update(policy_path=path) or object(),
    )
    monkeypatch.setattr(
        cli,
        "load_operator_config",
        lambda path: SimpleNamespace(
            autofix_publication=SimpleNamespace(enabled=False, allowed_projects=frozenset()),
            notification_channels={},
            backends={},
            retention=SimpleNamespace(
                publication_plan_backup_dir=None,
                sensitive_payload_days=30,
            ),
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
    assert captured["policy_path"] == ".gitlab/reviewflow.yml"
    assert captured["worker"]["project_id"] == "42"
    assert captured["notification_dispatcher"].batch_calls == 1
    assert captured["notification_dispatcher"].stop_event is captured["runtime_stop_event"]


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

        def process_retention(self):
            return None

        def run_forever(self, poll_interval):
            raise AssertionError("test must use --once")

        @contextmanager
        def runtime_ownership(self, *, stop_event):
            captured["runtime_stop_event"] = stop_event
            yield lambda: None

    monkeypatch.setattr(cli, "SQLiteStore", lambda path: object())
    monkeypatch.setenv("GITLAB_REVIEW_BOT_USERNAME", "review-bot")
    monkeypatch.setattr(cli, "PythonGitLabClient", lambda url, token, username: object())
    monkeypatch.setattr(cli, "GitWorktreeManager", lambda repository, root: object())
    monkeypatch.setattr(cli, "ReviewWorker", FakeWorker)
    monkeypatch.setattr(cli, "notifiers_from_operator", lambda channels: object())
    monkeypatch.setattr(cli, "resolve_notification_channels", lambda *args: ())
    monkeypatch.setattr(cli, "GitLabTargetBranchPolicyResolver", lambda gitlab, path: object())
    monkeypatch.setattr(
        cli,
        "load_operator_config",
        lambda path: SimpleNamespace(
            autofix_publication=SimpleNamespace(enabled=False, allowed_projects=frozenset()),
            notification_channels={},
            backends={},
            retention=SimpleNamespace(
                publication_plan_backup_dir=None,
                sensitive_payload_days=30,
            ),
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
    assert captured["notification_dispatcher"].stop_event is captured["runtime_stop_event"]


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


def test_ops_status_uses_read_only_database_and_reports_missing_database(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "review.db"
    args = ["ops-status", "--project-id", "42", "--db", str(database)]
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "unavailable",
        "reason": "database_missing",
    }
    assert not database.exists()
    from review_agent_flow.store import SQLiteStore

    SQLiteStore(database).enqueue("42", 1, "a" * 40)
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["project_id"] == "42"
    assert result["runs"]["pending"] == 1
    assert result["retention"]["last_sweep_result"] == "unavailable"


def test_review_mr_is_project_bound_and_idempotent(monkeypatch, tmp_path: Path, capsys) -> None:
    sha = "a" * 40
    merge_request = MergeRequest(
        "42",
        7,
        "Review me",
        "alice",
        "feature",
        "main",
        sha,
        "https://gitlab.example/group/project/-/merge_requests/7",
    )

    class FakeGitLab:
        def get_merge_request(self, project_id, mr_iid):
            assert (project_id, mr_iid) == ("42", 7)
            return merge_request

    monkeypatch.setattr(cli, "PythonGitLabClient", lambda url, token: FakeGitLab())
    args = SimpleNamespace(
        gitlab_url="https://gitlab.example",
        gitlab_token="token",
        project_id="42",
        mr_iid=7,
        db=str(tmp_path / "review.db"),
    )

    assert cli._run_review_mr(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli._run_review_mr(args) == 0
    duplicate = json.loads(capsys.readouterr().out)

    assert first["status"] == "enqueued"
    assert duplicate["status"] == "existing"
    assert duplicate["run_id"] == first["run_id"]
    assert duplicate["sha"] == sha
    assert duplicate["run_status"] == "pending"


@pytest.mark.parametrize(
    ("merge_request", "message"),
    [
        (
            MergeRequest("99", 7, "Wrong Project", "alice", "feature", "main", "a" * 40, "url"),
            "bound Project",
        ),
        (
            MergeRequest(
                "42",
                7,
                "Closed",
                "alice",
                "feature",
                "main",
                "a" * 40,
                "url",
                state="closed",
            ),
            "must be opened",
        ),
        (
            MergeRequest("42", 7, "Bad SHA", "alice", "feature", "main", "bad", "url"),
            "invalid current",
        ),
    ],
)
def test_review_mr_rejects_unbound_or_ineligible_resource(
    monkeypatch,
    tmp_path: Path,
    merge_request: MergeRequest,
    message: str,
) -> None:
    monkeypatch.setattr(
        cli,
        "PythonGitLabClient",
        lambda url, token: SimpleNamespace(
            get_merge_request=lambda project_id, mr_iid: merge_request
        ),
    )
    args = SimpleNamespace(
        gitlab_url="https://gitlab.example",
        gitlab_token="token",
        project_id="42",
        mr_iid=7,
        db=str(tmp_path / "review.db"),
    )

    with pytest.raises(SystemExit, match=message):
        cli._run_review_mr(args)


def test_cli_routes_manual_review_mr_trigger(monkeypatch) -> None:
    captured = {}

    def run(args):
        captured["mr_iid"] = args.mr_iid
        return 5

    monkeypatch.setattr(cli, "_run_review_mr", run)

    assert cli.main(["review-mr", "--mr-iid", "7"]) == 5
    assert captured == {"mr_iid": 7}


def test_review_mr_rejects_project_id_outside_review_key_contract(
    monkeypatch, tmp_path: Path
) -> None:
    merge_request = MergeRequest(
        "group/project",
        7,
        "Title",
        "alice",
        "feature",
        "main",
        "a" * 40,
        "url",
    )
    monkeypatch.setattr(
        cli,
        "PythonGitLabClient",
        lambda url, token: SimpleNamespace(
            get_merge_request=lambda project_id, mr_iid: merge_request
        ),
    )
    args = SimpleNamespace(
        gitlab_url="https://gitlab.example",
        gitlab_token="token",
        project_id="group/project",
        mr_iid=7,
        db=str(tmp_path / "review.db"),
    )

    with pytest.raises(SystemExit, match="invalid ReviewKey"):
        cli._run_review_mr(args)


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
            "--operator-config",
            "/trusted/operator.yml",
            "--reason",
            "resend approved",
        ]
    )

    assert result == 8
    assert captured["args"].operator_config == "/trusted/operator.yml"
    assert captured["args"].reason == "resend approved"


def test_notification_resend_requires_frozen_policy_authority(monkeypatch, tmp_path: Path) -> None:
    store = SimpleNamespace(
        get_program_auto_fix_relation_by_child=lambda project_id, mr_iid: SimpleNamespace(run_id=7),
        get_policy_snapshot=lambda run_id: None,
    )
    monkeypatch.setattr(
        cli,
        "_recovery_service",
        lambda args: SimpleNamespace(store=store),
    )
    args = SimpleNamespace(
        project_id="42",
        operator_config=str(tmp_path / "operator.yml"),
        child_mr_iid=9,
    )

    with pytest.raises(SystemExit, match="frozen Policy Snapshot"):
        cli._run_resend_auto_fix_notification(args)


def test_cli_routes_operator_plan_transitions(monkeypatch) -> None:
    captured = []

    def run(args, action):
        captured.append((args.plan_id, args.reason, action))
        return 9

    monkeypatch.setattr(cli, "_run_transition_auto_fix_plan", run)

    assert cli.main(["rearm-autofix-plan", "--plan-id", "7", "--reason", "evidence checked"]) == 9
    assert cli.main(["abandon-autofix-plan", "--plan-id", "8", "--reason", "pilot cancelled"]) == 9
    assert captured == [
        (7, "evidence checked", "rearm"),
        (8, "pilot cancelled", "abandon"),
    ]


def test_cli_routes_api_process(monkeypatch) -> None:
    captured = {}

    def run(args):
        captured.update(host=args.host, port=args.port, log_level=args.log_level)
        return 6

    monkeypatch.setattr(cli, "_run_api", run)

    assert cli.main(["api", "--host", "127.0.0.2", "--port", "9000", "--log-level", "warning"]) == 6
    assert captured == {"host": "127.0.0.2", "port": 9000, "log_level": "warning"}

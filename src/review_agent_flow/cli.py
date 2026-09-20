"""Command-line entry points."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import socket
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from threading import Event
from uuid import uuid4

from . import __version__
from .autofix_recovery import AutoFixPlanOperator, AutoFixRecoveryService, OperatorContext
from .backup import create_backup
from .gitlab import canonical_note_marker, is_valid_sha, normalize_sha
from .gitlab_client import PythonGitLabClient
from .notifications import notifiers_from_operator
from .operator_config import (
    OperatorConfigError,
    load_operator_config,
    resolve_backend,
    resolve_notification_channels,
)
from .policy import ReviewPolicy, policy_from_json, policy_template
from .policy_resolver import GitLabTargetBranchPolicyResolver
from .repository import GitShaEnsurer
from .retention import RetentionManager
from .store import SQLiteStore
from .worker import ReviewWorker
from .worktree import GitWorktreeManager


def _init_policy(path: Path, force: bool) -> int:
    if path.exists() and not force:
        print(f"refusing to overwrite existing file: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(policy_template(), encoding="utf-8")
    print(path)
    return 0


def _required(value: str | None, env_name: str) -> str:
    configured = value if value is not None else os.getenv(env_name)
    if configured is not None and configured.strip():
        return configured.strip()
    raise SystemExit(f"missing required option or environment variable: {env_name}")


def _run_worker(args: argparse.Namespace) -> int:
    gitlab_url = _required(args.gitlab_url, "GITLAB_URL")
    gitlab_token = _required(args.gitlab_token, "GITLAB_TOKEN")
    project_id = _required(args.project_id, "REVIEW_AGENT_FLOW_PROJECT_ID")
    repository = Path(_required(args.repository, "REVIEW_AGENT_FLOW_REPOSITORY")).expanduser().resolve()
    policy_path = args.policy or os.getenv(
        "REVIEW_AGENT_FLOW_POLICY", ".gitlab/reviewflow.yml"
    )
    operator_path = Path(
        _required(args.operator_config, "REVIEW_AGENT_FLOW_OPERATOR_CONFIG")
    ).expanduser().resolve()
    if operator_path == repository or repository in operator_path.parents:
        raise SystemExit("Operator configuration must be outside the reviewed repository")
    try:
        operator_config = load_operator_config(operator_path)
        notification_channel_ids = tuple(
            channel_id
            for channel_id, channel in operator_config.notification_channels.items()
            if project_id in channel.allowed_projects
        )
        resolved_notifications = resolve_notification_channels(
            operator_config, notification_channel_ids, project_id
        )
    except OperatorConfigError as exc:
        raise SystemExit(f"invalid Operator configuration: {exc}") from exc
    review_bot_username = _required(None, "GITLAB_REVIEW_BOT_USERNAME")
    backup_dir = operator_config.retention.publication_plan_backup_dir
    if backup_dir is not None and (
        backup_dir == repository or repository in backup_dir.parents
    ):
        raise SystemExit(
            "Publication Plan backup directory must be outside the reviewed repository"
        )
    store = SQLiteStore(args.db or os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3"))
    gitlab = PythonGitLabClient(gitlab_url, gitlab_token, review_bot_username)
    allowed_timeouts = [
        profile.timeout_seconds
        for backend in operator_config.backends.values()
        if backend.enabled and project_id in backend.allowed_projects
        for profile in backend.profiles.values()
    ]
    max_agent_timeout = max(allowed_timeouts, default=1800)
    worker = ReviewWorker(
        store=store,
        gitlab=gitlab,
        worktrees=GitWorktreeManager(
            repository=repository,
            root=Path(args.worktrees or os.getenv("REVIEW_AGENT_FLOW_WORKTREES", "/tmp/review-agent-flow")),
        ),
        policy=ReviewPolicy(),
        repository=repository,
        gitlab_url=gitlab_url,
        gitlab_token=gitlab_token,
        project_id=project_id,
        sha_ensurer=GitShaEnsurer(
            repository, fetch_timeout_seconds=getattr(args, "fetch_timeout", 30.0)
        ),
        notifier=notifiers_from_operator(resolved_notifications),
        agent_timeout_seconds=max_agent_timeout,
        lease_seconds=max_agent_timeout + 60,
        retry_delay_seconds=args.poll_interval,
        autofix_publication=operator_config.autofix_publication,
        policy_resolver=GitLabTargetBranchPolicyResolver(gitlab, policy_path),
        backend_resolver=lambda policy: resolve_backend(
            operator_config, policy.ai, project_id
        ),
        retention_manager=RetentionManager(store, operator_config.retention),
    )
    if args.once:
        once_stop = Event()
        with worker.runtime_ownership(stop_event=once_stop) as assert_owned:
            assert_owned()
            result = worker.process_one()
            if result is None:
                assert_owned()
                result = worker.reconcile_auto_fixes(limit=1)
            assert_owned()
            worker.process_worktree_cleanup(limit=1)
            assert_owned()
            worker.notification_dispatcher.process_channel_batch(stop_event=once_stop)
            assert_owned()
            worker.process_retention()
        if result is None:
            print(json.dumps({"status": "idle"}))
            return 0
        print(json.dumps(asdict(result), default=str))
        return 0 if result.status in {"succeeded", "deferred"} else 1
    stop_event = Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_event.set()

    previous_handlers = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        worker.run_forever(args.poll_interval, stop_event=stop_event)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


def _run_api(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "review_agent_flow.web:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        proxy_headers=False,
    )
    return 0


def _run_status(args: argparse.Namespace) -> int:
    """Print one project-bound Review Run snapshot without contacting GitLab."""

    project_id = _required(args.project_id, "REVIEW_AGENT_FLOW_PROJECT_ID")
    database = args.db or os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3")
    try:
        store = SQLiteStore(database, read_only=True)
    except FileNotFoundError:
        print(json.dumps({"status": "not_found"}, sort_keys=True))
        return 1
    snapshot = store.get_project_bound_review_run_status(project_id, args.run_id)
    if snapshot is None:
        print(json.dumps({"status": "not_found"}, sort_keys=True))
        return 1
    print(json.dumps(snapshot, sort_keys=True))
    return 0


def _run_ops_status(args: argparse.Namespace) -> int:
    """Print local Store aggregates without contacting providers or claiming work."""
    project_id = _required(args.project_id, "REVIEW_AGENT_FLOW_PROJECT_ID")
    database = args.db or os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3")
    try:
        store = SQLiteStore(database, read_only=True)
    except FileNotFoundError:
        print(json.dumps({"status": "unavailable", "reason": "database_missing"}, sort_keys=True))
        return 1
    print(json.dumps(store.get_project_ops_status(project_id), sort_keys=True))
    return 0


def _run_backup_store(args: argparse.Namespace) -> int:
    try:
        manifest = create_backup(
            Path(args.db), Path(args.plan_backup_dir), Path(args.output)
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"backup-store failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "created",
        "output": str(Path(args.output).absolute()),
        "schema_version": manifest["schema_version"],
        "snapshot_at": manifest["snapshot_at"],
        "plan_count": len(manifest["plans"]),
    }, sort_keys=True))
    return 0


def _run_review_mr(args: argparse.Namespace) -> int:
    """Idempotently enqueue the current head of one bound-Project Merge Request."""

    gitlab_url = _required(args.gitlab_url, "GITLAB_URL")
    gitlab_token = _required(args.gitlab_token, "GITLAB_TOKEN")
    project_id = _required(args.project_id, "REVIEW_AGENT_FLOW_PROJECT_ID")
    if type(args.mr_iid) is not int or args.mr_iid <= 0:
        raise SystemExit("--mr-iid must be a positive integer")
    gitlab = PythonGitLabClient(gitlab_url, gitlab_token)
    merge_request = gitlab.get_merge_request(project_id, args.mr_iid)
    if merge_request.project_id != project_id or merge_request.iid != args.mr_iid:
        raise SystemExit("GitLab Merge Request identity does not match the bound Project")
    state = str(merge_request.state or "opened").lower()
    if state != "opened":
        raise SystemExit(f"Merge Request must be opened, not {state}")
    if not is_valid_sha(merge_request.sha):
        raise SystemExit("GitLab returned an invalid current Merge Request head SHA")
    sha = normalize_sha(merge_request.sha)
    try:
        canonical_note_marker(project_id, args.mr_iid, sha)
    except ValueError as exc:
        raise SystemExit(f"invalid ReviewKey: {exc}") from exc
    store = SQLiteStore(
        args.db or os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3")
    )
    store.add_redaction_secret(gitlab_token)
    run, created = store.enqueue_with_status(project_id, args.mr_iid, sha)
    current = store.wake_run(project_id, args.mr_iid, sha) or run
    print(
        json.dumps(
            {
                "status": "enqueued" if created else "existing",
                "project_id": project_id,
                "mr_iid": args.mr_iid,
                "sha": sha,
                "run_id": current.run_id,
                "run_status": current.status,
                "merge_request_url": merge_request.web_url,
            },
            sort_keys=True,
        )
    )
    return 0


def _operator_context(args: argparse.Namespace) -> OperatorContext:
    return OperatorContext(
        recovery_id=args.recovery_id or uuid4().hex,
        reason=args.reason.strip(),
        principal=getpass.getuser(),
        hostname=socket.gethostname(),
        tool_version=__version__,
    )


def _recovery_service(args: argparse.Namespace) -> AutoFixRecoveryService:
    gitlab_url = _required(args.gitlab_url, "GITLAB_URL")
    gitlab_token = _required(args.gitlab_token, "GITLAB_TOKEN")
    review_bot_username = _required(None, "GITLAB_REVIEW_BOT_USERNAME")
    store = SQLiteStore(args.db or os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3"))
    store.add_redaction_secret(gitlab_token)
    return AutoFixRecoveryService(
        store,
        PythonGitLabClient(gitlab_url, gitlab_token, review_bot_username),
        review_bot_username,
    )


def _run_recover_auto_fix_relation(args: argparse.Namespace) -> int:
    service = _recovery_service(args)
    project_id = _required(args.project_id, "REVIEW_AGENT_FLOW_PROJECT_ID")
    plan_json = Path(args.plan_file).expanduser().resolve().read_text(encoding="utf-8").strip()
    context = _operator_context(args)
    try:
        relation = service.recover_relation(
            context=context,
            project_id=project_id,
            parent_mr_iid=args.parent_mr_iid,
            parent_sha=args.parent_sha,
            intent_nonce=args.intent_nonce,
            plan_json=plan_json,
            plan_sha256=args.plan_sha256,
            child_mr_iid=args.child_mr_iid,
        )
    except Exception:  # noqa: BLE001 - detailed rejection is redacted in the durable audit
        print(json.dumps({"status": "rejected", "recovery_id": context.recovery_id}))
        return 1
    print(
        json.dumps(
            {
                "status": "recovered",
                "recovery_id": context.recovery_id,
                "relation_id": relation.relation_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _run_resend_auto_fix_notification(args: argparse.Namespace) -> int:
    service = _recovery_service(args)
    project_id = _required(args.project_id, "REVIEW_AGENT_FLOW_PROJECT_ID")
    operator_path = Path(
        _required(args.operator_config, "REVIEW_AGENT_FLOW_OPERATOR_CONFIG")
    ).expanduser().resolve()
    relation = service.store.get_program_auto_fix_relation_by_child(
        project_id, args.child_mr_iid
    )
    if relation is None:
        raise SystemExit("Auto-fix Relation was not found")
    snapshot = service.store.get_policy_snapshot(relation.run_id)
    if snapshot is None:
        raise SystemExit("notification resend requires the frozen Policy Snapshot")
    try:
        operator_config = load_operator_config(operator_path)
        channel_ids = policy_from_json(snapshot.policy_json).notifications.channels
        resolved = resolve_notification_channels(operator_config, channel_ids, project_id)
    except OperatorConfigError as exc:
        raise SystemExit(f"invalid Operator configuration: {exc}") from exc
    channels = tuple(
        (channel.channel_id, channel.channel_type) for channel in resolved
    )
    context = _operator_context(args)
    try:
        request = service.resend_notification(
            context=context,
            project_id=project_id,
            child_mr_iid=args.child_mr_iid,
            channels=channels,
        )
    except Exception:  # noqa: BLE001 - detailed rejection is redacted in the durable audit
        print(json.dumps({"status": "rejected", "recovery_id": context.recovery_id}))
        return 1
    print(
        json.dumps(
            {
                "status": "queued",
                "recovery_id": context.recovery_id,
                "request_id": request.request_id,
                "generation": request.generation,
            },
            sort_keys=True,
        )
    )
    return 0


def _run_transition_auto_fix_plan(args: argparse.Namespace, action: str) -> int:
    store = SQLiteStore(
        args.db or os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3")
    )
    context = _operator_context(args)
    try:
        audit = AutoFixPlanOperator(store).transition(
            context=context,
            plan_id=args.plan_id,
            action=action,
        )
    except Exception:  # noqa: BLE001 - rejection detail is durable and may be sensitive
        print(json.dumps({"status": "rejected", "action_id": context.recovery_id}))
        return 1
    print(
        json.dumps(
            {
                "status": "rearmed" if action == "rearm" else "abandoned",
                "action_id": audit.action_id,
                "plan_id": audit.plan_id,
                "from_state": audit.from_state,
                "to_state": audit.to_state,
                "write_phase": audit.write_phase,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="review-agent-flow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="generate a Project policy template")
    init.add_argument("path", nargs="?", default=".gitlab/reviewflow.yml")
    init.add_argument("--force", action="store_true")
    worker = subparsers.add_parser("worker", help="run the Review Run worker")
    worker.add_argument("--gitlab-url")
    worker.add_argument("--gitlab-token")
    worker.add_argument("--project-id")
    worker.add_argument("--repository")
    worker.add_argument("--policy")
    worker.add_argument("--operator-config")
    worker.add_argument("--db")
    worker.add_argument("--worktrees")
    worker.add_argument("--fetch-timeout", type=int, default=60)
    worker.add_argument("--poll-interval", type=float, default=5.0)
    worker.add_argument("--once", action="store_true")
    api = subparsers.add_parser("api", help="run the webhook/status API process")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
    )
    status = subparsers.add_parser("status", help="query one project-bound Review Run")
    status.add_argument("--run-id", type=int, required=True)
    status.add_argument("--project-id")
    status.add_argument("--db")
    ops_status = subparsers.add_parser("ops-status", help="query local Project operations summary")
    ops_status.add_argument("--project-id")
    ops_status.add_argument("--db")
    backup = subparsers.add_parser("backup-store", help="create a private SQLite and Plan bundle")
    backup.add_argument("--db", required=True)
    backup.add_argument("--plan-backup-dir", required=True)
    backup.add_argument("--output", required=True)
    review_mr = subparsers.add_parser(
        "review-mr",
        help="idempotently enqueue the current head of one bound-Project Merge Request",
    )
    review_mr.add_argument("--mr-iid", type=int, required=True)
    review_mr.add_argument("--gitlab-url")
    review_mr.add_argument("--gitlab-token")
    review_mr.add_argument("--project-id")
    review_mr.add_argument("--db")
    recover = subparsers.add_parser(
        "recover-autofix-relation",
        help="explicitly validate and recover one lost Auto-fix Relation",
    )
    recover.add_argument("--gitlab-url")
    recover.add_argument("--gitlab-token")
    recover.add_argument("--project-id")
    recover.add_argument("--parent-mr-iid", type=int, required=True)
    recover.add_argument("--parent-sha", required=True)
    recover.add_argument("--intent-nonce", required=True)
    recover.add_argument("--plan-sha256", required=True)
    recover.add_argument("--plan-file", required=True)
    recover.add_argument("--child-mr-iid", type=int, required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument("--recovery-id")
    recover.add_argument("--db")
    resend = subparsers.add_parser(
        "resend-autofix-notification",
        help="create a new audited notification request for an existing Relation",
    )
    resend.add_argument("--gitlab-url")
    resend.add_argument("--gitlab-token")
    resend.add_argument("--project-id")
    resend.add_argument("--child-mr-iid", type=int, required=True)
    resend.add_argument("--operator-config")
    resend.add_argument("--reason", required=True)
    resend.add_argument("--recovery-id")
    resend.add_argument("--db")
    for command, help_text in (
        ("rearm-autofix-plan", "rearm one blocked Auto-fix Plan using its safe phase"),
        ("abandon-autofix-plan", "terminally abandon one idle Auto-fix Plan"),
    ):
        transition = subparsers.add_parser(command, help=help_text)
        transition.add_argument("--plan-id", type=int, required=True)
        transition.add_argument("--reason", required=True)
        transition.add_argument("--recovery-id")
        transition.add_argument("--db")
    args = parser.parse_args(argv)
    if args.command == "init":
        return _init_policy(Path(args.path), args.force)
    if args.command == "worker":
        return _run_worker(args)
    if args.command == "api":
        return _run_api(args)
    if args.command == "status":
        return _run_status(args)
    if args.command == "ops-status":
        return _run_ops_status(args)
    if args.command == "backup-store":
        return _run_backup_store(args)
    if args.command == "review-mr":
        return _run_review_mr(args)
    if args.command == "recover-autofix-relation":
        return _run_recover_auto_fix_relation(args)
    if args.command == "resend-autofix-notification":
        return _run_resend_auto_fix_notification(args)
    if args.command == "rearm-autofix-plan":
        return _run_transition_auto_fix_plan(args, "rearm")
    if args.command == "abandon-autofix-plan":
        return _run_transition_auto_fix_plan(args, "abandon")
    return 0

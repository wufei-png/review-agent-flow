"""Command-line entry points."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .gitlab_client import PythonGitLabClient
from .notifications import notifiers_from_policy
from .policy import load_policy, policy_template
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
    if value:
        return value
    from_environment = os.getenv(env_name)
    if from_environment:
        return from_environment
    raise SystemExit(f"missing required option or environment variable: {env_name}")


def _run_worker(args: argparse.Namespace) -> int:
    gitlab_url = _required(args.gitlab_url, "GITLAB_URL")
    gitlab_token = _required(args.gitlab_token, "GITLAB_TOKEN")
    repository = Path(_required(args.repository, "REVIEW_AGENT_FLOW_REPOSITORY"))
    policy_path = Path(args.policy or os.getenv("REVIEW_AGENT_FLOW_POLICY", ".gitlab/reviewflow.yml"))
    policy = load_policy(policy_path)
    store = SQLiteStore(args.db or os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3"))
    worker = ReviewWorker(
        store=store,
        gitlab=PythonGitLabClient(gitlab_url, gitlab_token),
        worktrees=GitWorktreeManager(
            repository=repository,
            root=Path(args.worktrees or os.getenv("REVIEW_AGENT_FLOW_WORKTREES", "/tmp/review-agent-flow")),
        ),
        policy=policy,
        repository=repository,
        gitlab_url=gitlab_url,
        gitlab_token=gitlab_token,
        notifier=notifiers_from_policy(policy.notifications.channels),
        agent_timeout_seconds=args.agent_timeout,
    )
    if args.once:
        result = worker.process_one()
        if result is None:
            print(json.dumps({"status": "idle"}))
            return 0
        print(json.dumps(asdict(result), default=str))
        return 0 if result.status in {"succeeded", "deferred"} else 1
    worker.run_forever(args.poll_interval)
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
    worker.add_argument("--repository")
    worker.add_argument("--policy")
    worker.add_argument("--db")
    worker.add_argument("--worktrees")
    worker.add_argument("--agent-timeout", type=int, default=1800)
    worker.add_argument("--poll-interval", type=float, default=5.0)
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "init":
        return _init_policy(Path(args.path), args.force)
    if args.command == "worker":
        return _run_worker(args)
    return 0

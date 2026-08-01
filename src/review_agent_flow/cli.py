"""Command-line entry points."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .policy import policy_template


def _init_policy(path: Path, force: bool) -> int:
    if path.exists() and not force:
        print(f"refusing to overwrite existing file: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(policy_template(), encoding="utf-8")
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="review-agent-flow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="generate a Project policy template")
    init.add_argument("path", nargs="?", default=".gitlab/reviewflow.yml")
    init.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "init":
        return _init_policy(Path(args.path), args.force)
    return 0

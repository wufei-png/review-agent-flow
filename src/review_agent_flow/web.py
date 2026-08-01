"""FastAPI webhook surface."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from .events import parse_webhooks
from .policy import load_policy
from .store import SQLiteStore


def _required_config(value: str | None, env_name: str) -> str:
    configured = value or os.getenv(env_name)
    if not configured:
        raise RuntimeError(f"missing required webhook configuration: {env_name}")
    return configured


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise RuntimeError(f"{name} must be true or false")
    return normalized == "true"


def create_app(
    store: SQLiteStore | None = None,
    webhook_secret: str | None = None,
    project_id: str | None = None,
    pipeline_gate: bool | None = None,
    policy_path: str | Path | None = None,
) -> FastAPI:
    store = store or SQLiteStore(os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3"))
    webhook_secret = _required_config(webhook_secret, "GITLAB_WEBHOOK_SECRET")
    project_id = _required_config(project_id, "REVIEW_AGENT_FLOW_PROJECT_ID")
    if pipeline_gate is None:
        configured_policy = policy_path or os.getenv("REVIEW_AGENT_FLOW_POLICY")
        if configured_policy is None and os.getenv("REVIEW_AGENT_FLOW_REPOSITORY"):
            configured_policy = ".gitlab/reviewflow.yml"
        if configured_policy is not None:
            configured_path = Path(configured_policy)
            repository = os.getenv("REVIEW_AGENT_FLOW_REPOSITORY")
            if not configured_path.is_absolute() and repository:
                configured_path = Path(repository) / configured_path
            pipeline_gate = load_policy(configured_path).pipeline_gate
        else:
            pipeline_gate = _env_bool("REVIEW_AGENT_FLOW_PIPELINE_GATE")
    app = FastAPI(title="Review Agent Flow", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/gitlab")
    def gitlab_webhook(
        payload: dict[str, Any],
        x_gitlab_token: str | None = Header(default=None),
        x_gitlab_event: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not hmac.compare_digest(x_gitlab_token or "", webhook_secret):
            raise HTTPException(status_code=401, detail="invalid GitLab webhook token")
        decisions = parse_webhooks(x_gitlab_event, payload)
        responses: list[dict[str, Any]] = []
        for decision in decisions:
            if not decision.accepted:
                responses.append({"accepted": False, "reason": decision.reason})
                continue
            assert decision.project_id is not None
            assert decision.mr_iid is not None
            assert decision.sha is not None
            if decision.project_id != project_id:
                responses.append(
                    {
                        "accepted": False,
                        "reason": "event project is not the configured Project",
                        "project_id": decision.project_id,
                    }
                )
                continue
            if decision.kind == "pipeline":
                if not pipeline_gate:
                    responses.append(
                        {"accepted": False, "reason": "pipeline gate is disabled", "kind": "pipeline"}
                    )
                    continue
                match = store.resolve_auto_fix_match(
                    decision.project_id,
                    decision.mr_iid,
                    decision.source_branch,
                    decision.target_branch,
                    decision.description,
                )
                if match is not None:
                    responses.append(
                        {
                            "accepted": True,
                            "kind": "pipeline",
                            "status": "auto-fix pipeline ignored",
                        }
                    )
                    continue
                woken = store.wake_run(decision.project_id, decision.mr_iid, decision.sha)
                responses.append(
                    {
                        "accepted": True,
                        "kind": "pipeline",
                        "status": "woken" if woken is not None and woken.status == "pending" else "ignored",
                    }
                )
                continue
            match = store.resolve_auto_fix_match(
                decision.project_id,
                decision.mr_iid,
                decision.source_branch,
                decision.target_branch,
                decision.description,
            )
            if decision.kind == "merge":
                if match is None:
                    responses.append(
                        {"accepted": False, "reason": "untracked Merge Request merge event"}
                    )
                    continue
                store.mark_auto_fix_merged(decision.project_id, decision.mr_iid, decision.sha)
                responses.append(
                    {
                        "accepted": True,
                        "kind": "autofix_merged",
                        "merged_sha": decision.sha,
                    }
                )
                continue
            kind = "autofix" if match is not None else "normal"
            run = store.enqueue(
                decision.project_id,
                decision.mr_iid,
                decision.sha,
                kind,
            )
            responses.append(
                {
                    "accepted": True,
                    "kind": kind,
                    "run_id": run.run_id,
                    "status": run.status,
                }
            )
        if len(responses) == 1:
            return responses[0]
        return {"accepted": any(item["accepted"] for item in responses), "decisions": responses}

    return app

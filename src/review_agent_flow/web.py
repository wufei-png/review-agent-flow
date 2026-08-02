"""FastAPI webhook surface."""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from .events import parse_webhooks
from .store import SQLiteStore


class StartupConfigurationError(RuntimeError):
    """Raised when a production entry point is missing required configuration."""


def _required_config(value: str | None, env_name: str) -> str:
    configured = value if value is not None else os.getenv(env_name)
    if configured is None or not configured.strip():
        raise StartupConfigurationError(
            f"ReviewFlow startup configuration error: {env_name} must be set"
        )
    return configured


def create_app(
    store: SQLiteStore | None = None,
    webhook_secret: str | None = None,
    project_id: str | None = None,
) -> FastAPI:
    webhook_secret = _required_config(webhook_secret, "GITLAB_WEBHOOK_SECRET")
    project_id = _required_config(project_id, "REVIEW_AGENT_FLOW_PROJECT_ID").strip()
    store = store or SQLiteStore(os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3"))
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
        if any(decision.project_id and decision.project_id != project_id for decision in decisions):
            raise HTTPException(status_code=403, detail="project is not bound to this deployment")
        responses: list[dict[str, Any]] = []
        for decision in decisions:
            if not decision.accepted:
                responses.append({"accepted": False, "reason": decision.reason})
                continue
            assert decision.project_id is not None
            assert decision.mr_iid is not None
            assert decision.sha is not None
            if decision.kind == "autofix_merged":
                store.mark_auto_fix_merged(decision.project_id, decision.mr_iid, decision.sha)
                responses.append(
                    {
                        "accepted": True,
                        "kind": decision.kind,
                        "merged_sha": decision.sha,
                    }
                )
                continue
            run = store.enqueue(
                decision.project_id,
                decision.mr_iid,
                decision.sha,
                decision.kind or "normal",
            )
            responses.append(
                {
                    "accepted": True,
                    "kind": decision.kind,
                    "run_id": run.run_id,
                    "status": run.status,
                }
            )
        if len(responses) == 1:
            return responses[0]
        return {"accepted": any(item["accepted"] for item in responses), "decisions": responses}

    return app

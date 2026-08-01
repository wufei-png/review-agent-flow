"""FastAPI webhook surface."""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from .events import parse_webhooks
from .store import SQLiteStore


def create_app(store: SQLiteStore | None = None, webhook_secret: str | None = None) -> FastAPI:
    store = store or SQLiteStore(os.getenv("REVIEW_AGENT_FLOW_DB", "review-agent-flow.sqlite3"))
    webhook_secret = webhook_secret or os.getenv("GITLAB_WEBHOOK_SECRET")
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
        if webhook_secret is not None and not hmac.compare_digest(x_gitlab_token or "", webhook_secret):
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

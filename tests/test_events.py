from review_agent_flow.events import (
    parse_auto_fix_branch,
    parse_auto_fix_marker,
    parse_webhook,
    parse_webhooks,
)

SHA = "a" * 40
MERGED_SHA = "b" * 40
PIPELINE_SHA = "c" * 40
NONCE = "d" * 32


def _merge_request_payload(**attrs):
    object_attributes = {
        "action": "update",
        "iid": 1,
        "title": "Improve validation",
        "source_branch": "feature/validation",
        "target_branch": "main",
        "last_commit": {"id": SHA},
        "work_in_progress": False,
    }
    object_attributes.update(attrs)
    return {
        "project": {"id": 42},
        "user": {"username": "alice"},
        "object_attributes": object_attributes,
    }


def test_normal_merge_request_is_accepted() -> None:
    decision = parse_webhook("Merge Request Hook", _merge_request_payload())
    assert decision.accepted is True
    assert decision.kind == "normal"
    assert decision.project_id == "42"
    assert decision.mr_iid == 1
    assert decision.sha == SHA


def test_auto_fix_shaped_merge_request_is_not_trusted_by_parser() -> None:
    decision = parse_webhook(
        "Merge Request Hook",
        _merge_request_payload(
            title="[ReviewFlow Auto-Fix] Improve validation",
            source_branch=f"autofix/MR-1/sha-{SHA}/intent-{NONCE}",
        ),
    )
    assert decision.accepted is True
    assert decision.kind == "normal"
    assert decision.parent_mr_iid is None
    assert parse_auto_fix_branch(f"autofix/MR-1/sha-{SHA}/intent-{NONCE}") == (1, SHA, NONCE)
    assert parse_auto_fix_branch("autofix/MR-1/run-7") is None


def test_canonical_auto_fix_marker_is_recognized_only_as_an_exact_line() -> None:
    marker = (
        f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
        f"intent={NONCE};plan_sha256={'e' * 64} -->"
    )

    assert parse_auto_fix_marker(f"context\n{marker}") == (
        "42",
        1,
        SHA,
        NONCE,
        "e" * 64,
    )
    assert parse_auto_fix_marker(f"prefix {marker}") is None


def test_failed_pipeline_is_ignored() -> None:
    decision = parse_webhook(
        "Pipeline Hook",
        {
            "project": {"id": 42},
            "object_attributes": {"status": "failed", "sha": PIPELINE_SHA},
            "merge_request": {"iid": 1},
        },
    )
    assert decision.accepted is False


def test_auto_fix_merge_event_carries_the_resulting_parent_sha() -> None:
    decision = parse_webhook(
        "Merge Request Hook",
        _merge_request_payload(
            action="merge",
            title="[ReviewFlow Auto-Fix] Improve validation",
            source_branch=f"autofix/MR-1/sha-{SHA}/intent-{NONCE}",
            merge_commit_sha=MERGED_SHA,
        ),
    )
    assert decision.accepted is True
    assert decision.kind == "merge"
    assert decision.merged_sha == MERGED_SHA


def test_pipeline_hook_fans_out_merge_request_list_and_skips_drafts() -> None:
    decisions = parse_webhooks(
        "Pipeline Hook",
        {
            "project": {"id": 42},
            "object_attributes": {"status": "success", "sha": PIPELINE_SHA},
            "merge_request": [
                {"iid": 1, "title": "Ready", "source_branch": "feature/one"},
                {"iid": 2, "title": "Draft", "draft": True, "source_branch": "feature/two"},
                {"iid": 3, "title": "Ready too", "source_branch": "feature/three"},
            ],
        },
    )
    assert [decision.mr_iid for decision in decisions if decision.accepted] == [1, 3]
    assert decisions[1].accepted is False
    assert decisions[1].reason == "draft merge request"


def test_normal_merge_request_does_not_fallback_to_merge_commit_sha() -> None:
    decision = parse_webhook(
        "Merge Request Hook",
        _merge_request_payload(last_commit=None, last_commit_id=None, merge_commit_sha=MERGED_SHA),
    )
    assert decision.accepted is False
    assert decision.reason == "missing project, MR IID, or SHA"


def test_invalid_merge_request_sha_is_rejected() -> None:
    decision = parse_webhook(
        "Merge Request Hook",
        _merge_request_payload(last_commit={"id": "not-a-sha"}),
    )
    assert decision.accepted is False
    assert "invalid Merge Request head SHA" in decision.reason

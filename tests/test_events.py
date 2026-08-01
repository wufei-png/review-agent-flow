from review_agent_flow.events import parse_auto_fix_branch, parse_webhook


def _merge_request_payload(**attrs):
    object_attributes = {
        "action": "update",
        "iid": 1,
        "title": "Improve validation",
        "source_branch": "feature/validation",
        "target_branch": "main",
        "last_commit": {"id": "abc"},
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
    assert decision.sha == "abc"


def test_auto_fix_merge_request_is_classified_without_ai_path() -> None:
    decision = parse_webhook(
        "Merge Request Hook",
        _merge_request_payload(
            title="[ReviewFlow Auto-Fix] Improve validation",
            source_branch="autofix/MR-1/run-7",
        ),
    )
    assert decision.accepted is True
    assert decision.kind == "autofix"
    assert decision.parent_mr_iid == 1
    assert parse_auto_fix_branch("autofix/MR-1/run-7") == (1, 7)


def test_failed_pipeline_is_ignored() -> None:
    decision = parse_webhook(
        "Pipeline Hook",
        {
            "project": {"id": 42},
            "object_attributes": {"status": "failed", "sha": "abc"},
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
            source_branch="autofix/MR-1/run-7",
            merge_commit_sha="merged-parent-sha",
        ),
    )
    assert decision.accepted is True
    assert decision.kind == "autofix_merged"
    assert decision.merged_sha == "merged-parent-sha"

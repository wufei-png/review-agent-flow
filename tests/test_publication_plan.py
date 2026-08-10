import hashlib
import json

import pytest

from review_agent_flow.publication_plan import (
    MAX_PUBLICATION_ACTIONS,
    MAX_PUBLICATION_FILE_BYTES,
    PublicationPlanError,
    PublicationPlanV1,
    canonical_json,
)

SHA = "a" * 40
NONCE = "b" * 32


def _plan_value() -> dict[str, object]:
    actions = [
        {
            "action": "update",
            "file_path": "README.md",
            "content": "fixed\n",
            "execute_filemode": False,
        }
    ]
    candidate_sha256 = hashlib.sha256(
        canonical_json({"version": 1, "base_sha": SHA, "actions": actions}).encode()
    ).hexdigest()
    return {
        "version": 1,
        "review_key": {"project_id": "42", "mr_iid": 1, "sha": SHA},
        "intent": {"generation": 1, "nonce": NONCE},
        "proposal_summary": "Apply fix",
        "candidate_sha256": candidate_sha256,
        "actions": actions,
        "verification": {
            "candidate_sha256": candidate_sha256,
            "command_sha256": "d" * 64,
        },
        "branch": f"autofix/MR-1/sha-{SHA}/intent-{NONCE}",
        "marker_template": (
            f"<!-- reviewflow:v1;project=42;mr=1;sha={SHA};effect=autofix;"
            f"intent={NONCE};plan_sha256={{plan_sha256}} -->"
        ),
        "commit_message": f"ReviewFlow Auto-fix for !1 at {SHA}\n\n[skip ci]",
        "title": "Draft: [ReviewFlow Auto-Fix] Parent",
        "target_branch": "feature",
        "initial_readiness": "draft",
        "ci_trigger": "manual",
    }


def test_publication_plan_v1_parses_one_canonical_typed_contract() -> None:
    plan_json = canonical_json(_plan_value())
    digest = hashlib.sha256(plan_json.encode()).hexdigest()

    plan = PublicationPlanV1.from_json(plan_json, digest)

    assert plan.review_key.project_id == "42"
    assert plan.review_key.sha == SHA
    assert plan.intent.nonce == NONCE
    assert plan.actions[0]["file_path"] == "README.md"
    assert plan.plan_sha256 == digest
    assert plan.marker.endswith(f"plan_sha256={digest} -->")


def test_publication_plan_v1_rejects_noncanonical_or_mismatched_authority() -> None:
    value = _plan_value()
    pretty = json.dumps(value, indent=2)

    with pytest.raises(PublicationPlanError, match="canonical"):
        PublicationPlanV1.from_json(pretty)
    with pytest.raises(PublicationPlanError, match="digest"):
        PublicationPlanV1.from_json(canonical_json(value), "e" * 64)


def test_publication_plan_v1_rejects_duplicate_action_paths() -> None:
    value = _plan_value()
    actions = value["actions"]
    assert isinstance(actions, list)
    value["actions"] = [*actions, *actions]

    with pytest.raises(PublicationPlanError, match="unique sorted paths"):
        PublicationPlanV1.from_value(value)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "dir\\file"])
def test_publication_plan_v1_rejects_noncanonical_action_paths(path: str) -> None:
    value = _plan_value()
    value["actions"] = [
        {
            "action": "update",
            "file_path": path,
            "content": "fixed\n",
            "execute_filemode": False,
        }
    ]

    with pytest.raises(PublicationPlanError, match="path is not canonical"):
        PublicationPlanV1.from_value(value)


@pytest.mark.parametrize(
    ("action", "message"),
    [
        ({"action": "rename", "file_path": "README.md"}, "type is unsupported"),
        (
            {"action": "delete", "file_path": "README.md", "content": ""},
            "fields are not canonical",
        ),
        (
            {"action": "chmod", "file_path": "README.md", "execute_filemode": 1},
            "requires an executable mode",
        ),
        (
            {
                "action": "update",
                "file_path": "README.md",
                "content": "nul\0content",
                "execute_filemode": False,
            },
            "exceeds the text envelope",
        ),
    ],
)
def test_publication_plan_v1_rejects_malformed_actions(
    action: dict[str, object], message: str
) -> None:
    value = _plan_value()
    value["actions"] = [action]

    with pytest.raises(PublicationPlanError, match=message):
        PublicationPlanV1.from_value(value)


def test_publication_plan_v1_enforces_action_and_content_bounds() -> None:
    value = _plan_value()
    value["actions"] = []
    with pytest.raises(PublicationPlanError, match="outside the bounded envelope"):
        PublicationPlanV1.from_value(value)

    value["actions"] = [
        {"action": "delete", "file_path": f"file-{index:03d}.txt"}
        for index in range(MAX_PUBLICATION_ACTIONS + 1)
    ]
    with pytest.raises(PublicationPlanError, match="outside the bounded envelope"):
        PublicationPlanV1.from_value(value)

    value["actions"] = [
        {
            "action": "update",
            "file_path": "oversized.txt",
            "content": "x" * (MAX_PUBLICATION_FILE_BYTES + 1),
            "execute_filemode": False,
        }
    ]
    with pytest.raises(PublicationPlanError, match="exceeds the text envelope"):
        PublicationPlanV1.from_value(value)

    value["actions"] = [
        {
            "action": "update",
            "file_path": f"file-{index}.txt",
            "content": "x" * MAX_PUBLICATION_FILE_BYTES,
            "execute_filemode": False,
        }
        for index in range(6)
    ]
    with pytest.raises(PublicationPlanError, match="aggregate text bound"):
        PublicationPlanV1.from_value(value)


def test_publication_plan_v1_rejects_unparseable_marker_identity() -> None:
    value = _plan_value()
    value["review_key"] = {"project_id": "42;forged", "mr_iid": 1, "sha": SHA}
    value["marker_template"] = (
        f"<!-- reviewflow:v1;project=42;forged;mr=1;sha={SHA};effect=autofix;"
        f"intent={NONCE};plan_sha256={{plan_sha256}} -->"
    )

    with pytest.raises(PublicationPlanError, match="branch or marker identity"):
        PublicationPlanV1.from_value(value)

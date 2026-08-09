"""Typed, canonical Publication Plan v1 shared by every trusted consumer."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .events import parse_auto_fix_branch, parse_auto_fix_marker
from .gitlab import is_valid_sha, normalize_sha

MAX_PUBLICATION_ACTIONS = 100
MAX_PUBLICATION_FILE_BYTES = 1024 * 1024
MAX_PUBLICATION_TOTAL_BYTES = 5 * 1024 * 1024
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_PLAN_KEYS = {
    "version",
    "review_key",
    "intent",
    "proposal_summary",
    "candidate_sha256",
    "actions",
    "verification",
    "branch",
    "marker_template",
    "commit_message",
    "title",
    "target_branch",
    "initial_readiness",
    "ci_trigger",
}


class PublicationPlanError(ValueError):
    """Raised when persisted Publication Plan authority is not canonical v1."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PublicationReviewKey:
    project_id: str
    mr_iid: int
    sha: str


@dataclass(frozen=True)
class PublicationIntent:
    generation: int
    nonce: str


@dataclass(frozen=True)
class PublicationVerification:
    candidate_sha256: str
    command_sha256: str


@dataclass(frozen=True)
class PublicationPlanV1:
    review_key: PublicationReviewKey
    intent: PublicationIntent
    proposal_summary: str
    candidate_sha256: str
    actions: tuple[dict[str, object], ...]
    verification: PublicationVerification
    branch: str
    marker_template: str
    commit_message: str
    title: str
    target_branch: str
    initial_readiness: str
    ci_trigger: str
    canonical_json: str
    plan_sha256: str

    @property
    def marker(self) -> str:
        return self.marker_template.replace("{plan_sha256}", self.plan_sha256)

    @classmethod
    def from_json(cls, plan_json: str, expected_sha256: str | None = None) -> PublicationPlanV1:
        try:
            value = json.loads(plan_json)
        except json.JSONDecodeError as exc:
            raise PublicationPlanError("Publication Plan is not valid JSON") from exc
        if canonical_json(value) != plan_json:
            raise PublicationPlanError("Publication Plan must use canonical JSON")
        plan = cls.from_value(value)
        if expected_sha256 is not None and (
            _DIGEST_RE.fullmatch(expected_sha256) is None or plan.plan_sha256 != expected_sha256
        ):
            raise PublicationPlanError("Publication Plan digest does not match the exact payload")
        return plan

    @classmethod
    def from_value(cls, value: object) -> PublicationPlanV1:
        if not isinstance(value, dict) or set(value) != _PLAN_KEYS or value.get("version") != 1:
            raise PublicationPlanError("Publication Plan fields do not match version 1")

        review_key_value = value.get("review_key")
        if not isinstance(review_key_value, dict) or set(review_key_value) != {
            "project_id",
            "mr_iid",
            "sha",
        }:
            raise PublicationPlanError("Publication Plan ReviewKey is invalid")
        project_id = review_key_value.get("project_id")
        mr_iid = review_key_value.get("mr_iid")
        sha = review_key_value.get("sha")
        if (
            not isinstance(project_id, str)
            or not project_id.strip()
            or type(mr_iid) is not int
            or mr_iid <= 0
            or not is_valid_sha(sha)
            or sha != normalize_sha(sha)
        ):
            raise PublicationPlanError("Publication Plan ReviewKey is invalid")
        review_key = PublicationReviewKey(project_id, mr_iid, sha)

        intent_value = value.get("intent")
        if not isinstance(intent_value, dict) or set(intent_value) != {"generation", "nonce"}:
            raise PublicationPlanError("Publication Plan Intent is invalid")
        generation = intent_value.get("generation")
        nonce = intent_value.get("nonce")
        if (
            type(generation) is not int
            or generation <= 0
            or not isinstance(nonce, str)
            or _NONCE_RE.fullmatch(nonce) is None
        ):
            raise PublicationPlanError("Publication Plan Intent is invalid")
        intent = PublicationIntent(generation, nonce)

        actions = cls._actions(value.get("actions"))
        candidate_sha256 = value.get("candidate_sha256")
        candidate_value = {
            "version": 1,
            "base_sha": review_key.sha,
            "actions": list(actions),
        }
        if (
            not isinstance(candidate_sha256, str)
            or _DIGEST_RE.fullmatch(candidate_sha256) is None
            or hashlib.sha256(canonical_json(candidate_value).encode("utf-8")).hexdigest()
            != candidate_sha256
        ):
            raise PublicationPlanError("Publication Plan candidate digest is invalid")

        verification_value = value.get("verification")
        if not isinstance(verification_value, dict) or set(verification_value) != {
            "candidate_sha256",
            "command_sha256",
        }:
            raise PublicationPlanError("Publication Plan verification evidence is invalid")
        command_sha256 = verification_value.get("command_sha256")
        if (
            verification_value.get("candidate_sha256") != candidate_sha256
            or not isinstance(command_sha256, str)
            or _DIGEST_RE.fullmatch(command_sha256) is None
        ):
            raise PublicationPlanError("Publication Plan verification evidence is invalid")
        verification = PublicationVerification(candidate_sha256, command_sha256)

        branch = f"autofix/MR-{mr_iid}/sha-{sha}/intent-{nonce}"
        marker_template = (
            f"<!-- reviewflow:v1;project={project_id};mr={mr_iid};sha={sha};"
            f"effect=autofix;intent={nonce};plan_sha256={{plan_sha256}} -->"
        )
        serialized = canonical_json(value)
        plan_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        marker = marker_template.replace("{plan_sha256}", plan_sha256)
        if (
            value.get("branch") != branch
            or value.get("marker_template") != marker_template
            or parse_auto_fix_branch(branch) != (mr_iid, sha, nonce)
            or parse_auto_fix_marker(marker) != (project_id, mr_iid, sha, nonce, plan_sha256)
        ):
            raise PublicationPlanError("Publication Plan branch or marker identity is invalid")

        proposal_summary = value.get("proposal_summary")
        commit_message = value.get("commit_message")
        title = value.get("title")
        target_branch = value.get("target_branch")
        initial_readiness = value.get("initial_readiness")
        ci_trigger = value.get("ci_trigger")
        if (
            not isinstance(proposal_summary, str)
            or not isinstance(commit_message, str)
            or not commit_message.strip()
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(target_branch, str)
            or not target_branch.strip()
            or initial_readiness not in {"draft", "ready"}
            or ci_trigger not in {"manual", "automatic"}
            or (initial_readiness == "ready" and ci_trigger == "manual")
        ):
            raise PublicationPlanError("Publication Plan publication settings are invalid")

        return cls(
            review_key,
            intent,
            proposal_summary,
            candidate_sha256,
            actions,
            verification,
            branch,
            marker_template,
            commit_message,
            title,
            target_branch,
            initial_readiness,
            ci_trigger,
            serialized,
            plan_sha256,
        )

    @staticmethod
    def _actions(value: object) -> tuple[dict[str, object], ...]:
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_PUBLICATION_ACTIONS:
            raise PublicationPlanError("Publication Plan actions are outside the bounded envelope")
        actions: list[dict[str, object]] = []
        paths: list[str] = []
        total_bytes = 0
        for raw in value:
            if not isinstance(raw, dict):
                raise PublicationPlanError("Publication Plan action must be an object")
            action = raw.get("action")
            path_value = raw.get("file_path")
            if not isinstance(path_value, str):
                raise PublicationPlanError("Publication Plan action path must be text")
            path = PurePosixPath(path_value)
            if (
                not path_value
                or str(path) != path_value
                or path.is_absolute()
                or "\\" in path_value
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise PublicationPlanError("Publication Plan action path is not canonical")
            if action == "delete":
                expected_keys = {"action", "file_path"}
            elif action == "chmod":
                expected_keys = {"action", "file_path", "execute_filemode"}
                if type(raw.get("execute_filemode")) is not bool:
                    raise PublicationPlanError("chmod action requires an executable mode")
            elif action in {"create", "update"}:
                expected_keys = {"action", "file_path", "content", "execute_filemode"}
                content = raw.get("content")
                if not isinstance(content, str) or type(raw.get("execute_filemode")) is not bool:
                    raise PublicationPlanError("content action has an invalid payload")
                content_bytes = content.encode("utf-8")
                if len(content_bytes) > MAX_PUBLICATION_FILE_BYTES or "\0" in content:
                    raise PublicationPlanError("content action exceeds the text envelope")
                total_bytes += len(content_bytes)
            else:
                raise PublicationPlanError("Publication Plan action type is unsupported")
            if set(raw) != expected_keys:
                raise PublicationPlanError("Publication Plan action fields are not canonical")
            paths.append(path_value)
            actions.append(dict(raw))
        if paths != sorted(paths) or len(set(paths)) != len(paths):
            raise PublicationPlanError("Publication Plan actions must have unique sorted paths")
        if total_bytes > MAX_PUBLICATION_TOTAL_BYTES:
            raise PublicationPlanError("Publication Plan actions exceed the aggregate text bound")
        return tuple(actions)

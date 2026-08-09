"""Project policy loading and safe defaults."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

POLICY_SCHEMA_VERSION = 4
MAX_POLICY_BYTES = 256 * 1024


class PolicyError(ValueError):
    """Raised when a project policy is invalid."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Reject duplicate authority keys instead of accepting last-key-wins YAML."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise PolicyError("policy mapping keys must be scalar") from exc
        if duplicate:
            raise PolicyError(f"policy contains duplicate key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class HumanPolicy:
    enabled: bool = True
    reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AIPolicy:
    enabled: bool = False
    backend: str | None = None
    profile: str | None = None
    skill_path: str | None = None
    auto_fix: bool = False


@dataclass(frozen=True)
class NotificationPolicy:
    channels: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewPolicy:
    version: int = POLICY_SCHEMA_VERSION
    pipeline_gate: bool = False
    human: HumanPolicy = field(default_factory=HumanPolicy)
    ai: AIPolicy = field(default_factory=AIPolicy)
    notifications: NotificationPolicy = field(default_factory=NotificationPolicy)


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise PolicyError(f"{field_name} contains unknown fields: {', '.join(unknown)}")


def _as_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{field_name} must be a mapping")
    return value


def _as_bool(value: Any, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PolicyError(f"{field_name} must be a boolean")
    return value


def _as_reviewers(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise PolicyError("human.reviewers must be a list of non-empty strings")
    return tuple(value)


def _as_channels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item and item.strip() == item for item in value
    ):
        raise PolicyError("notifications.channels must be a list of non-empty channel IDs")
    channels = tuple(value)
    if len(set(channels)) != len(channels):
        raise PolicyError("notifications.channels ids must be unique")
    return channels


def _as_optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{field_name} must be a non-empty string")
    return value


def _as_skill_path(value: Any) -> str | None:
    path = _as_optional_string(value, "ai.skill")
    if path is None:
        return None
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise PolicyError("ai.skill must be a repository-relative path without '..'")
    return path


def parse_policy(text: str) -> ReviewPolicy:
    """Parse untrusted Project policy text using the current strict schema."""

    if len(text.encode("utf-8")) > MAX_POLICY_BYTES:
        raise PolicyError(f"Project Policy exceeds {MAX_POLICY_BYTES} bytes")
    try:
        loaded = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise PolicyError(f"invalid policy YAML: {exc}") from exc
    raw = {} if loaded is None else loaded
    if not isinstance(raw, dict):
        raise PolicyError("policy root must be a mapping")
    _reject_unknown(
        raw,
        {"version", "pipeline_gate", "human", "ai", "notifications"},
        "policy",
    )
    version = raw.get("version", POLICY_SCHEMA_VERSION)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != POLICY_SCHEMA_VERSION
    ):
        raise PolicyError(f"unsupported policy version: {version}")

    human = _as_mapping(raw.get("human"), "human")
    ai = _as_mapping(raw.get("ai"), "ai")
    notifications = _as_mapping(raw.get("notifications"), "notifications")
    _reject_unknown(human, {"enabled", "reviewers"}, "human")
    _reject_unknown(ai, {"enabled", "backend", "profile", "skill", "auto_fix"}, "ai")
    _reject_unknown(notifications, {"channels"}, "notifications")
    channels = _as_channels(notifications.get("channels", []))

    ai_policy = AIPolicy(
        enabled=_as_bool(ai.get("enabled"), "ai.enabled", False),
        backend=_as_optional_string(ai.get("backend"), "ai.backend"),
        profile=_as_optional_string(ai.get("profile"), "ai.profile"),
        skill_path=_as_skill_path(ai.get("skill")),
        auto_fix=_as_bool(ai.get("auto_fix"), "ai.auto_fix", False),
    )
    if ai_policy.enabled and (ai_policy.backend is None or ai_policy.profile is None):
        raise PolicyError("ai.backend and ai.profile are required when ai.enabled is true")
    if ai_policy.auto_fix and not ai_policy.enabled:
        raise PolicyError("ai.auto_fix requires ai.enabled to be true")

    return ReviewPolicy(
        version=version,
        pipeline_gate=_as_bool(raw.get("pipeline_gate"), "pipeline_gate", False),
        human=HumanPolicy(
            enabled=_as_bool(human.get("enabled"), "human.enabled", True),
            reviewers=_as_reviewers(human.get("reviewers")),
        ),
        ai=ai_policy,
        notifications=NotificationPolicy(channels=channels),
    )


def load_policy(path: str | Path | None = None) -> ReviewPolicy:
    """Load an optional Project policy, falling back to safe defaults."""

    policy_path = Path(path) if path is not None else None
    if policy_path is None or not policy_path.exists():
        return ReviewPolicy()
    if not policy_path.is_file():
        raise PolicyError(f"policy path is not a file: {policy_path}")
    return parse_policy(policy_path.read_text(encoding="utf-8"))


def policy_json(policy: ReviewPolicy) -> str:
    """Return the canonical credential-free representation persisted per Run."""

    value = {
        "version": policy.version,
        "pipeline_gate": policy.pipeline_gate,
        "human": {
            "enabled": policy.human.enabled,
            "reviewers": list(policy.human.reviewers),
        },
        "ai": {
            "enabled": policy.ai.enabled,
            "backend": policy.ai.backend,
            "profile": policy.ai.profile,
            "skill": policy.ai.skill_path,
            "auto_fix": policy.ai.auto_fix,
        },
        "notifications": {"channels": list(policy.notifications.channels)},
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def policy_from_json(value: str) -> ReviewPolicy:
    """Rehydrate a validated immutable Policy Snapshot."""

    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PolicyError("stored policy snapshot is invalid JSON") from exc
    return parse_policy(yaml.safe_dump(loaded, sort_keys=False))


def policy_template() -> str:
    """Return the starter policy generated by ``review-agent-flow init``."""

    return f"""version: {POLICY_SCHEMA_VERSION}

# Wait for the latest successful pipeline before starting a Review Run.
pipeline_gate: true

human:
  enabled: true
  reviewers:
    - gitlab-username

# AI and auto-fix are opt-in safe defaults.
ai:
  enabled: false
  backend: local-agent
  profile: default
  skill: .gitlab/review-agent/SKILL.md
  auto_fix: false

notifications:
  channels:
    - feishu-reviewers
    - wecom-reviewers
"""

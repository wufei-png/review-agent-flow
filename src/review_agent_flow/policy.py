"""Project policy loading and safe defaults."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

POLICY_SCHEMA_VERSION = 2


class PolicyError(ValueError):
    """Raised when a project policy is invalid."""


@dataclass(frozen=True)
class HumanPolicy:
    enabled: bool = True
    reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AIPolicy:
    enabled: bool = False
    command: tuple[str, ...] = ()
    skill_path: str | None = None
    auto_fix: bool = False


@dataclass(frozen=True)
class NotificationPolicy:
    channels: tuple[dict[str, Any], ...] = ()


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


def _as_command(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        command = tuple(shlex.split(value))
    elif isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        command = tuple(value)
    else:
        raise PolicyError("ai.command must be a string or a list of strings")
    if not command:
        raise PolicyError("ai.command cannot be empty when provided")
    return command


def _as_reviewers(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise PolicyError("human.reviewers must be a list of non-empty strings")
    return tuple(value)


def _as_channels(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise PolicyError("notifications.channels must be a list of mappings")
    channels: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise PolicyError(f"notifications.channels[{index}] must be a mapping")
        _reject_unknown(
            item,
            {"id", "type", "webhook_env", "user_ids"},
            f"notifications.channels[{index}]",
        )
        channel_id = item.get("id")
        if not isinstance(channel_id, str) or not channel_id:
            raise PolicyError(
                f"notifications.channels[{index}].id must be a non-empty string"
            )
        channel_type = item.get("type")
        if channel_type not in {"feishu", "wecom"}:
            raise PolicyError(
                f"notifications.channels[{index}].type must be 'feishu' or 'wecom'"
            )
        webhook_env = item.get("webhook_env")
        if not isinstance(webhook_env, str) or not webhook_env:
            raise PolicyError(
                f"notifications.channels[{index}].webhook_env must be a non-empty string"
            )
        user_ids = item.get("user_ids", {})
        if not isinstance(user_ids, dict) or not all(
            isinstance(key, str)
            and key
            and isinstance(user_id, str)
            and user_id
            for key, user_id in user_ids.items()
        ):
            raise PolicyError(
                f"notifications.channels[{index}].user_ids must map non-empty strings"
            )
        channels.append(item)
    channel_ids = [str(channel["id"]) for channel in channels]
    if len(set(channel_ids)) != len(channel_ids):
        raise PolicyError("notifications.channels ids must be unique")
    return tuple(channels)


def _as_optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{field_name} must be a non-empty string")
    return value


def load_policy(path: str | Path | None = None) -> ReviewPolicy:
    """Load an optional Project policy, falling back to safe defaults."""

    policy_path = Path(path) if path is not None else None
    if policy_path is None:
        return ReviewPolicy()
    if not policy_path.exists():
        return ReviewPolicy()
    if not policy_path.is_file():
        raise PolicyError(f"policy path is not a file: {policy_path}")

    loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
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
    _reject_unknown(ai, {"enabled", "command", "skill", "auto_fix"}, "ai")
    _reject_unknown(notifications, {"channels"}, "notifications")
    channels = _as_channels(notifications.get("channels", []))

    ai_policy = AIPolicy(
        enabled=_as_bool(ai.get("enabled"), "ai.enabled", False),
        command=_as_command(ai.get("command")),
        skill_path=_as_optional_string(ai.get("skill"), "ai.skill"),
        auto_fix=_as_bool(ai.get("auto_fix"), "ai.auto_fix", False),
    )
    if ai_policy.enabled and not ai_policy.command:
        raise PolicyError("ai.command is required when ai.enabled is true")
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
  command:
    - opencode
    - run
  skill: .gitlab/review-agent/SKILL.md
  auto_fix: false

notifications:
  channels:
    - id: feishu-reviewers
      type: feishu
      webhook_env: FEISHU_WEBHOOK
    - id: wecom-reviewers
      type: wecom
      webhook_env: WECOM_WEBHOOK
"""

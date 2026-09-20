"""Strict Operator-owned execution and publication configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .backend_adapter import OPENCODE_ADAPTER, SUPPORTED_BACKEND_ADAPTERS
from .policy import AIPolicy

OPERATOR_CONFIG_VERSION = 4
MAX_BACKEND_TIMEOUT_SECONDS = 1800
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN_AGENT_ENVIRONMENT = {
    "GITLAB_TOKEN",
    "GITLAB_WEBHOOK_SECRET",
    "GITLAB_REVIEW_BOT_USERNAME",
    "FEISHU_WEBHOOK",
    "WECOM_WEBHOOK",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_CONFIG_DIR",
}


class OperatorConfigError(ValueError):
    """Raised when trusted deployment configuration is invalid or denies a request."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects last-key-wins authority changes."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise OperatorConfigError("operator config mapping keys must be scalar") from exc
        if duplicate:
            raise OperatorConfigError(f"operator config contains duplicate key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class VerificationProfile:
    command: tuple[str, ...]


@dataclass(frozen=True)
class BackendProfile:
    timeout_seconds: int
    environment: tuple[str, ...]
    verification: VerificationProfile | None = None
    model: str | None = None


@dataclass(frozen=True)
class AgentBackend:
    enabled: bool
    adapter: str
    command: tuple[str, ...]
    allowed_projects: frozenset[str]
    profiles: Mapping[str, BackendProfile]


@dataclass(frozen=True)
class AutoFixPublicationConfig:
    enabled: bool = False
    allowed_projects: frozenset[str] = frozenset()
    initial_readiness: str = "draft"
    ci_trigger: str = "manual"


@dataclass(frozen=True)
class NotificationChannelConfig:
    channel_type: str
    webhook_env: str | None
    allowed_projects: frozenset[str]
    user_ids: Mapping[str, str]
    app_id_env: str | None = None
    app_secret_env: str | None = None
    recipient_open_id_env: str | None = None
    recipient_username: str | None = None


@dataclass(frozen=True)
class RetentionConfig:
    sensitive_payload_days: int = 30
    publication_plan_backup_dir: Path | None = None


@dataclass(frozen=True)
class ResolvedNotificationChannel:
    channel_id: str
    channel_type: str
    webhook_url: str | None
    user_ids: Mapping[str, str]
    app_id: str | None = None
    app_secret: str | None = None
    recipient_open_id: str | None = None
    recipient_username: str | None = None


@dataclass(frozen=True)
class OperatorConfig:
    version: int
    backends: Mapping[str, AgentBackend]
    autofix_publication: AutoFixPublicationConfig
    notification_channels: Mapping[str, NotificationChannelConfig]
    retention: RetentionConfig


@dataclass(frozen=True)
class ResolvedBackend:
    name: str
    profile_name: str
    adapter: str
    command: tuple[str, ...]
    timeout_seconds: int
    environment: Mapping[str, str]
    verification: VerificationProfile | None
    model: str | None = None


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperatorConfigError(f"{name} must be a mapping")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise OperatorConfigError(f"{name} contains unknown fields: {', '.join(unknown)}")


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise OperatorConfigError(f"{field} must be a non-empty trimmed string")
    return value


def _command(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise OperatorConfigError(f"{field} must be a non-empty list of strings")
    return tuple(_name(item, field) for item in value)


def _environment_name(value: Any) -> str:
    name = _name(value, "environment entry")
    if _ENVIRONMENT_NAME_RE.fullmatch(name) is None:
        raise OperatorConfigError(f"invalid environment variable name: {name}")
    if name.startswith("REVIEW_AGENT_FLOW_") or name in _FORBIDDEN_AGENT_ENVIRONMENT:
        raise OperatorConfigError(f"Agent environment cannot expose reserved credential: {name}")
    return name


def _notification_environment_name(value: Any) -> str:
    name = _name(value, "notification environment")
    if _ENVIRONMENT_NAME_RE.fullmatch(name) is None:
        raise OperatorConfigError(f"invalid environment variable name: {name}")
    return name


def _projects(value: Any, field: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise OperatorConfigError(f"{field} must be a list")
    projects = frozenset(_name(item, field) for item in value)
    if len(projects) != len(value):
        raise OperatorConfigError(f"{field} must not contain duplicates")
    return projects


def _parse_backend_profiles(
    value: Any,
    backend_name: str,
    adapter: str,
) -> dict[str, BackendProfile]:
    profile_values = _mapping(value, f"backends.{backend_name}.profiles")
    profiles: dict[str, BackendProfile] = {}
    for profile_name, profile_value in profile_values.items():
        profile_name = _name(profile_name, "profile name")
        profile = _mapping(profile_value, f"profiles.{profile_name}")
        _reject_unknown(
            profile,
            {"timeout_seconds", "environment", "verification", "model"},
            f"profiles.{profile_name}",
        )
        timeout = profile.get("timeout_seconds", 1800)
        if type(timeout) is not int or timeout <= 0 or timeout > MAX_BACKEND_TIMEOUT_SECONDS:
            raise OperatorConfigError(
                f"profiles.{profile_name}.timeout_seconds must be between 1 and "
                f"{MAX_BACKEND_TIMEOUT_SECONDS}"
            )
        environment_value = profile.get("environment", [])
        if not isinstance(environment_value, list):
            raise OperatorConfigError(f"profiles.{profile_name}.environment must be a list")
        environment = tuple(_environment_name(item) for item in environment_value)
        if len(set(environment)) != len(environment):
            raise OperatorConfigError(f"profiles.{profile_name}.environment has duplicates")
        verification_value = profile.get("verification")
        model_value = profile.get("model")
        model = (
            _name(model_value, f"profiles.{profile_name}.model")
            if model_value is not None
            else None
        )
        if model is not None and adapter != OPENCODE_ADAPTER:
            raise OperatorConfigError(
                f"profiles.{profile_name}.model is supported only by the OpenCode adapter"
            )
        verification = None
        if verification_value is not None:
            verification_map = _mapping(verification_value, "verification")
            _reject_unknown(verification_map, {"command"}, "verification")
            verification = VerificationProfile(
                _command(verification_map.get("command"), "verification.command")
            )
        profiles[profile_name] = BackendProfile(timeout, environment, verification, model)
    if not profiles:
        raise OperatorConfigError(f"backends.{backend_name}.profiles cannot be empty")
    return profiles


def load_operator_config(path: str | Path) -> OperatorConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise OperatorConfigError(f"operator config is not a file: {config_path}")
    try:
        loaded = yaml.load(
            config_path.read_text(encoding="utf-8"),
            Loader=_UniqueKeySafeLoader,
        )
    except yaml.YAMLError as exc:
        raise OperatorConfigError(f"invalid operator config YAML: {exc}") from exc
    root = _mapping(loaded, "operator config")
    _reject_unknown(
        root,
        {"version", "backends", "autofix_publication", "notifications", "retention"},
        "operator config",
    )
    version = root.get("version")
    if type(version) is not int or version != OPERATOR_CONFIG_VERSION:
        raise OperatorConfigError(f"unsupported operator config version: {version}")

    backend_values = _mapping(root.get("backends"), "backends")
    backends: dict[str, AgentBackend] = {}
    for backend_name, backend_value in backend_values.items():
        backend_name = _name(backend_name, "backend name")
        backend = _mapping(backend_value, f"backends.{backend_name}")
        _reject_unknown(
            backend,
            {"enabled", "adapter", "command", "allowed_projects", "profiles"},
            f"backends.{backend_name}",
        )
        backend_enabled = backend.get("enabled", True)
        if not isinstance(backend_enabled, bool):
            raise OperatorConfigError(f"backends.{backend_name}.enabled must be a boolean")
        adapter = backend.get("adapter")
        if adapter not in SUPPORTED_BACKEND_ADAPTERS:
            raise OperatorConfigError(
                f"backends.{backend_name}.adapter must be one of: "
                + ", ".join(sorted(SUPPORTED_BACKEND_ADAPTERS))
            )
        command = _command(backend.get("command"), f"backends.{backend_name}.command")
        if adapter == OPENCODE_ADAPTER and len(command) != 1:
            raise OperatorConfigError(
                f"backends.{backend_name}.command must contain only the OpenCode executable"
            )
        profiles = _parse_backend_profiles(backend.get("profiles"), backend_name, adapter)
        backends[backend_name] = AgentBackend(
            backend_enabled,
            adapter,
            command,
            _projects(backend.get("allowed_projects"), f"backends.{backend_name}.allowed_projects"),
            MappingProxyType(profiles),
        )

    publication = _mapping(root.get("autofix_publication", {}), "autofix_publication")
    _reject_unknown(
        publication,
        {"enabled", "allowed_projects", "initial_readiness", "ci_trigger"},
        "autofix_publication",
    )
    enabled = publication.get("enabled", False)
    if not isinstance(enabled, bool):
        raise OperatorConfigError("autofix_publication.enabled must be a boolean")
    readiness = publication.get("initial_readiness", "draft")
    ci_trigger = publication.get("ci_trigger", "manual")
    if readiness not in {"draft", "ready"}:
        raise OperatorConfigError("initial_readiness must be draft or ready")
    if ci_trigger not in {"manual", "automatic"}:
        raise OperatorConfigError("ci_trigger must be manual or automatic")
    if readiness == "ready" and ci_trigger == "manual":
        raise OperatorConfigError("ready/manual Auto-fix publication is not supported")
    notification_root = _mapping(root.get("notifications", {}), "notifications")
    _reject_unknown(notification_root, {"channels"}, "notifications")
    notification_values = _mapping(notification_root.get("channels", {}), "notifications.channels")
    notification_channels: dict[str, NotificationChannelConfig] = {}
    for channel_id, channel_value in notification_values.items():
        channel_id = _name(channel_id, "notification channel ID")
        channel = _mapping(channel_value, f"notifications.channels.{channel_id}")
        channel_type = channel.get("type")
        if channel_type not in {"feishu", "wecom", "feishu_dm"}:
            raise OperatorConfigError(
                f"notifications.channels.{channel_id}.type must be feishu, wecom or feishu_dm"
            )
        dm = channel_type == "feishu_dm"
        _reject_unknown(
            channel,
            (
                {
                    "type",
                    "allowed_projects",
                    "app_id_env",
                    "app_secret_env",
                    "recipient_open_id_env",
                    "recipient_username",
                }
                if dm
                else {"type", "webhook_env", "allowed_projects", "user_ids"}
            ),
            f"notifications.channels.{channel_id}",
        )
        webhook_env = None if dm else _notification_environment_name(channel.get("webhook_env"))
        app_id_env = _notification_environment_name(channel.get("app_id_env")) if dm else None
        app_secret_env = (
            _notification_environment_name(channel.get("app_secret_env")) if dm else None
        )
        recipient_open_id_env = (
            _notification_environment_name(channel.get("recipient_open_id_env")) if dm else None
        )
        recipient_username = (
            _name(channel.get("recipient_username"), "recipient_username") if dm else None
        )
        if dm and len({app_id_env, app_secret_env, recipient_open_id_env}) != 3:
            raise OperatorConfigError(
                f"notifications.channels.{channel_id} environment names must differ"
            )
        allowed_projects = _projects(
            channel.get("allowed_projects", []),
            f"notifications.channels.{channel_id}.allowed_projects",
        )
        user_ids_value = _mapping(
            channel.get("user_ids", {}),
            f"notifications.channels.{channel_id}.user_ids",
        )
        user_ids = {
            _name(username, f"notifications.channels.{channel_id}.user_ids key"): _name(
                user_id, f"notifications.channels.{channel_id}.user_ids value"
            )
            for username, user_id in user_ids_value.items()
        }
        notification_channels[channel_id] = NotificationChannelConfig(
            channel_type,
            webhook_env,
            allowed_projects,
            MappingProxyType(user_ids),
            app_id_env,
            app_secret_env,
            recipient_open_id_env,
            recipient_username,
        )

    notification_credentials = {
        name
        for channel in notification_channels.values()
        for name in (
            channel.webhook_env,
            channel.app_id_env,
            channel.app_secret_env,
            channel.recipient_open_id_env,
        )
        if name is not None
    }
    for backend_name, backend in backends.items():
        for profile_name, profile in backend.profiles.items():
            exposed = sorted(set(profile.environment) & notification_credentials)
            if exposed:
                raise OperatorConfigError(
                    "Agent environment overlaps Operator notification credentials "
                    f"for {backend_name}/{profile_name}: {', '.join(exposed)}"
                )

    retention_value = _mapping(root.get("retention", {}), "retention")
    _reject_unknown(
        retention_value,
        {"sensitive_payload_days", "publication_plan_backup_dir"},
        "retention",
    )
    sensitive_payload_days = retention_value.get("sensitive_payload_days", 30)
    if (
        type(sensitive_payload_days) is not int
        or sensitive_payload_days <= 0
        or sensitive_payload_days > 3650
    ):
        raise OperatorConfigError("retention.sensitive_payload_days must be between 1 and 3650")
    backup_value = retention_value.get("publication_plan_backup_dir")
    backup_dir = None
    if backup_value is not None:
        backup_path = Path(_name(backup_value, "publication_plan_backup_dir"))
        if not backup_path.is_absolute():
            raise OperatorConfigError("retention.publication_plan_backup_dir must be absolute")
        backup_dir = backup_path.resolve()

    return OperatorConfig(
        version,
        MappingProxyType(backends),
        AutoFixPublicationConfig(
            enabled,
            _projects(
                publication.get("allowed_projects", []), "autofix_publication.allowed_projects"
            ),
            readiness,
            ci_trigger,
        ),
        MappingProxyType(notification_channels),
        RetentionConfig(sensitive_payload_days, backup_dir),
    )


def resolve_notification_channels(
    config: OperatorConfig,
    requested_channel_ids: Iterable[str],
    project_id: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[ResolvedNotificationChannel, ...]:
    """Resolve Project-requested channel IDs through trusted Operator bindings."""

    requested = tuple(requested_channel_ids)
    if len(set(requested)) != len(requested):
        raise OperatorConfigError("requested notification channel IDs must be unique")
    source = os.environ if environ is None else environ
    resolved: list[ResolvedNotificationChannel] = []
    for channel_id in requested:
        channel = config.notification_channels.get(channel_id)
        if channel is None:
            raise OperatorConfigError(
                f"notification channel is not Operator-authorized: {channel_id}"
            )
        if project_id not in channel.allowed_projects:
            raise OperatorConfigError(
                f"notification channel {channel_id} does not allow Project {project_id}"
            )
        if channel.channel_type == "feishu_dm":
            assert channel.app_id_env and channel.app_secret_env and channel.recipient_open_id_env
            app_id = source.get(channel.app_id_env)
            app_secret = source.get(channel.app_secret_env)
            recipient_open_id = source.get(channel.recipient_open_id_env)
            if not app_id or not app_secret or not recipient_open_id:
                raise OperatorConfigError(
                    f"notification channel {channel_id} requires app and recipient credentials"
                )
            if not recipient_open_id.startswith("ou_"):
                raise OperatorConfigError(
                    f"notification channel {channel_id} requires a Feishu open_id"
                )
            resolved.append(
                ResolvedNotificationChannel(
                    channel_id,
                    channel.channel_type,
                    None,
                    channel.user_ids,
                    app_id,
                    app_secret,
                    recipient_open_id,
                    channel.recipient_username,
                )
            )
            continue
        assert channel.webhook_env is not None
        webhook_url = source.get(channel.webhook_env)
        if not webhook_url:
            raise OperatorConfigError(
                f"notification webhook environment variable is missing: {channel.webhook_env}"
            )
        if not webhook_url.startswith("https://"):
            raise OperatorConfigError(
                f"notification channel {channel_id} requires an https webhook URL"
            )
        resolved.append(
            ResolvedNotificationChannel(
                channel_id,
                channel.channel_type,
                webhook_url,
                channel.user_ids,
            )
        )
    return tuple(resolved)


def resolve_backend(
    config: OperatorConfig,
    policy: AIPolicy,
    project_id: str,
    environ: Mapping[str, str] | None = None,
    forbidden_environment: Iterable[str] = (),
) -> ResolvedBackend | None:
    if not policy.enabled:
        return None
    if policy.backend is None or policy.profile is None:
        raise OperatorConfigError("enabled AI policy requires backend and profile")
    backend = config.backends.get(policy.backend)
    if backend is None:
        raise OperatorConfigError(f"backend is not Operator-authorized: {policy.backend}")
    if not backend.enabled:
        raise OperatorConfigError(f"backend is disabled by the Operator: {policy.backend}")
    if project_id not in backend.allowed_projects:
        raise OperatorConfigError(f"backend {policy.backend} does not allow Project {project_id}")
    profile = backend.profiles.get(policy.profile)
    if profile is None:
        raise OperatorConfigError(
            f"profile is not Operator-authorized: {policy.backend}/{policy.profile}"
        )
    forbidden = set(forbidden_environment)
    exposed = sorted(set(profile.environment) & forbidden)
    if exposed:
        raise OperatorConfigError(
            "Agent environment overlaps configured publication credentials: " + ", ".join(exposed)
        )
    source = os.environ if environ is None else environ
    allowed_values = {name: source[name] for name in profile.environment if name in source}
    # PATH is trusted deployment input needed by bare Operator commands and their
    # shebang interpreters. It is not Project-controlled or treated as a secret.
    allowed_values.setdefault("PATH", source.get("PATH", os.defpath))
    allowed_environment = MappingProxyType(allowed_values)
    return ResolvedBackend(
        policy.backend,
        policy.profile,
        backend.adapter,
        backend.command,
        profile.timeout_seconds,
        allowed_environment,
        profile.verification,
        profile.model,
    )

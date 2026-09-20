from pathlib import Path

import pytest

from review_agent_flow.operator_config import (
    OperatorConfigError,
    load_operator_config,
    resolve_backend,
    resolve_notification_channels,
)
from review_agent_flow.policy import AIPolicy


def _write_config(path: Path, publication: str = "") -> Path:
    path.write_text(
        f"""
version: 4
backends:
  local-agent:
    adapter: opencode
    command: [opencode]
    allowed_projects: ["42"]
    profiles:
      default:
        timeout_seconds: 120
        environment: [MODEL_TOKEN]
        verification:
          command: [pytest, -q]
autofix_publication:
  enabled: false
  allowed_projects: []
  initial_readiness: draft
  ci_trigger: manual
{publication}
""",
        encoding="utf-8",
    )
    return path


def test_operator_config_resolves_only_allowed_backend_profile_and_environment(tmp_path) -> None:
    config = load_operator_config(_write_config(tmp_path / "operator.yml"))
    assert config.retention.sensitive_payload_days == 30
    assert config.retention.publication_plan_backup_dir is None
    resolved = resolve_backend(
        config,
        AIPolicy(enabled=True, backend="local-agent", profile="default"),
        "42",
        {
            "MODEL_TOKEN": "allowed",
            "GITLAB_TOKEN": "forbidden",
            "PATH": "/operator/bin",
        },
    )

    assert resolved is not None
    assert resolved.adapter == "opencode"
    assert resolved.command == ("opencode",)
    assert resolved.timeout_seconds == 120
    assert resolved.environment == {"MODEL_TOKEN": "allowed", "PATH": "/operator/bin"}
    assert resolved.verification is not None
    assert resolved.verification.command == ("pytest", "-q")


def test_operator_execution_path_is_supplied_without_inheriting_other_environment(
    tmp_path,
) -> None:
    config = load_operator_config(_write_config(tmp_path / "operator.yml"))

    resolved = resolve_backend(
        config,
        AIPolicy(enabled=True, backend="local-agent", profile="default"),
        "42",
        {"PATH": "/trusted/bin", "UNLISTED": "hidden"},
    )

    assert resolved is not None
    assert resolved.environment == {"PATH": "/trusted/bin"}


def test_operator_config_resolves_generic_command_adapter(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("adapter: opencode", "adapter: command")
        .replace("command: [opencode]", "command: [custom-agent, --batch]"),
        encoding="utf-8",
    )

    resolved = resolve_backend(
        load_operator_config(path),
        AIPolicy(enabled=True, backend="local-agent", profile="default"),
        "42",
        {},
    )

    assert resolved is not None
    assert resolved.adapter == "command"
    assert resolved.command == ("custom-agent", "--batch")


def test_operator_config_resolves_opencode_model_from_profile(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "timeout_seconds: 120", "timeout_seconds: 120\n        model: openai/review-model"
        ),
        encoding="utf-8",
    )

    resolved = resolve_backend(
        load_operator_config(path),
        AIPolicy(enabled=True, backend="local-agent", profile="default"),
        "42",
        {},
    )

    assert resolved is not None and resolved.model == "openai/review-model"


def test_operator_config_rejects_model_for_generic_command_adapter(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("adapter: opencode", "adapter: command")
        .replace("command: [opencode]", "command: [custom-agent]")
        .replace("timeout_seconds: 120", "timeout_seconds: 120\n        model: provider/model"),
        encoding="utf-8",
    )

    with pytest.raises(OperatorConfigError, match="only by the OpenCode adapter"):
        load_operator_config(path)


def test_operator_config_rejects_opencode_subcommand_in_operator_argv(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace("command: [opencode]", "command: [opencode, run]"),
        encoding="utf-8",
    )

    with pytest.raises(OperatorConfigError, match="only the OpenCode executable"):
        load_operator_config(path)


def test_operator_config_denies_unknown_project_backend_and_profile(tmp_path) -> None:
    config = load_operator_config(_write_config(tmp_path / "operator.yml"))
    with pytest.raises(OperatorConfigError, match="does not allow Project"):
        resolve_backend(
            config,
            AIPolicy(enabled=True, backend="local-agent", profile="default"),
            "99",
        )
    with pytest.raises(OperatorConfigError, match="backend is not"):
        resolve_backend(config, AIPolicy(enabled=True, backend="missing", profile="default"), "42")
    with pytest.raises(OperatorConfigError, match="profile is not"):
        resolve_backend(
            config, AIPolicy(enabled=True, backend="local-agent", profile="missing"), "42"
        )


def test_operator_config_rejects_backend_timeout_above_shutdown_bound(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace("timeout_seconds: 120", "timeout_seconds: 1801"),
        encoding="utf-8",
    )

    with pytest.raises(OperatorConfigError, match="between 1 and 1800"):
        load_operator_config(path)


def test_operator_backend_kill_switch_denies_execution(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "adapter: opencode", "enabled: false\n    adapter: opencode"
        ),
        encoding="utf-8",
    )
    config = load_operator_config(path)
    with pytest.raises(OperatorConfigError, match="disabled by the Operator"):
        resolve_backend(
            config,
            AIPolicy(enabled=True, backend="local-agent", profile="default"),
            "42",
        )


@pytest.mark.parametrize(
    "replacement",
    [
        "version: 4",
        "version: 4\nunknown: true",
        "version: 4\nbackends: []",
    ],
)
def test_operator_config_is_strict(tmp_path, replacement: str) -> None:
    path = tmp_path / "operator.yml"
    path.write_text(replacement, encoding="utf-8")
    with pytest.raises(OperatorConfigError):
        load_operator_config(path)


def test_operator_config_rejects_ready_with_manual_ci(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    text = path.read_text(encoding="utf-8").replace(
        "initial_readiness: draft", "initial_readiness: ready"
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(OperatorConfigError, match="ready/manual"):
        load_operator_config(path)


def test_operator_retention_parses_absolute_backup_directory(tmp_path) -> None:
    backup_dir = tmp_path / "plan-backups"
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"""
retention:
  sensitive_payload_days: 45
  publication_plan_backup_dir: {backup_dir}
""",
        encoding="utf-8",
    )

    config = load_operator_config(path)

    assert config.retention.sensitive_payload_days == 45
    assert config.retention.publication_plan_backup_dir == backup_dir.resolve()


@pytest.mark.parametrize("days", [0, 3651, '"30"'])
def test_operator_retention_rejects_invalid_days(tmp_path, days) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8") + f"\nretention:\n  sensitive_payload_days: {days}\n",
        encoding="utf-8",
    )
    with pytest.raises(OperatorConfigError, match="sensitive_payload_days"):
        load_operator_config(path)


def test_operator_retention_rejects_relative_backup_directory(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\nretention:\n  publication_plan_backup_dir: relative/backups\n",
        encoding="utf-8",
    )
    with pytest.raises(OperatorConfigError, match="must be absolute"):
        load_operator_config(path)


@pytest.mark.parametrize(
    "name",
    [
        "GITLAB_TOKEN",
        "REVIEW_AGENT_FLOW_SHA",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "bad-name",
    ],
)
def test_operator_profile_cannot_expose_reserved_or_invalid_environment(
    tmp_path, name: str
) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "environment: [MODEL_TOKEN]", f"environment: [{name}]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(OperatorConfigError, match="environment"):
        load_operator_config(path)


def test_resolver_rejects_project_selected_notification_credential_name(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "environment: [MODEL_TOKEN]", "environment: [CUSTOM_CHAT_SECRET]"
        ),
        encoding="utf-8",
    )
    config = load_operator_config(path)
    with pytest.raises(OperatorConfigError, match="publication credentials"):
        resolve_backend(
            config,
            AIPolicy(enabled=True, backend="local-agent", profile="default"),
            "42",
            {"CUSTOM_CHAT_SECRET": "secret"},
            forbidden_environment={"CUSTOM_CHAT_SECRET"},
        )


def test_operator_yaml_rejects_duplicate_nested_authority_key(tmp_path) -> None:
    path = tmp_path / "operator.yml"
    path.write_text(
        """
version: 4
backends:
  local-agent:
    adapter: command
    command: [review-agent]
    command: [unsafe, run]
    allowed_projects: ["42"]
    profiles:
      default:
        environment: []
""",
        encoding="utf-8",
    )
    with pytest.raises(OperatorConfigError, match="duplicate key: command"):
        load_operator_config(path)


def test_notification_bindings_are_operator_owned_and_project_scoped(tmp_path, monkeypatch) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8")
        + """
notifications:
  channels:
    reviewers:
      type: feishu
      webhook_env: REVIEWERS_WEBHOOK
      allowed_projects: ["42"]
      user_ids:
        alice: ou_alice
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("REVIEWERS_WEBHOOK", "https://hooks.example.invalid/reviewers")
    config = load_operator_config(path)

    resolved = resolve_notification_channels(config, ("reviewers",), "42")

    assert resolved[0].channel_id == "reviewers"
    assert resolved[0].channel_type == "feishu"
    assert resolved[0].user_ids == {"alice": "ou_alice"}
    with pytest.raises(OperatorConfigError, match="does not allow Project"):
        resolve_notification_channels(config, ("reviewers",), "99")


def test_operator_config_does_not_expose_notification_credentials_to_agent(
    tmp_path,
) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "environment: [MODEL_TOKEN]", "environment: [REVIEWERS_WEBHOOK]"
        )
        + """
notifications:
  channels:
    reviewers:
      type: feishu
      webhook_env: REVIEWERS_WEBHOOK
      allowed_projects: ["42"]
""",
        encoding="utf-8",
    )

    with pytest.raises(OperatorConfigError, match="overlaps Operator notification"):
        load_operator_config(path)


def test_feishu_direct_channel_requires_one_bound_owner_and_private_credentials(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8")
        + """
notifications:
  channels:
    owner:
      type: feishu_dm
      app_id_env: FEISHU_APP_ID
      app_secret_env: FEISHU_APP_SECRET
      recipient_open_id_env: FEISHU_OWNER_OPEN_ID
      recipient_username: wufei-png
      allowed_projects: ["42"]
""",
        encoding="utf-8",
    )
    config = load_operator_config(path)
    environ = {
        "FEISHU_APP_ID": "cli_app",
        "FEISHU_APP_SECRET": "private-secret",
        "FEISHU_OWNER_OPEN_ID": "ou_owner",
    }

    resolved = resolve_notification_channels(config, ("owner",), "42", environ)

    assert resolved[0].channel_type == "feishu_dm"
    assert resolved[0].recipient_username == "wufei-png"
    assert resolved[0].recipient_open_id == "ou_owner"
    assert resolved[0].webhook_url is None
    with pytest.raises(OperatorConfigError, match="requires app and recipient credentials"):
        resolve_notification_channels(config, ("owner",), "42", {})
    with pytest.raises(OperatorConfigError, match="requires a Feishu open_id"):
        resolve_notification_channels(
            config, ("owner",), "42", {**environ, "FEISHU_OWNER_OPEN_ID": "chat-id"}
        )


def test_feishu_direct_channel_rejects_webhook_and_agent_credential_overlap(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    direct_channel = """
notifications:
  channels:
    owner:
      type: feishu_dm
      app_id_env: FEISHU_APP_ID
      app_secret_env: FEISHU_APP_SECRET
      recipient_open_id_env: FEISHU_OWNER_OPEN_ID
      recipient_username: wufei-png
      allowed_projects: ["42"]
"""
    path.write_text(path.read_text(encoding="utf-8") + direct_channel, encoding="utf-8")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "type: feishu_dm", "type: feishu_dm\n      webhook_env: GROUP_WEBHOOK"
        ),
        encoding="utf-8",
    )
    with pytest.raises(OperatorConfigError, match="unknown fields: webhook_env"):
        load_operator_config(path)

    path.write_text(
        _write_config(path)
        .read_text(encoding="utf-8")
        .replace("environment: [MODEL_TOKEN]", "environment: [FEISHU_APP_SECRET]")
        + direct_channel,
        encoding="utf-8",
    )
    with pytest.raises(OperatorConfigError, match="overlaps Operator notification"):
        load_operator_config(path)

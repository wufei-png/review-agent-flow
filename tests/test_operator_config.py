from pathlib import Path

import pytest

from review_agent_flow.operator_config import (
    OperatorConfigError,
    load_operator_config,
    resolve_backend,
)
from review_agent_flow.policy import AIPolicy


def _write_config(path: Path, publication: str = "") -> Path:
    path.write_text(
        f"""
version: 1
backends:
  local-agent:
    command: [opencode, run]
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
    assert resolved.command == ("opencode", "run")
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
        resolve_backend(config, AIPolicy(enabled=True, backend="local-agent", profile="missing"), "42")


def test_operator_backend_kill_switch_denies_execution(tmp_path) -> None:
    path = _write_config(tmp_path / "operator.yml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "command: [opencode, run]", "enabled: false\n    command: [opencode, run]"
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
        "version: 2",
        "version: 1\nunknown: true",
        "version: 1\nbackends: []",
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


@pytest.mark.parametrize("name", ["GITLAB_TOKEN", "REVIEW_AGENT_FLOW_SHA", "bad-name"])
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
version: 1
backends:
  local-agent:
    command: [opencode, run]
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

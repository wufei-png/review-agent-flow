import json
from pathlib import Path

import pytest

from review_agent_flow.opencode_auth_wrapper import prepare_auth


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "worker-auth.json"
    source.write_text(json.dumps({"openai": {"type": "oauth", "access": "private"}}))
    source.chmod(0o600)
    return source


def test_opencode_auth_is_copied_only_into_private_run_data(tmp_path: Path) -> None:
    source = _source(tmp_path)
    run_root = tmp_path / "run-1"
    run_root.mkdir(mode=0o700)

    target = prepare_auth(source, run_root / "opencode-data", run_root / "result.json")

    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700


def test_opencode_auth_rejects_other_target_and_broad_source(tmp_path: Path) -> None:
    source = _source(tmp_path)
    run_root = tmp_path / "run-1"
    run_root.mkdir(mode=0o700)
    with pytest.raises(PermissionError):
        prepare_auth(source, tmp_path / "outside", run_root / "result.json")
    source.chmod(0o644)
    with pytest.raises(PermissionError):
        prepare_auth(source, run_root / "opencode-data", run_root / "result.json")
    assert not (run_root / "opencode-data").exists()

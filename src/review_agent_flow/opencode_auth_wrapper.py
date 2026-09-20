"""Run OpenCode with one Worker-owned OAuth record in its isolated XDG data."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path


def _require_private_file(path: Path) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PermissionError("OpenCode auth source must be a Worker-owned mode-0600 file")
    content = path.read_bytes()
    record = json.loads(content)
    if set(record) != {"openai"} or record["openai"].get("type") != "oauth":
        raise ValueError("OpenCode auth source must contain only OpenAI OAuth")
    return content


def prepare_auth(source: Path, data_home: Path, result_path: Path) -> Path:
    """Materialize auth only beneath this Run's private result directory."""

    run_root = result_path.parent
    root_metadata = run_root.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or result_path.name != "result.json"
        or data_home != run_root / "opencode-data"
    ):
        raise PermissionError("OpenCode auth target is outside the private Run directory")
    content = _require_private_file(source)
    data_home.mkdir(mode=0o700)
    auth_dir = data_home / "opencode"
    auth_dir.mkdir(mode=0o700)
    target = auth_dir / "auth.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def main() -> None:
    source_value = os.environ.get("OPENCODE_AUTH_SOURCE", "")
    data_value = os.environ.get("XDG_DATA_HOME", "")
    result_value = os.environ.get("REVIEW_AGENT_FLOW_RESULT_PATH", "")
    if not source_value or not data_value or not result_value:
        raise SystemExit("OpenCode auth source and Run paths are required")
    source = Path(source_value)
    data_home = Path(data_value)
    result_path = Path(result_value)
    if not all(path.is_absolute() for path in (source, data_home, result_path)):
        raise SystemExit("OpenCode auth paths must be absolute")
    prepare_auth(source, data_home, result_path)
    executable = shutil.which("opencode")
    if executable is None:
        raise SystemExit("OpenCode executable is unavailable")
    # OpenCode's built-in OpenAI OAuth plugin is required for this auth record.
    # --pure still excludes external plugins and the adapter supplies task permissions.
    os.environ.pop("OPENCODE_DISABLE_DEFAULT_PLUGINS", None)
    os.execv(executable, (executable, *sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()

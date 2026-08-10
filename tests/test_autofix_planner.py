import os
import subprocess
import sys
from pathlib import Path

import pytest

from review_agent_flow.autofix_planner import (
    MAX_AUTOFIX_FILE_BYTES,
    DiffEnvelopeError,
    build_publication_plan_payload,
    capture_diff_envelope,
    run_verifier,
)


def _repository(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "change.txt").write_text("before\n", encoding="utf-8")
    (path / "delete.txt").write_text("delete me\n", encoding="utf-8")
    (path / "mode.txt").write_text("mode\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()


def test_capture_diff_envelope_canonicalizes_text_actions(tmp_path: Path) -> None:
    sha = _repository(tmp_path)
    (tmp_path / "change.txt").write_text("after\n", encoding="utf-8")
    (tmp_path / "delete.txt").unlink()
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")
    os.chmod(tmp_path / "mode.txt", 0o755)

    envelope = capture_diff_envelope(tmp_path, sha)

    assert [action.as_dict() for action in envelope.actions] == [
        {
            "action": "update",
            "file_path": "change.txt",
            "content": "after\n",
            "execute_filemode": False,
        },
        {"action": "delete", "file_path": "delete.txt"},
        {"action": "chmod", "file_path": "mode.txt", "execute_filemode": True},
        {
            "action": "create",
            "file_path": "new.txt",
            "content": "new\n",
            "execute_filemode": False,
        },
    ]
    assert len(envelope.candidate_sha256) == 64
    assert envelope.candidate_sha256 == capture_diff_envelope(
        tmp_path, sha
    ).candidate_sha256


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("binary.dat", b"text\x00binary", "binary content at path: binary.dat"),
        ("not-utf8.txt", b"\xff", "UTF-8 at path: not-utf8.txt"),
        (
            "large.txt",
            b"x" * (MAX_AUTOFIX_FILE_BYTES + 1),
            "per-file bound",
        ),
    ],
    ids=("binary", "non-utf8", "oversized"),
)
def test_capture_rejects_binary_non_utf8_and_oversized_files(
    tmp_path: Path, name: str, content: bytes, message: str
) -> None:
    sha = _repository(tmp_path)
    (tmp_path / name).write_bytes(content)

    with pytest.raises(DiffEnvelopeError, match=message):
        capture_diff_envelope(tmp_path, sha)


def test_capture_rejects_deleting_non_text_baseline(tmp_path: Path) -> None:
    _repository(tmp_path)
    binary = tmp_path / "tracked-binary.dat"
    binary.write_bytes(b"text\x00binary")
    subprocess.run(["git", "-C", str(tmp_path), "add", binary.name], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "binary baseline"],
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    binary.unlink()

    with pytest.raises(DiffEnvelopeError, match="binary"):
        capture_diff_envelope(tmp_path, sha)


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics require POSIX")
def test_capture_rejects_symlink_and_changed_head(tmp_path: Path) -> None:
    sha = _repository(tmp_path)
    (tmp_path / "link").symlink_to("change.txt")
    with pytest.raises(DiffEnvelopeError, match="symlink"):
        capture_diff_envelope(tmp_path, sha)
    (tmp_path / "link").unlink()
    (tmp_path / "committed.txt").write_text("committed\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "committed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "agent commit"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(DiffEnvelopeError, match="HEAD"):
        capture_diff_envelope(tmp_path, sha)


def test_verifier_evidence_binds_candidate_and_mutation_is_detectable(tmp_path: Path) -> None:
    sha = _repository(tmp_path)
    (tmp_path / "change.txt").write_text("candidate\n")
    before = capture_diff_envelope(tmp_path, sha)
    evidence = run_verifier(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('change.txt').write_text('mutated\\n')",
        ],
        tmp_path,
        {},
        before.candidate_sha256,
        timeout_seconds=5,
    )
    after = capture_diff_envelope(tmp_path, sha)

    assert evidence.candidate_sha256 == before.candidate_sha256
    assert evidence.returncode == 0
    assert after.candidate_sha256 != before.candidate_sha256


def test_verifier_failure_and_missing_verifier_fail_closed(tmp_path: Path) -> None:
    _repository(tmp_path)
    with pytest.raises(DiffEnvelopeError, match="requires"):
        run_verifier((), tmp_path, {}, "digest", timeout_seconds=5)
    with pytest.raises(DiffEnvelopeError, match="exited with code 3"):
        run_verifier(
            [sys.executable, "-c", "raise SystemExit(3)"],
            tmp_path,
            {},
            "digest",
            timeout_seconds=5,
        )


def test_publication_payload_contains_only_independent_digest_inputs(tmp_path: Path) -> None:
    sha = _repository(tmp_path)
    (tmp_path / "change.txt").write_text("candidate\n")
    envelope = capture_diff_envelope(tmp_path, sha)
    evidence = run_verifier(
        [sys.executable, "-c", "pass"],
        tmp_path,
        {},
        envelope.candidate_sha256,
        timeout_seconds=5,
    )

    payload = build_publication_plan_payload(
        project_id="42",
        mr_iid=1,
        sha=sha,
        generation=1,
        nonce="nonce",
        proposal_summary="Fix it",
        envelope=envelope,
        verification=evidence,
        target_branch="feature",
        parent_title="Review",
        initial_readiness="draft",
        ci_trigger="manual",
    )

    assert "plan_sha256" not in payload
    assert "<!-- reviewflow:v1" not in payload
    assert payload["branch"] == f"autofix/MR-1/sha-{sha}/intent-nonce"
    assert payload["marker_template"].endswith("plan_sha256={plan_sha256} -->")
    assert str(payload["commit_message"]).endswith("[skip ci]")
    assert str(payload["title"]).startswith("Draft: ")

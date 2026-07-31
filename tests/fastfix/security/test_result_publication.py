import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from benchmarks.scripts.run_fastfix_secure import SecureRunnerError, _write_result_bundle
from fastfix.security.result_publication import (
    ResultPublicationError,
    create_result_manifest,
    publication_state_path,
    publish_result_bundle,
    read_publication_state,
    restore_windows_acl,
    verify_published_result,
)


def write_bundle(path: Path) -> dict[str, str]:
    (path / "nested").mkdir(parents=True)
    (path / "summary.json").write_text('{"status": "complete"}\n', encoding="utf-8")
    (path / "nested" / "trajectory.json").write_text('{"messages": []}\n', encoding="utf-8")
    (path / "changed-files.txt").write_text("app/main.py\n", encoding="utf-8")
    return create_result_manifest(
        path,
        {"summary.json", "nested/trajectory.json", "changed-files.txt"},
    )


def test_non_windows_acl_is_noop_and_publication_stays_atomic(tmp_path: Path) -> None:
    temporary = tmp_path / "temporary"
    destination = tmp_path / "result"
    manifest = write_bundle(temporary)
    calls: list[object] = []
    order: list[str] = []

    def verify(path: Path, expected: dict[str, str]) -> None:
        order.append("verify")
        verify_published_result(path, expected)

    restore_windows_acl(
        temporary,
        platform="posix",
        run_command=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    publish_result_bundle(
        temporary,
        destination,
        manifest,
        acl_restorer=lambda path: order.append("acl"),
        verifier=verify,
    )

    assert calls == []
    assert order == ["acl", "verify"]
    assert not temporary.exists()
    assert not publication_state_path(destination).exists()
    verify_published_result(destination, manifest)


def test_windows_acl_uses_argument_list_for_spaced_bundle(tmp_path: Path) -> None:
    target = tmp_path / "published result with spaces"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, stdout="ok", stderr="")

    restore_windows_acl(target, platform="nt", run_command=run)

    assert calls == [
        (
            ["icacls", str(target), "/inheritance:e", "/t", "/c"],
            {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 60,
                "shell": False,
            },
        )
    ]


def test_acl_failure_is_quarantined_and_records_command_details(tmp_path: Path) -> None:
    temporary = tmp_path / "temporary"
    destination = tmp_path / "result"
    manifest = write_bundle(temporary)

    def fail(path: Path) -> None:
        raise ResultPublicationError(
            "windows_acl_repair_failed",
            "failed",
            returncode=5,
            stdout="stdout",
            stderr="stderr",
        )

    with pytest.raises(ResultPublicationError) as error:
        publish_result_bundle(temporary, destination, manifest, acl_restorer=fail)

    assert error.value.code == "windows_acl_repair_failed"
    assert error.value.returncode == 5
    assert error.value.stdout == "stdout"
    assert error.value.stderr == "stderr"
    assert not destination.exists()
    state = read_publication_state(destination)
    assert state is not None
    assert state["state"] == "failed"
    assert state["error"] == "windows_acl_repair_failed"
    quarantine = destination.parent / state["quarantine"]
    assert quarantine.is_dir()
    verify_published_result(quarantine, manifest)


def test_nonzero_acl_command_has_stable_error_and_captured_output(tmp_path: Path) -> None:
    def run(arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 1337, stdout="captured out", stderr="captured err")

    with pytest.raises(ResultPublicationError) as error:
        restore_windows_acl(tmp_path, platform="nt", run_command=run)

    assert error.value.code == "windows_acl_repair_failed"
    assert error.value.returncode == 1337
    assert error.value.stdout == "captured out"
    assert error.value.stderr == "captured err"


def test_post_acl_readability_failure_is_not_published(tmp_path: Path) -> None:
    temporary = tmp_path / "temporary"
    destination = tmp_path / "result"
    manifest = write_bundle(temporary)
    calls: list[Path] = []

    def unreadable(path: Path, expected: dict[str, str]) -> None:
        calls.append(path)
        raise ResultPublicationError("result_readability_failed", "unreadable")

    with pytest.raises(ResultPublicationError) as error:
        publish_result_bundle(
            temporary,
            destination,
            manifest,
            acl_restorer=lambda path: calls.append(path),
            verifier=unreadable,
        )

    assert error.value.code == "result_readability_failed"
    assert calls == [destination, destination]
    assert not destination.exists()
    assert read_publication_state(destination)["error"] == "result_readability_failed"


def test_existing_result_is_never_overwritten(tmp_path: Path) -> None:
    temporary = tmp_path / "temporary"
    destination = tmp_path / "result"
    manifest = write_bundle(temporary)
    destination.mkdir()
    sentinel = destination / "historical.json"
    sentinel.write_text('{"preserved": true}\n', encoding="utf-8")

    with pytest.raises(ResultPublicationError) as error:
        publish_result_bundle(temporary, destination, manifest)

    assert error.value.code == "result_exists"
    assert json.loads(sentinel.read_text(encoding="utf-8")) == {"preserved": True}
    assert temporary.is_dir()
    assert not publication_state_path(destination).exists()


def test_manifest_rejects_unexpected_files_and_invalid_json(tmp_path: Path) -> None:
    temporary = tmp_path / "temporary"
    manifest = write_bundle(temporary)
    (temporary / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ResultPublicationError) as unexpected:
        create_result_manifest(temporary, manifest)
    assert unexpected.value.code == "result_publication_failed"

    (temporary / "unexpected.txt").unlink()
    (temporary / "summary.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ResultPublicationError) as invalid:
        verify_published_result(temporary, manifest)
    assert invalid.value.code == "result_readability_failed"


def test_atomic_publish_failure_has_stable_error_and_incomplete_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "temporary"
    destination = tmp_path / "result"
    manifest = write_bundle(temporary)

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("denied")

    monkeypatch.setattr(
        "fastfix.security.result_publication.os.replace",
        fail_replace,
    )
    with pytest.raises(ResultPublicationError) as error:
        publish_result_bundle(temporary, destination, manifest)

    assert error.value.code == "result_publication_failed"
    assert temporary.is_dir()
    assert not destination.exists()
    assert read_publication_state(destination)["state"] == "publishing"


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("result_publication_failed", "publication failed"),
        ("windows_acl_repair_failed", "ACL failed"),
        ("result_readability_failed", "readability failed"),
    ],
)
def test_runner_preserves_publication_error_codes(tmp_path: Path, code: str, message: str) -> None:
    output = tmp_path / "result"

    def fail(temporary: Path, destination: Path, manifest: dict[str, str]) -> None:
        raise ResultPublicationError(code, message)

    with pytest.raises(SecureRunnerError) as error:
        _write_result_bundle(
            output,
            summary={
                "targeted_tests_passed": False,
                "regression_tests_passed": False,
                "ruff_passed": False,
                "validation_revision": 0,
                "sandbox_image_id": None,
            },
            trajectory={},
            tool_calls=[],
            changed_files=[],
            package=None,
            failure=None,
            replacements={},
            publisher=fail,
        )

    assert error.value.code == code
    assert str(error.value) == message
    assert not output.exists()
    assert not list(tmp_path.glob(".*.tmp-*"))


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows NTFS ACL tools")
def test_real_windows_acl_publication_preserves_hashes_and_readability(tmp_path: Path) -> None:
    if shutil.which("icacls") is None:
        pytest.skip("Windows icacls is unavailable")
    workspace_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="real acl integration ", dir=tmp_path) as workspace:
        workspace_path = Path(workspace)
        parent = workspace_path / "acl publication with spaces"
        parent.mkdir()
        temporary = Path(tempfile.mkdtemp(prefix="restricted source ", dir=parent))
        destination = parent / "published result"
        manifest = write_bundle(temporary)
        disabled = subprocess.run(
            ["icacls", str(temporary), "/inheritance:d", "/t", "/c"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
        )
        if disabled.returncode:
            pytest.skip(f"cannot create a non-inheriting ACL fixture: exit {disabled.returncode}")
        before_acl = subprocess.run(
            ["icacls", str(temporary)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
        )
        assert before_acl.returncode == 0
        assert "(I)" not in before_acl.stdout
        assert all(path.read_bytes() for path in temporary.rglob("*") if path.is_file())

        publish_result_bundle(temporary, destination, manifest)

        assert len(manifest) == 3
        assert {
            path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in destination.rglob("*")
            if path.is_file()
        } == manifest
        assert all(
            "(I)"
            in subprocess.run(
                ["icacls", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                shell=False,
            ).stdout
            for path in [destination, *destination.rglob("*")]
        )
        git = shutil.which("git")
        assert git is not None
        subprocess.run([git, "init", "-q", str(parent)], check=True, shell=False)
        status = subprocess.run(
            [git, "-C", str(parent), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=True,
            shell=False,
        ).stdout
        assert all(f"published result/{path}" in status for path in manifest)
    assert workspace_path is not None and not workspace_path.exists()

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from fastfix.approval.package import ApprovalPackageError, ApprovalPackageManager
from fastfix.repair.state import RepairSessionState
from fastfix.workspace.candidate import CandidateWorkspace, CandidateWorkspaceManager

REQUEST_ID = "12345678-1234-4234-8234-123456789abc"
IMAGE_ID = "sha256:" + "a" * 64


def git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )


def repository(path: Path) -> Path:
    (path / "app").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    (path / "app" / "spare.py").write_text("spare = 1\n", encoding="utf-8")
    for index in range(6):
        (path / "app" / f"file{index}.py").write_text(f"value = {index}\n", encoding="utf-8")
    (path / "tests" / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    git(path, "init", "-q")
    git(path, "config", "user.name", "FastFix Tests")
    git(path, "config", "user.email", "fastfix@example.invalid")
    git(path, "config", "core.autocrlf", "false")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "baseline")
    return path


def validation_result(*, ruff: bool = False, validation_epoch: int = 1) -> dict[str, object]:
    return {
        "returncode": 0,
        "passed": True if ruff else 1,
        "failed": 0,
        "skipped": 0,
        "duration_seconds": 0.2,
        "timed_out": False,
        "image_id": IMAGE_ID,
        "network_mode": "none",
        "scope_complete": True,
        "validation_epoch": validation_epoch,
    }


def validated_state(revision: int = 1) -> RepairSessionState:
    return RepairSessionState(
        revision=revision,
        validation_epoch=1,
        patch_count=1,
        changed_files=["app/main.py"],
        targeted_test_revision=revision,
        regression_test_revision=revision,
        ruff_revision=revision,
        targeted_test_result=validation_result(),
        regression_test_result=validation_result(),
        ruff_result=validation_result(ruff=True),
    )


@pytest.fixture
def prepared(tmp_path: Path):
    source = repository(tmp_path / "source repository")
    candidates = tmp_path / "candidate repositories"
    candidates.mkdir()
    manager = CandidateWorkspaceManager(candidates)
    candidate = manager.create(source, target=candidates / "candidate repository")
    (candidate.path / "app" / "main.py").write_text("value = 2\n", encoding="utf-8")
    output = tmp_path / "approval packages"
    output.mkdir()
    yield source, candidate, output
    candidate.cleanup()


def package(
    source: Path,
    candidate: CandidateWorkspace,
    output: Path,
    *,
    state: RepairSessionState | None = None,
) -> tuple[ApprovalPackageManager, Path]:
    manager = FixedIdManager(output)
    return manager, manager.create(
        task_id="FF-001",
        source=source,
        candidate=candidate.path,
        source_head=candidate.source_head,
        repair_state=state or validated_state(),
    )


def snapshot(repository: Path) -> tuple[dict[str, str], str, str]:
    hashes = {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repository).parts
    }
    return (
        hashes,
        git(repository, "rev-parse", "HEAD").stdout,
        git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout,
    )


def writable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IWRITE)


def test_create_package_matches_candidate_diff_and_contains_no_absolute_paths(prepared) -> None:
    source, candidate, output = prepared
    manager, created = package(source, candidate, output)
    assert sorted(path.name for path in created.iterdir()) == [
        "approval-request.json",
        "manifest.json",
        "patch.diff",
        "validation-summary.json",
    ]
    assert (created / "patch.diff").read_text(encoding="utf-8") == git(candidate.path, "diff", "HEAD", "--").stdout
    request = manager.verify_package(created)
    assert request.source_head == request.candidate_head == candidate.source_head
    assert request.changed_files == ["app/main.py"]
    assert request.added_lines == request.deleted_lines == 1
    assert request.status == "pending" and request.sandbox_network_mode == "none"
    validation = json.loads((created / "validation-summary.json").read_text(encoding="utf-8"))
    assert request.validation_epoch == validation["validation_epoch"] == 1
    assert request.reopen_count == validation["reopen_count"] == 0
    serialized = "".join(path.read_text(encoding="utf-8") for path in created.iterdir())
    assert str(source.resolve()) not in serialized and str(candidate.path.resolve()) not in serialized


def test_source_and_candidate_are_unchanged_by_package_creation(prepared) -> None:
    source, candidate, output = prepared
    before = snapshot(source), snapshot(candidate.path)
    package(source, candidate, output)
    assert (snapshot(source), snapshot(candidate.path)) == before


def test_dirty_source_and_changed_source_head_are_rejected(prepared) -> None:
    source, candidate, output = prepared
    (source / "app" / "main.py").write_text("dirty = True\n", encoding="utf-8")
    with pytest.raises(ApprovalPackageError) as dirty:
        package(source, candidate, output)
    assert dirty.value.code == "source_not_clean"
    git(source, "restore", "app/main.py")
    (source / "app" / "new.py").write_text("new = True\n", encoding="utf-8")
    git(source, "add", "app/new.py")
    git(source, "commit", "-q", "-m", "move head")
    with pytest.raises(ApprovalPackageError) as changed:
        package(source, candidate, output)
    assert changed.value.code == "source_head_mismatch"


def test_empty_diff_is_rejected(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    parent = tmp_path / "candidates"
    parent.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    with CandidateWorkspaceManager(parent).create(source) as candidate:
        with pytest.raises(ApprovalPackageError) as error:
            package(source, candidate, output)
    assert error.value.code == "empty_patch"


@pytest.mark.parametrize(
    ("kind", "code"), [("staged", "candidate_staged_changes"), ("untracked", "candidate_untracked_files")]
)
def test_staged_and_untracked_candidate_content_is_rejected(prepared, kind: str, code: str) -> None:
    source, candidate, output = prepared
    if kind == "staged":
        git(candidate.path, "add", "app/main.py")
    else:
        (candidate.path / "notes.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(ApprovalPackageError) as error:
        package(source, candidate, output)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("path", "code"),
    [("tests/test_main.py", "tests_modified"), ("README.md", "patch_out_of_scope")],
)
def test_test_and_out_of_scope_changes_are_rejected(prepared, path: str, code: str) -> None:
    source, candidate, output = prepared
    (candidate.path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    target = candidate.path / path
    target.write_text(target.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    with pytest.raises(ApprovalPackageError) as error:
        package(source, candidate, output)
    assert error.value.code == code


@pytest.mark.parametrize("operation", ["new", "delete", "rename", "binary"])
def test_new_delete_rename_and_binary_changes_are_rejected(prepared, operation: str) -> None:
    source, candidate, output = prepared
    main = candidate.path / "app" / "main.py"
    main.write_text("value = 1\n", encoding="utf-8")
    if operation == "new":
        (candidate.path / "app" / "new.py").write_text("new = True\n", encoding="utf-8")
    elif operation == "delete":
        (candidate.path / "app" / "spare.py").unlink()
    elif operation == "rename":
        git(candidate.path, "mv", "app/spare.py", "app/renamed.py")
    else:
        (candidate.path / "app" / "spare.py").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ApprovalPackageError) as error:
        package(source, candidate, output)
    assert error.value.code in {
        "binary_patch",
        "candidate_staged_changes",
        "candidate_untracked_files",
        "patch_operation_not_allowed",
    }


@pytest.mark.parametrize("mode", ["missing", "stale", "failed", "wrong_network"])
def test_incomplete_stale_or_failed_validation_is_rejected(prepared, mode: str) -> None:
    source, candidate, output = prepared
    state = validated_state()
    if mode == "missing":
        state.targeted_test_result = None
    elif mode == "stale":
        state.targeted_test_revision = 0
    elif mode == "failed":
        state.regression_test_result["returncode"] = 1
    else:
        state.ruff_result["network_mode"] = "bridge"
    with pytest.raises(ApprovalPackageError) as error:
        package(source, candidate, output, state=state)
    assert error.value.code in {"validation_incomplete", "validation_failed", "sandbox_validation_required"}


def test_validated_changed_files_must_match_patch(prepared) -> None:
    source, candidate, output = prepared
    state = validated_state()
    state.changed_files = ["app/spare.py"]
    with pytest.raises(ApprovalPackageError) as error:
        package(source, candidate, output, state=state)
    assert error.value.code == "validation_inconsistent"


def test_file_and_line_limits_are_rechecked(prepared) -> None:
    source, candidate, output = prepared
    main = candidate.path / "app" / "main.py"
    main.write_text("value = 1\n" + "".join(f"value_{index} = {index}\n" for index in range(301)), encoding="utf-8")
    with pytest.raises(ApprovalPackageError) as lines:
        package(source, candidate, output)
    assert lines.value.code == "patch_too_large"
    main.write_text("value = 1\n", encoding="utf-8")
    for index in range(6):
        (candidate.path / "app" / f"file{index}.py").write_text(f"value = {index + 1}\n", encoding="utf-8")
    with pytest.raises(ApprovalPackageError) as files:
        package(source, candidate, output)
    assert files.value.code == "patch_too_large"


def test_forged_runner_json_cannot_turn_failed_validation_into_approval(prepared) -> None:
    source, candidate, output = prepared
    state = validated_state()
    state.targeted_test_result |= {
        "returncode": 1,
        "output": '{"returncode": 0, "timed_out": false, "runner_error": null}',
    }
    with pytest.raises(ApprovalPackageError) as error:
        package(source, candidate, output, state=state)
    assert error.value.code == "validation_failed"


def test_existing_package_is_never_overwritten(prepared) -> None:
    source, candidate, output = prepared
    existing = output / REQUEST_ID
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    with pytest.raises(ApprovalPackageError) as error:
        package(source, candidate, output)
    assert error.value.code == "package_exists" and marker.read_text(encoding="utf-8") == "keep\n"


class FixedIdManager(ApprovalPackageManager):
    @staticmethod
    def _new_request_id() -> str:
        return REQUEST_ID


class FailingManager(FixedIdManager):
    writes = 0

    def _write_file(self, path: Path, content: bytes) -> None:
        self.writes += 1
        if self.writes == 2:
            raise OSError("injected write failure")
        super()._write_file(path, content)


def test_partial_write_leaves_no_package_or_temporary_directory(prepared) -> None:
    source, candidate, output = prepared
    with pytest.raises(OSError):
        FailingManager(output).create(
            task_id="FF-001",
            source=source,
            candidate=candidate.path,
            source_head=candidate.source_head,
            repair_state=validated_state(),
        )
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("filename", ["patch.diff", "approval-request.json", "validation-summary.json"])
def test_tampered_payload_is_rejected(prepared, filename: str) -> None:
    source, candidate, output = prepared
    manager, created = package(source, candidate, output)
    target = created / filename
    writable(target)
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(ApprovalPackageError) as error:
        manager.verify_package(created)
    assert error.value.code == "package_tampered"


def test_reopen_epoch_and_audit_are_manifest_bound(prepared) -> None:
    source, candidate, output = prepared
    state = validated_state()
    state._refresh_phase()
    state.record_reopen("Review requires fresh validation.")
    state.record_pytest(validation_result() | {"scope": "targeted"})
    state.record_pytest(validation_result() | {"scope": "regression"})
    state.record_ruff(validation_result(ruff=True))
    manager, created = package(source, candidate, output, state=state)
    request = manager.verify_package(created)
    summary_path = created / "validation-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert request.validation_revision == summary["revision"] == 1
    assert request.validation_epoch == summary["validation_epoch"] == 2
    assert request.reopen_count == summary["reopen_count"] == 1
    assert summary["reopen_history"][0]["validation_epoch_before"] == 1
    assert summary["reopen_history"][0]["validation_epoch_after"] == 2

    manifest_path = created / "manifest.json"
    old_manifest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    summary["validation_epoch"] = 3
    summary_bytes = (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    writable(summary_path)
    summary_path.write_bytes(summary_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == "validation-summary.json")
    entry["bytes"] = len(summary_bytes)
    entry["sha256"] = hashlib.sha256(summary_bytes).hexdigest()
    writable(manifest_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() != old_manifest
    with pytest.raises(ApprovalPackageError) as error:
        manager.verify_package(created)
    assert error.value.code == "package_invalid"


def test_manifest_path_traversal_is_rejected(prepared) -> None:
    source, candidate, output = prepared
    manager, created = package(source, candidate, output)
    manifest_path = created / "manifest.json"
    writable(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../approval-request.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ApprovalPackageError) as error:
        manager.verify_package(created)
    assert error.value.code == "manifest_path_invalid"


def test_spaces_in_all_paths_are_supported(prepared) -> None:
    source, candidate, output = prepared
    assert package(source, candidate, output)[1].is_dir()


def test_git_subprocess_uses_minimal_environment() -> None:
    assert set(ApprovalPackageManager._environment()) <= {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "PATHEXT",
    }
    assert "API_KEY" not in ApprovalPackageManager._environment()


def test_production_request_ids_are_random_uuids() -> None:
    first = ApprovalPackageManager._new_request_id()
    second = ApprovalPackageManager._new_request_id()
    assert UUID(first) and UUID(second) and first != second

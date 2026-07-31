import inspect
import subprocess
from pathlib import Path

import pytest

from fastfix.security.paths import PathPolicyError
from fastfix.tools.editing import (
    ApplyPatchArgs,
    ReplaceTextArgs,
    RollbackChangesArgs,
    ShowGitDiffArgs,
    WorkspaceGitTools,
)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    ).stdout


def repository(tmp_path: Path, *, files: dict[str, str] | None = None) -> Path:
    for name, content in (
        files
        or {
            "app/main.py": "value = 1\n",
            "tests/test_main.py": "def test_value():\n    assert True\n",
            "README.md": "fixture\n",
        }
    ).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "FastFix Tests")
    git(tmp_path, "config", "user.email", "fastfix@example.invalid")
    git(tmp_path, "config", "core.autocrlf", "false")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def patch(path: str, before: str, after: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{before}\n+{after}\n"


def test_apply_show_diff_and_rollback(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    tools = WorkspaceGitTools(workspace)
    result = tools.apply_patch(ApplyPatchArgs(patch=patch("app/main.py", "value = 1", "value = 2")))
    assert result.ok
    assert result.metadata == {
        "changed_files": ["app/main.py"],
        "added_lines": 1,
        "deleted_lines": 1,
    }
    shown = tools.show_git_diff(ShowGitDiffArgs())
    assert shown.ok and shown.metadata["changed_files"] == ["app/main.py"]
    assert "-value = 1" in shown.output and "+value = 2" in shown.output
    rolled_back = tools.rollback_changes(RollbackChangesArgs(reason="test"))
    assert rolled_back.ok and rolled_back.metadata == {
        "rolled_back_files": ["app/main.py"],
        "changed_files": [],
        "clean": True,
    }
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "value = 1\n"


def recommit_bytes(workspace: Path, relative_path: str, content: bytes) -> Path:
    path = workspace / relative_path
    path.write_bytes(content)
    git(workspace, "add", relative_path)
    git(workspace, "commit", "-q", "--amend", "--no-edit")
    return path


def test_replace_text_unique_match_has_real_metadata(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    path = workspace / "app" / "main.py"
    original_size = len(path.read_bytes())
    result = WorkspaceGitTools(workspace).replace_text(
        ReplaceTextArgs(path="app/main.py", old_text="value = 1", new_text="value = 200")
    )
    assert result.ok
    assert path.read_text(encoding="utf-8") == "value = 200\n"
    assert result.metadata == {
        "path": "app/main.py",
        "replacement_count": 1,
        "changed_files": ["app/main.py"],
        "bytes_before": original_size,
        "bytes_after": len(path.read_bytes()),
    }


def test_replace_text_can_revert_one_of_multiple_changed_files(tmp_path: Path) -> None:
    workspace = repository(
        tmp_path,
        files={
            "app/main.py": "value = 1\n",
            "app/service.py": "result = 1\n",
        },
    )
    tools = WorkspaceGitTools(workspace)
    (workspace / "app" / "main.py").write_text("value = 2\n", encoding="utf-8")
    (workspace / "app" / "service.py").write_text("result = 2\n", encoding="utf-8")

    result = tools.replace_text(ReplaceTextArgs(path="app/main.py", old_text="value = 2", new_text="value = 1"))

    assert result.ok and result.metadata["changed_files"] == ["app/service.py"]
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (workspace / "app" / "service.py").read_text(encoding="utf-8") == "result = 2\n"
    assert git(workspace, "diff", "--name-only").splitlines() == ["app/service.py"]


def test_replace_text_can_revert_only_changed_file(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    tools = WorkspaceGitTools(workspace)
    (workspace / "app" / "main.py").write_text("value = 2\n", encoding="utf-8")

    result = tools.replace_text(ReplaceTextArgs(path="app/main.py", old_text="value = 2", new_text="value = 1"))

    assert result.ok and result.metadata["changed_files"] == []
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "value = 1\n"
    assert git(workspace, "status", "--porcelain") == ""


def test_apply_patch_reports_complete_current_diff_after_local_revert(tmp_path: Path) -> None:
    workspace = repository(
        tmp_path,
        files={
            "app/main.py": "value = 1\n",
            "app/service.py": "result = 1\n",
        },
    )
    tools = WorkspaceGitTools(workspace)
    (workspace / "app" / "main.py").write_text("value = 2\n", encoding="utf-8")
    (workspace / "app" / "service.py").write_text("result = 2\n", encoding="utf-8")
    result = tools.apply_patch(ApplyPatchArgs(patch=patch("app/service.py", "result = 2", "result = 1")))
    assert result.ok and result.metadata["changed_files"] == ["app/main.py"]


def test_replace_text_multiline_and_multiple_occurrences(tmp_path: Path) -> None:
    workspace = repository(tmp_path, files={"app/main.py": "one\ntwo\none\ntwo\n"})
    result = WorkspaceGitTools(workspace).replace_text(
        ReplaceTextArgs(
            path="app/main.py",
            old_text="one\ntwo",
            new_text="three\nfour",
            expected_occurrences=2,
        )
    )
    assert result.ok and result.metadata["replacement_count"] == 2
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "three\nfour\nthree\nfour\n"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"value = 1\nnext = 2\n", b"value = 3\nnext = 2\n"),
        (b"value = 1\r\nnext = 2\r\n", b"value = 3\r\nnext = 2\r\n"),
    ],
)
def test_replace_text_preserves_file_newlines(
    tmp_path: Path,
    content: bytes,
    expected: bytes,
) -> None:
    workspace = repository(tmp_path)
    path = recommit_bytes(workspace, "app/main.py", content)
    result = WorkspaceGitTools(workspace).replace_text(
        ReplaceTextArgs(path="app/main.py", old_text="value = 1\n", new_text="value = 3\n")
    )
    assert result.ok and path.read_bytes() == expected


def test_replace_text_not_found_and_occurrence_mismatch_leave_no_diff(tmp_path: Path) -> None:
    workspace = repository(tmp_path, files={"app/main.py": "value = 1\nvalue = 1\n"})
    tools = WorkspaceGitTools(workspace)
    missing = tools.replace_text(ReplaceTextArgs(path="app/main.py", old_text="missing", new_text="replacement"))
    mismatch = tools.replace_text(ReplaceTextArgs(path="app/main.py", old_text="value = 1", new_text="value = 2"))
    assert missing.error_code == "text_not_found"
    assert mismatch.error_code == "occurrence_mismatch"
    assert mismatch.metadata["actual_occurrences"] == 2
    assert git(workspace, "diff", "--name-only") == ""


def test_replace_text_no_effect_is_rejected(tmp_path: Path) -> None:
    tools = WorkspaceGitTools(repository(tmp_path))
    result = tools.replace_text(ReplaceTextArgs(path="app/main.py", old_text="value = 1", new_text="value = 1"))
    assert not result.ok and result.error_code == "no_effect"


def test_replace_text_normalized_no_effect_is_rejected(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    path = recommit_bytes(workspace, "app/main.py", b"value = 1\r\n")
    result = WorkspaceGitTools(workspace).replace_text(
        ReplaceTextArgs(path="app/main.py", old_text="value = 1\r\n", new_text="value = 1\n")
    )
    assert not result.ok and result.error_code == "no_effect"
    assert path.read_bytes() == b"value = 1\r\n"
    assert git(workspace, "diff", "--name-only") == ""


@pytest.mark.parametrize("path", ["tests/test_main.py", "README.md", "../app/main.py", "app/.env"])
def test_replace_text_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    files = {
        "app/main.py": "value = 1\n",
        "app/.env": "SECRET=value\n",
        "tests/test_main.py": "def test_ok():\n    assert True\n",
        "README.md": "fixture\n",
    }
    tools = WorkspaceGitTools(repository(tmp_path, files=files))
    with pytest.raises(PathPolicyError):
        tools.replace_text(ReplaceTextArgs(path=path, old_text="value", new_text="other"))


def test_replace_text_non_utf8_is_rejected(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    path = recommit_bytes(workspace, "app/main.py", b"\xff\xfe\x00")
    result = WorkspaceGitTools(workspace).replace_text(
        ReplaceTextArgs(path="app/main.py", old_text="value", new_text="other")
    )
    assert not result.ok and result.error_code == "decode_error"
    assert path.read_bytes() == b"\xff\xfe\x00"


def test_replace_text_git_validation_failure_restores_original(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    path = workspace / "app" / "main.py"
    original = path.read_bytes()
    result = WorkspaceGitTools(workspace).replace_text(
        ReplaceTextArgs(path="app/main.py", old_text="value = 1", new_text="value = 2   ")
    )
    assert not result.ok and result.error_code == "edit_validation_failed"
    assert path.read_bytes() == original
    assert git(workspace, "diff", "--name-only") == ""


def test_replace_text_uses_atomic_replace_and_non_shell_git() -> None:
    source = inspect.getsource(WorkspaceGitTools)
    assert "os.replace" in source
    assert "shell=False" in source


def test_apply_patch_handles_lf_only_workspace_file(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    path = workspace / "app" / "main.py"
    path.write_bytes(b"value = 1\n")
    git(workspace, "add", "app/main.py")
    git(workspace, "commit", "-q", "--amend", "--no-edit")
    result = WorkspaceGitTools(workspace).apply_patch(
        ApplyPatchArgs(patch=patch("app/main.py", "value = 1", "value = 2"))
    )
    assert result.ok
    assert path.read_bytes() == b"value = 2\n"


def test_patch_check_failure_is_structured_and_hides_workspace(tmp_path: Path) -> None:
    tools = WorkspaceGitTools(repository(tmp_path))
    result = tools.apply_patch(ApplyPatchArgs(patch=patch("app/main.py", "missing = 1", "value = 2")))
    assert not result.ok and result.error_code == "patch_apply_failed"
    assert str(tmp_path.resolve()) not in result.output


@pytest.mark.parametrize(
    ("path", "error_code"),
    [
        ("tests/test_main.py", "patch_out_of_scope"),
        ("README.md", "patch_out_of_scope"),
        ("../outside.py", "patch_out_of_scope"),
    ],
)
def test_patch_path_scope_is_enforced(tmp_path: Path, path: str, error_code: str) -> None:
    tools = WorkspaceGitTools(repository(tmp_path))
    with pytest.raises(PathPolicyError) as error:
        tools.apply_patch(ApplyPatchArgs(patch=patch(path, "value = 1", "value = 2")))
    assert error.value.code == error_code


@pytest.mark.parametrize(
    "marker",
    [
        "new file mode 100644",
        "deleted file mode 100644",
        "rename from app/main.py",
        "rename to app/other.py",
        "GIT binary patch",
        "Binary files a/app/main.py and b/app/main.py differ",
        "--- /dev/null",
    ],
)
def test_forbidden_patch_operations_are_rejected(tmp_path: Path, marker: str) -> None:
    tools = WorkspaceGitTools(repository(tmp_path))
    with pytest.raises(PathPolicyError, match="forbidden"):
        tools.apply_patch(ApplyPatchArgs(patch=f"{patch('app/main.py', 'value = 1', 'value = 2')}{marker}\n"))


def test_rename_by_mismatched_header_is_rejected(tmp_path: Path) -> None:
    tools = WorkspaceGitTools(repository(tmp_path))
    value = patch("app/main.py", "value = 1", "value = 2").replace(
        "diff --git a/app/main.py b/app/main.py",
        "diff --git a/app/main.py b/app/other.py",
    )
    with pytest.raises(PathPolicyError, match="must match"):
        tools.apply_patch(ApplyPatchArgs(patch=value))


def test_more_than_five_files_is_rejected(tmp_path: Path) -> None:
    files = {f"app/file{number}.py": "value = 1\n" for number in range(6)}
    tools = WorkspaceGitTools(repository(tmp_path, files=files))
    value = "".join(patch(name, "value = 1", "value = 2") for name in files)
    with pytest.raises(PathPolicyError) as error:
        tools.apply_patch(ApplyPatchArgs(patch=value))
    assert error.value.code == "patch_too_large"


def test_patch_line_limit_is_enforced(tmp_path: Path) -> None:
    before = "".join(f"value_{number} = 1\n" for number in range(301))
    workspace = repository(tmp_path, files={"app/main.py": before})
    (workspace / "app" / "main.py").write_text(before.replace(" = 1", " = 2"), encoding="utf-8")
    value = git(workspace, "diff", "--no-ext-diff")
    git(workspace, "restore", "app/main.py")
    tools = WorkspaceGitTools(workspace)
    with pytest.raises(PathPolicyError) as error:
        tools.apply_patch(ApplyPatchArgs(patch=value))
    assert error.value.code == "patch_too_large"


def test_git_subprocess_is_explicitly_non_shell() -> None:
    assert "shell=False" in inspect.getsource(WorkspaceGitTools._run)

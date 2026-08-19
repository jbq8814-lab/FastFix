from pathlib import Path

import pytest

from fastfix.security.paths import PathPolicyError, WorkspacePathPolicy


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    return tmp_path


def test_resolve_relative_file_and_directory(workspace: Path) -> None:
    policy = WorkspacePathPolicy(workspace)
    assert policy.to_relative(policy.resolve("app/main.py", expect="file")) == "app/main.py"
    assert policy.to_relative(policy.resolve("app", expect="directory")) == "app"


def test_absolute_path_is_rejected_without_exposing_workspace(workspace: Path) -> None:
    policy = WorkspacePathPolicy(workspace)
    with pytest.raises(PathPolicyError) as error:
        policy.resolve(str(workspace / "app" / "main.py"))
    assert error.value.code == "path_not_allowed"
    assert str(workspace) not in str(error.value)


def test_parent_traversal_is_rejected(workspace: Path) -> None:
    with pytest.raises(PathPolicyError, match="traversal") as error:
        WorkspacePathPolicy(workspace).resolve("../outside.py")
    assert error.value.code == "path_not_allowed"


def test_missing_path_is_structured(workspace: Path) -> None:
    with pytest.raises(PathPolicyError) as error:
        WorkspacePathPolicy(workspace).resolve("missing.py")
    assert error.value.code == "path_not_found"


@pytest.mark.parametrize(
    ("path", "expect", "code"),
    [
        ("app", "file", "not_a_file"),
        ("app/main.py", "directory", "not_a_directory"),
    ],
)
def test_expected_path_type_is_enforced(workspace: Path, path: str, expect: str, code: str) -> None:
    with pytest.raises(PathPolicyError) as error:
        WorkspacePathPolicy(workspace).resolve(path, expect=expect)
    assert error.value.code == code


def test_allowed_paths_are_enforced(workspace: Path) -> None:
    policy = WorkspacePathPolicy(workspace, allowed_paths=("app",))
    assert policy.resolve("app/main.py").is_file()
    with pytest.raises(PathPolicyError) as error:
        policy.resolve("tests")
    assert error.value.code == "path_not_allowed"


@pytest.mark.parametrize("path", [".git/config", ".env", ".env.local"])
def test_sensitive_dot_paths_are_rejected(workspace: Path, path: str) -> None:
    with pytest.raises(PathPolicyError) as error:
        WorkspacePathPolicy(workspace).resolve(path)
    assert error.value.code == "sensitive_path"


def test_gitignore_is_allowed(workspace: Path) -> None:
    assert WorkspacePathPolicy(workspace).resolve(".gitignore").is_file()


@pytest.mark.parametrize("name", ["certificate.pem", "private.key", "id_rsa", "id_ed25519"])
def test_sensitive_key_files_are_rejected(workspace: Path, name: str) -> None:
    (workspace / name).write_text("secret", encoding="utf-8")
    with pytest.raises(PathPolicyError) as error:
        WorkspacePathPolicy(workspace).resolve(name)
    assert error.value.code == "sensitive_path"


def test_python_filename_containing_key_is_allowed(workspace: Path) -> None:
    path = workspace / "monkey.py"
    path.write_text("value = 1\n", encoding="utf-8")
    assert WorkspacePathPolicy(workspace).resolve("monkey.py") == path.resolve()


def test_symlink_escape_is_rejected_when_supported(workspace: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Symlink creation is unavailable: {error}")
    with pytest.raises(PathPolicyError) as policy_error:
        WorkspacePathPolicy(workspace).resolve("escape.txt")
    assert policy_error.value.code == "path_not_allowed"

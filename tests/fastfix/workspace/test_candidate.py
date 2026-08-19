import inspect
import stat
import subprocess
from pathlib import Path

import pytest

from fastfix.workspace.candidate import CandidateWorkspaceError, CandidateWorkspaceManager


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
    path.mkdir(parents=True)
    (path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(path, "init", "-q")
    git(path, "config", "user.name", "FastFix Tests")
    git(path, "config", "user.email", "fastfix@example.invalid")
    git(path, "config", "core.autocrlf", "false")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "baseline")
    return path


def state(path: Path) -> tuple[str, str, str]:
    return (
        git(path, "rev-parse", "HEAD").stdout.strip(),
        git(path, "branch", "--show-current").stdout.strip(),
        git(path, "status", "--porcelain=v1", "--untracked-files=all").stdout,
    )


def test_create_is_independent_detached_clean_and_preserves_source(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    before = state(source)
    (tmp_path / "candidates").mkdir()
    manager = CandidateWorkspaceManager(tmp_path / "candidates")
    with manager.create(source) as candidate:
        assert candidate.source_head == before[0]
        assert git(candidate.path, "rev-parse", "HEAD").stdout.strip() == before[0]
        assert git(candidate.path, "symbolic-ref", "--quiet", "HEAD", check=False).returncode == 1
        assert git(candidate.path, "remote").stdout == ""
        assert (candidate.path / ".git").is_dir() and not (candidate.path / ".git").is_symlink()
        assert git(candidate.path, "status", "--porcelain").stdout == ""
        (candidate.path / "app.py").write_text("value = 2\n", encoding="utf-8")
        assert (source / "app.py").read_text(encoding="utf-8") == "value = 1\n"
        assert git(candidate.path, "diff", "--name-only").stdout.strip() == "app.py"
        assert state(source) == before
    assert not candidate.path.exists()
    assert state(source) == before


def test_ignored_env_is_not_cloned(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    (source / ".env").write_text("SECRET=not-copied\n", encoding="utf-8")
    parent = tmp_path / "candidates"
    parent.mkdir()
    with CandidateWorkspaceManager(parent).create(source) as candidate:
        assert not (candidate.path / ".env").exists()


def test_tracked_sensitive_file_is_rejected(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    (source / ".env").write_text("SECRET=tracked\n", encoding="utf-8")
    git(source, "add", "-f", ".env")
    git(source, "commit", "-q", "-m", "track sensitive file")
    parent = tmp_path / "candidates"
    parent.mkdir()
    with pytest.raises(CandidateWorkspaceError) as error:
        CandidateWorkspaceManager(parent).create(source)
    assert error.value.code == "sensitive_tracked_path"
    assert list(parent.iterdir()) == []


def assert_dirty_source_is_rejected(tmp_path: Path, dirty_kind: str) -> None:
    source = repository(tmp_path / "source")
    if dirty_kind == "tracked":
        (source / "app.py").write_text("value = 2\n", encoding="utf-8")
    elif dirty_kind == "staged":
        (source / "app.py").write_text("value = 2\n", encoding="utf-8")
        git(source, "add", "app.py")
    else:
        (source / "notes.txt").write_text("untracked\n", encoding="utf-8")
    parent = tmp_path / "candidates"
    parent.mkdir()
    with pytest.raises(CandidateWorkspaceError) as error:
        CandidateWorkspaceManager(parent).create(source)
    assert error.value.code == "source_not_clean"
    assert list(parent.iterdir()) == []


def test_tracked_change_is_rejected(tmp_path: Path) -> None:
    assert_dirty_source_is_rejected(tmp_path, "tracked")


def test_staged_change_is_rejected(tmp_path: Path) -> None:
    assert_dirty_source_is_rejected(tmp_path, "staged")


def test_untracked_file_is_rejected(tmp_path: Path) -> None:
    assert_dirty_source_is_rejected(tmp_path, "untracked")


def test_non_git_and_empty_git_sources_are_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "candidates"
    parent.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(CandidateWorkspaceError):
        CandidateWorkspaceManager(parent).create(plain)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(CandidateWorkspaceError):
        CandidateWorkspaceManager(parent).create(empty)


def test_existing_target_is_rejected(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    parent = tmp_path / "candidates"
    parent.mkdir()
    target = parent / "existing"
    target.mkdir()
    with pytest.raises(CandidateWorkspaceError) as error:
        CandidateWorkspaceManager(parent).create(source, target=target)
    assert error.value.code == "target_exists"
    assert target.is_dir()


def test_failed_clone_removes_partial_target(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    blob = git(source, "rev-parse", "HEAD:app.py").stdout.strip()
    blob_path = source / ".git" / "objects" / blob[:2] / blob[2:]
    blob_path.chmod(stat.S_IWRITE)
    blob_path.unlink()
    parent = tmp_path / "candidates"
    parent.mkdir()
    target = parent / "partial"
    with pytest.raises(CandidateWorkspaceError):
        CandidateWorkspaceManager(parent).create(source, target=target)
    assert not target.exists()


def test_cleanup_is_idempotent_and_exception_safe(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    parent = tmp_path / "candidates"
    parent.mkdir()
    manager = CandidateWorkspaceManager(parent)
    candidate = manager.create(source)
    candidate.cleanup()
    candidate.cleanup()
    assert not candidate.path.exists()
    with pytest.raises(RuntimeError):
        with manager.create(source) as failed:
            raise RuntimeError("test")
    assert not failed.path.exists()


def test_manager_does_not_cleanup_another_managers_candidate(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    first_parent = tmp_path / "first"
    second_parent = tmp_path / "second"
    first_parent.mkdir()
    second_parent.mkdir()
    candidate = CandidateWorkspaceManager(first_parent).create(source)
    with pytest.raises(CandidateWorkspaceError) as error:
        CandidateWorkspaceManager(second_parent).cleanup(candidate)
    assert error.value.code == "not_managed" and candidate.path.is_dir()
    candidate.cleanup()


def test_target_inside_source_is_rejected(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    parent = tmp_path / "candidates"
    parent.mkdir()
    with pytest.raises(CandidateWorkspaceError) as error:
        CandidateWorkspaceManager(parent).create(source, target=source / "candidate")
    assert error.value.code == "unsafe_target"
    assert state(source)[2] == ""


def test_paths_with_spaces_are_supported_and_source_branch_is_unchanged(tmp_path: Path) -> None:
    source = repository(tmp_path / "source repository")
    before = state(source)
    parent = tmp_path / "candidate repositories"
    parent.mkdir()
    target = parent / "candidate repository"
    with CandidateWorkspaceManager(parent).create(source, target=target) as candidate:
        assert candidate.path == target.resolve()
        assert candidate.source_branch == before[1]
    assert state(source) == before


def test_subprocess_is_non_shell_and_environment_is_minimal() -> None:
    assert "shell=False" in inspect.getsource(CandidateWorkspaceManager._run)
    assert "API_KEY" not in CandidateWorkspaceManager._environment()
    assert set(CandidateWorkspaceManager._environment()) <= {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "PATHEXT",
    }

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-010-orm-attribute-mismatch"
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-010-orm-attribute-mismatch"


def hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def run(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_ff010_metadata_issue_and_gold_patch_are_consistent() -> None:
    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    patch = (TASK_DIR / "gold.patch").read_text(encoding="utf-8")
    assert task == {
        "task_id": "FF-010",
        "title": "User creation fails because the ORM attribute name is inconsistent",
        "category": "sqlalchemy_orm_mapping",
        "provenance": "synthetic",
        "fixture_repo": "benchmarks/fixture_repos/ff-010-orm-attribute-mismatch",
        "issue_file": "benchmarks/tasks/ff-010-orm-attribute-mismatch/issue.md",
        "gold_patch": "benchmarks/tasks/ff-010-orm-attribute-mismatch/gold.patch",
        "failing_tests": ["tests/test_users.py::test_created_user_can_be_fetched"],
        "test_command": "python -m pytest -q",
        "ruff_command": "python -m ruff check .",
        "allowed_paths": ["app"],
        "buggy_files": ["app/models.py"],
        "expected_buggy_result": {"passed": 2, "failed": 1},
        "expected_fixed_result": {"passed": 3, "failed": 0},
    }
    for key in ("fixture_repo", "issue_file", "gold_patch"):
        assert not Path(task[key]).is_absolute()
        assert (ROOT / task[key]).exists()
    assert set(hashes(FIXTURE)) == {
        ".gitignore",
        "app/__init__.py",
        "app/database.py",
        "app/main.py",
        "app/models.py",
        "app/schemas.py",
        "app/service.py",
        "pyproject.toml",
        "tests/test_users.py",
    }
    for observable in ("GET /health", "GET /users", "POST /users", "HTTP 500", "HTTP 201"):
        assert observable in issue
    assert "ORM mapping layer" in issue
    assert "username" not in issue.lower()
    assert "mapped_column" not in issue
    assert patch.count("diff --git ") == 1
    assert "--- a/app/models.py" in patch
    assert "+++ b/app/models.py" in patch
    changed_files = {
        line.split()[2].removeprefix("a/") for line in patch.splitlines() if line.startswith("diff --git ")
    }
    assert changed_files == {"app/models.py"}
    assert changed_files == set(task["buggy_files"])


def test_ff010_buggy_and_gold_states_are_reproducible(tmp_path: Path) -> None:
    canonical_before = hashes(FIXTURE)
    repository = tmp_path / "repository"
    shutil.copytree(FIXTURE, repository)
    try:
        for arguments in (
            ("git", "init", "-q"),
            ("git", "config", "user.name", "FastFix Tests"),
            ("git", "config", "user.email", "fastfix@example.invalid"),
            ("git", "config", "core.autocrlf", "false"),
            ("git", "add", "."),
            ("git", "commit", "-q", "-m", "buggy baseline"),
        ):
            assert run(repository, *arguments).returncode == 0

        targeted = run(
            repository,
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_users.py::test_created_user_can_be_fetched",
        )
        assert targeted.returncode == 1
        assert "assert 500 == 201" in output(targeted)

        buggy = run(repository, sys.executable, "-m", "pytest", "-q")
        assert buggy.returncode == 1
        assert "1 failed, 2 passed" in output(buggy)
        assert "assert 500 == 201" in output(buggy)
        assert run(repository, sys.executable, "-m", "ruff", "check", ".").returncode == 0

        patch = TASK_DIR / "gold.patch"
        assert run(repository, "git", "apply", "--check", str(patch)).returncode == 0
        assert run(repository, "git", "apply", str(patch)).returncode == 0
        assert run(repository, "git", "diff", "--name-status").stdout == "M\tapp/models.py\n"
        assert run(repository, "git", "diff", "--numstat").stdout == "1\t1\tapp/models.py\n"

        fixed_targeted = run(
            repository,
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_users.py::test_created_user_can_be_fetched",
        )
        assert fixed_targeted.returncode == 0
        assert "1 passed" in output(fixed_targeted)

        fixed = run(repository, sys.executable, "-m", "pytest", "-q")
        assert fixed.returncode == 0
        assert "3 passed" in output(fixed)
        assert run(repository, sys.executable, "-m", "ruff", "check", ".").returncode == 0
    finally:
        assert hashes(FIXTURE) == canonical_before

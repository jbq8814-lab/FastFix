import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-004-unhandled-service-exception"
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-004-unhandled-service-exception"


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


def test_ff004_metadata_issue_and_gold_patch_are_consistent() -> None:
    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    patch = (TASK_DIR / "gold.patch").read_text(encoding="utf-8")
    assert task["task_id"] == "FF-004"
    assert task["title"] == "Missing users return an internal server error"
    assert task["provenance"] == "synthetic"
    assert task["category"] == "fastapi_exception_handling"
    assert task["fixture_repo"] == "benchmarks/fixture_repos/ff-004-unhandled-service-exception"
    assert task["failing_tests"] == ["tests/test_users.py::test_missing_user_returns_404"]
    assert task["test_command"] == "python -m pytest -q"
    assert task["ruff_command"] == "python -m ruff check ."
    assert task["allowed_paths"] == ["app"]
    assert task["buggy_files"] == ["app/main.py"]
    assert task["expected_buggy_result"] == {"passed": 2, "failed": 1}
    assert task["expected_fixed_result"] == {"passed": 3, "failed": 0}
    for key in ("fixture_repo", "issue_file", "gold_patch"):
        assert not Path(task[key]).is_absolute()
        assert (ROOT / task[key]).exists()
    for path in task["allowed_paths"] + task["buggy_files"]:
        assert not Path(path).is_absolute()
    expected_files = {
        ".gitignore",
        "app/__init__.py",
        "app/main.py",
        "app/schemas.py",
        "app/service.py",
        "pyproject.toml",
        "tests/test_users.py",
    }
    assert set(hashes(FIXTURE)) == expected_files
    assert "GET /health" in issue
    assert "GET /users/7" in issue
    assert "GET /users/404" in issue
    assert "HTTP 500" in issue
    assert "HTTP 404" in issue
    assert '{"detail": "User not found"}' in issue
    assert "service behavior should not be modified" in issue
    assert "without changing tests" in issue
    assert "UserNotFoundError" not in issue
    assert "HTTPException" not in issue
    assert patch.count("diff --git ") == 1
    assert "--- a/app/main.py" in patch
    assert "+++ b/app/main.py" in patch
    changed_files = {
        line.split()[2].removeprefix("a/") for line in patch.splitlines() if line.startswith("diff --git ")
    }
    assert changed_files == {"app/main.py"}
    assert changed_files == set(task["buggy_files"])


def test_ff004_buggy_and_gold_states_are_reproducible(tmp_path: Path) -> None:
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

        health = run(
            repository,
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_users.py::test_health_check",
        )
        assert health.returncode == 0
        assert "1 passed" in output(health)

        existing_user = run(
            repository,
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_users.py::test_get_existing_user",
        )
        assert existing_user.returncode == 0
        assert "1 passed" in output(existing_user)

        buggy = run(repository, sys.executable, "-m", "pytest", "-q")
        assert buggy.returncode == 1
        assert "1 failed, 2 passed" in output(buggy)
        assert "assert 500 == 404" in output(buggy)

        patch = TASK_DIR / "gold.patch"
        assert run(repository, "git", "apply", "--check", str(patch)).returncode == 0
        assert run(repository, "git", "apply", str(patch)).returncode == 0
        assert run(repository, "git", "diff", "--name-status").stdout == "M\tapp/main.py\n"
        assert run(repository, "git", "diff", "--numstat").stdout == "6\t3\tapp/main.py\n"

        fixed = run(repository, sys.executable, "-m", "pytest", "-q")
        assert fixed.returncode == 0
        assert "3 passed" in output(fixed)
        assert run(repository, sys.executable, "-m", "ruff", "check", ".").returncode == 0
    finally:
        assert hashes(FIXTURE) == canonical_before

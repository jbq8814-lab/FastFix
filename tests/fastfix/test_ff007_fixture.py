import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-007-missing-service-return"
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-007-missing-service-return"


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


def test_ff007_metadata_issue_and_gold_patch_are_consistent() -> None:
    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    patch = (TASK_DIR / "gold.patch").read_text(encoding="utf-8")
    assert task["task_id"] == "FF-007"
    assert task["title"] == "Existing user detail returns an internal server error"
    assert task["category"] == "fastapi_response_validation"
    assert task["provenance"] == "synthetic"
    assert task["fixture_repo"] == "benchmarks/fixture_repos/ff-007-missing-service-return"
    assert task["failing_tests"] == ["tests/test_users.py::test_get_existing_user"]
    assert task["test_command"] == "python -m pytest -q"
    assert task["ruff_command"] == "python -m ruff check ."
    assert task["allowed_paths"] == ["app"]
    assert task["buggy_files"] == ["app/service.py"]
    assert task["expected_buggy_result"] == {"passed": 2, "failed": 1}
    assert task["expected_fixed_result"] == {"passed": 3, "failed": 0}
    for key in ("fixture_repo", "issue_file", "gold_patch"):
        assert not Path(task[key]).is_absolute()
        assert (ROOT / task[key]).exists()
    for path in task["allowed_paths"] + task["buggy_files"]:
        assert not Path(path).is_absolute()
    assert set(hashes(FIXTURE)) == {
        ".gitignore",
        "app/__init__.py",
        "app/main.py",
        "app/schemas.py",
        "app/service.py",
        "pyproject.toml",
        "tests/test_users.py",
    }
    assert "GET /health" in issue
    assert "GET /users" in issue
    assert "GET /users/1" in issue
    assert "HTTP 500" in issue
    assert "HTTP 200" in issue
    assert "existing user JSON" in issue
    assert "API routes, response schemas, or tests" in issue
    for disclosure in ("missing return", "return user", "none", "responsevalidationerror"):
        assert disclosure not in issue.lower()
    assert patch
    assert patch.count("diff --git ") == 1
    assert "--- a/app/service.py" in patch
    assert "+++ b/app/service.py" in patch
    changed_files = {
        line.split()[2].removeprefix("a/") for line in patch.splitlines() if line.startswith("diff --git ")
    }
    assert changed_files == {"app/service.py"}
    assert changed_files == set(task["buggy_files"])


def test_ff007_buggy_and_gold_states_are_reproducible(tmp_path: Path) -> None:
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

        users = run(
            repository,
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_users.py::test_list_users",
        )
        assert users.returncode == 0
        assert "1 passed" in output(users)

        buggy = run(repository, sys.executable, "-m", "pytest", "-q")
        assert buggy.returncode == 1
        assert "1 failed, 2 passed" in output(buggy)
        assert "assert 500 == 200" in output(buggy)

        patch = TASK_DIR / "gold.patch"
        assert run(repository, "git", "apply", "--check", str(patch)).returncode == 0
        assert run(repository, "git", "apply", str(patch)).returncode == 0
        assert run(repository, "git", "diff", "--name-status").stdout == "M\tapp/service.py\n"
        assert run(repository, "git", "diff", "--numstat").stdout == "1\t0\tapp/service.py\n"

        fixed = run(repository, sys.executable, "-m", "pytest", "-q")
        assert fixed.returncode == 0
        assert "3 passed" in output(fixed)
        assert run(repository, sys.executable, "-m", "ruff", "check", ".").returncode == 0
    finally:
        assert hashes(FIXTURE) == canonical_before

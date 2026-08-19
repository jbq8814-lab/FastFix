import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-009-session-lifecycle"
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-009-session-lifecycle"


def hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts
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


def test_ff009_metadata_issue_and_gold_patch_are_consistent() -> None:
    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    patch = (TASK_DIR / "gold.patch").read_text(encoding="utf-8")
    assert task == {
        "task_id": "FF-009",
        "title": "SQLAlchemy Session not closed after request ends",
        "category": "sqlalchemy_session_lifecycle",
        "provenance": "synthetic",
        "fixture_repo": "benchmarks/fixture_repos/ff-009-session-lifecycle",
        "issue_file": "benchmarks/tasks/ff-009-session-lifecycle/issue.md",
        "gold_patch": "benchmarks/tasks/ff-009-session-lifecycle/gold.patch",
        "failing_tests": ["tests/test_users.py::test_get_db_must_be_generator"],
        "test_command": "python -m pytest -q",
        "ruff_command": "python -m ruff check .",
        "allowed_paths": ["app"],
        "buggy_files": ["app/database.py"],
        "expected_buggy_result": {"passed": 3, "failed": 1},
        "expected_fixed_result": {"passed": 4, "failed": 0},
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
    for observable in ("GET /health", "GET /users", "POST /users", "HTTP 200", "HTTP 201"):
        assert observable in issue
    assert "get_db()" in issue
    assert hashlib.sha256((TASK_DIR / "gold.patch").read_bytes()).hexdigest() == (
        "9c5085a0a01b52045f120bf3d6cca600f38f104afb17d1aa6c5a394dc91fb584"
    )
    assert patch.count("diff --git ") == 1
    assert "--- a/app/database.py" in patch
    assert "+++ b/app/database.py" in patch
    changed_files = {
        line.split()[2].removeprefix("a/") for line in patch.splitlines() if line.startswith("diff --git ")
    }
    assert changed_files == {"app/database.py"}
    assert changed_files == set(task["buggy_files"])


def test_ff009_buggy_and_gold_states_are_reproducible(tmp_path: Path) -> None:
    canonical_before = hashes(FIXTURE)
    repository = tmp_path / "repository"
    shutil.copytree(FIXTURE, repository, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
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
            "tests/test_users.py::test_get_db_must_be_generator",
        )
        assert targeted.returncode == 1
        assert "get_db must use 'yield'" in output(targeted)

        buggy = run(repository, sys.executable, "-m", "pytest", "-q")
        assert buggy.returncode == 1
        assert "1 failed, 4 passed" in output(buggy)
        assert "get_db must use 'yield'" in output(buggy)
        assert run(repository, sys.executable, "-m", "ruff", "check", "--no-cache", ".").returncode == 0

        database = repository / "app" / "database.py"
        before = database.read_bytes()
        removed = (
            b"    # BUG: directly returns Session without yield/finally/close\r\n"
            b"    # Session is never closed after request ends\r\n"
            b"    return SessionLocal()\r\n"
        )
        added = b"    db = SessionLocal()\r\n    try:\r\n        yield db\r\n    finally:\r\n        db.close()\r\n"
        patch_text = (TASK_DIR / "gold.patch").read_text(encoding="utf-8")
        assert all(f"-{line}" in patch_text for line in removed.decode().splitlines())
        assert all(f"+{line}" in patch_text for line in added.decode().splitlines())
        assert before.count(removed) == 1
        database.write_bytes(before.replace(removed, added))
        assert run(repository, "git", "diff", "--name-status").stdout == "M\tapp/database.py\n"
        assert run(repository, "git", "diff", "--numstat").stdout == "5\t3\tapp/database.py\n"

        fixed_targeted = run(
            repository,
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_users.py::test_get_db_must_be_generator",
        )
        assert fixed_targeted.returncode == 0
        assert "1 passed" in output(fixed_targeted)

        fixed = run(repository, sys.executable, "-m", "pytest", "-q")
        assert fixed.returncode == 0
        assert "5 passed" in output(fixed)
        assert run(repository, sys.executable, "-m", "ruff", "check", "--no-cache", ".").returncode == 0
    finally:
        assert hashes(FIXTURE) == canonical_before

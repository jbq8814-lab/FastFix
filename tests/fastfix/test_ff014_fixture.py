import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-014-awaiting-sync-service"
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-014-awaiting-sync-service"


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


def test_ff014_metadata_issue_and_gold_patch_are_consistent() -> None:
    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    patch = (TASK_DIR / "gold.patch").read_text(encoding="utf-8")
    assert task == {
        "task_id": "FF-014",
        "title": "The user detail endpoint fails while other user routes work",
        "category": "sync_async_service_boundary",
        "provenance": "synthetic",
        "fixture_repo": "benchmarks/fixture_repos/ff-014-awaiting-sync-service",
        "issue_file": "benchmarks/tasks/ff-014-awaiting-sync-service/issue.md",
        "gold_patch": "benchmarks/tasks/ff-014-awaiting-sync-service/gold.patch",
        "failing_tests": ["tests/test_users.py::test_user_detail"],
        "test_command": "python -m pytest -q",
        "ruff_command": "python -m ruff check .",
        "allowed_paths": ["app"],
        "buggy_files": ["app/main.py"],
        "expected_buggy_result": {"passed": 2, "failed": 1},
        "expected_fixed_result": {"passed": 3, "failed": 0},
    }
    for key in ("fixture_repo", "issue_file", "gold_patch"):
        assert not Path(task[key]).is_absolute()
        assert (ROOT / task[key]).exists()
    assert set(hashes(FIXTURE)) == {
        ".gitignore",
        "app/__init__.py",
        "app/main.py",
        "app/service.py",
        "pyproject.toml",
        "tests/test_users.py",
    }
    for observable in ("GET /health", "GET /users", "GET /users/1", "HTTP 500", "HTTP 200"):
        assert observable in issue
    for disclosure in ("await", "async", "sync", "service"):
        assert disclosure not in issue.lower()
    assert patch.count("diff --git ") == 1
    assert "--- a/app/main.py" in patch
    assert "+++ b/app/main.py" in patch
    changed_files = {
        line.split()[2].removeprefix("a/") for line in patch.splitlines() if line.startswith("diff --git ")
    }
    assert changed_files == {"app/main.py"}
    assert changed_files == set(task["buggy_files"])


def test_ff014_buggy_and_gold_states_are_reproducible(tmp_path: Path) -> None:
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
            "tests/test_users.py::test_user_detail",
        )
        assert targeted.returncode == 1
        assert "assert 500 == 200" in output(targeted)

        buggy = run(repository, sys.executable, "-m", "pytest", "-q")
        assert buggy.returncode == 1
        assert "1 failed, 2 passed" in output(buggy)
        assert "assert 500 == 200" in output(buggy)
        assert run(repository, sys.executable, "-m", "ruff", "check", "--no-cache", ".").returncode == 0

        main = repository / "app" / "main.py"
        before = main.read_text(encoding="utf-8")
        patch = TASK_DIR / "gold.patch"
        assert run(repository, "git", "apply", "--check", str(patch)).returncode == 0
        assert run(repository, "git", "apply", str(patch)).returncode == 0
        after = main.read_text(encoding="utf-8")
        assert run(repository, "git", "diff", "--name-status").stdout == "M\tapp/main.py\n"
        assert run(repository, "git", "diff", "--numstat").stdout == "1\t1\tapp/main.py\n"
        assert before.count("    return await user_service.get_user(user_id)\n") == 1
        assert (
            before.replace(
                "    return await user_service.get_user(user_id)\n",
                "    return user_service.get_user(user_id)\n",
            )
            == after
        )

        fixed_targeted = run(
            repository,
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_users.py::test_user_detail",
        )
        assert fixed_targeted.returncode == 0
        assert "1 passed" in output(fixed_targeted)

        fixed = run(repository, sys.executable, "-m", "pytest", "-q")
        assert fixed.returncode == 0
        assert "3 passed" in output(fixed)
        assert run(repository, sys.executable, "-m", "ruff", "check", "--no-cache", ".").returncode == 0
    finally:
        assert hashes(FIXTURE) == canonical_before

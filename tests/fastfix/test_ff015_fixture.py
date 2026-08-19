import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-015-environment-variable-mapping"
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-015-environment-variable-mapping"


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


def test_ff015_metadata_issue_and_gold_patch_are_consistent() -> None:
    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    patch = (TASK_DIR / "gold.patch").read_text(encoding="utf-8")
    assert task == {
        "task_id": "FF-015",
        "title": "The configured application name is ignored",
        "category": "application_configuration_mapping",
        "provenance": "synthetic",
        "fixture_repo": "benchmarks/fixture_repos/ff-015-environment-variable-mapping",
        "issue_file": "benchmarks/tasks/ff-015-environment-variable-mapping/issue.md",
        "gold_patch": "benchmarks/tasks/ff-015-environment-variable-mapping/gold.patch",
        "failing_tests": ["tests/test_config.py::test_app_name"],
        "test_command": "python -m pytest -q",
        "ruff_command": "python -m ruff check .",
        "allowed_paths": ["app"],
        "buggy_files": ["app/config.py"],
        "expected_buggy_result": {"passed": 2, "failed": 1},
        "expected_fixed_result": {"passed": 3, "failed": 0},
    }
    for key in ("fixture_repo", "issue_file", "gold_patch"):
        assert not Path(task[key]).is_absolute()
        assert (ROOT / task[key]).exists()
    assert set(hashes(FIXTURE)) == {
        ".gitignore",
        "app/__init__.py",
        "app/config.py",
        "app/main.py",
        "pyproject.toml",
        "tests/conftest.py",
        "tests/test_config.py",
    }
    for observable in ("GET /health", "GET /config/version", "GET /config/name", "HTTP 200"):
        assert observable in issue
    for disclosure in ("fastfix_app_name", "fastfix_service_name", "config.py", "os.getenv"):
        assert disclosure not in issue.lower()
    assert patch.count("diff --git ") == 1
    assert "--- a/app/config.py" in patch
    assert "+++ b/app/config.py" in patch
    changed_files = {
        line.split()[2].removeprefix("a/") for line in patch.splitlines() if line.startswith("diff --git ")
    }
    assert changed_files == {"app/config.py"}
    assert changed_files == set(task["buggy_files"])


def test_ff015_buggy_and_gold_states_are_reproducible(tmp_path: Path) -> None:
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
            "tests/test_config.py::test_app_name",
        )
        assert targeted.returncode == 1
        assert "{'name': 'FastFix'}" in output(targeted)

        buggy = run(repository, sys.executable, "-m", "pytest", "-q")
        assert buggy.returncode == 1
        assert "1 failed, 2 passed" in output(buggy)
        assert "{'name': 'FastFix'}" in output(buggy)
        assert run(repository, sys.executable, "-m", "ruff", "check", "--no-cache", ".").returncode == 0

        config = repository / "app" / "config.py"
        before = config.read_text(encoding="utf-8")
        patch = TASK_DIR / "gold.patch"
        assert run(repository, "git", "apply", "--check", str(patch)).returncode == 0
        assert run(repository, "git", "apply", str(patch)).returncode == 0
        after = config.read_text(encoding="utf-8")
        assert run(repository, "git", "diff", "--name-status").stdout == "M\tapp/config.py\n"
        assert run(repository, "git", "diff", "--numstat").stdout == "1\t1\tapp/config.py\n"
        assert before.count('    app_name = os.getenv("FASTFIX_SERVICE_NAME", "FastFix")\n') == 1
        assert (
            before.replace(
                '    app_name = os.getenv("FASTFIX_SERVICE_NAME", "FastFix")\n',
                '    app_name = os.getenv("FASTFIX_APP_NAME", "FastFix")\n',
            )
            == after
        )

        fixed_targeted = run(
            repository,
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_config.py::test_app_name",
        )
        assert fixed_targeted.returncode == 0
        assert "1 passed" in output(fixed_targeted)

        fixed = run(repository, sys.executable, "-m", "pytest", "-q")
        assert fixed.returncode == 0
        assert "3 passed" in output(fixed)
        assert run(repository, sys.executable, "-m", "ruff", "check", "--no-cache", ".").returncode == 0
    finally:
        assert hashes(FIXTURE) == canonical_before

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from fastfix.sandbox.docker import DockerValidationBackend
from fastfix.tools.validation import RunPytestArgs, RunRuffArgs, WorkspaceValidationTools

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"
IMAGE = "fastfix-validation:ff001-v1"

pytestmark = pytest.mark.docker


@pytest.fixture(autouse=True)
def require_explicit_docker_selection(request) -> None:
    if "docker" not in request.config.option.markexpr:
        pytest.skip("Run explicitly with -m docker.")


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


def hashes(repository: Path) -> dict[str, str]:
    return {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repository.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def candidate(tmp_path: Path):
    workspace = tmp_path / "candidate"
    shutil.copytree(FIXTURE, workspace)
    git(workspace, "init", "-q")
    git(workspace, "config", "user.name", "FastFix Docker Tests")
    git(workspace, "config", "user.email", "fastfix@example.invalid")
    git(workspace, "config", "core.autocrlf", "false")
    git(workspace, "add", ".")
    git(workspace, "commit", "-q", "-m", "baseline")
    return workspace


def tools(candidate: Path) -> WorkspaceValidationTools:
    return WorkspaceValidationTools(
        candidate,
        backend=DockerValidationBackend(candidate, image=IMAGE),
    )


def test_real_docker_pytest_and_ruff_pass_and_fail(candidate: Path) -> None:
    validation = tools(candidate)
    failed_pytest = validation.run_pytest(
        RunPytestArgs(
            scope="targeted",
            targets=["tests/test_users.py::test_get_user_returns_user"],
        )
    )
    assert not failed_pytest.ok and failed_pytest.error_code == "validation_failed"
    main = candidate / "app" / "main.py"
    main.write_text(
        main.read_text(encoding="utf-8").replace("return fetch_user", "return await fetch_user"), encoding="utf-8"
    )
    passed_pytest = validation.run_pytest(
        RunPytestArgs(
            scope="targeted",
            targets=["tests/test_users.py::test_get_user_returns_user"],
        )
    )
    passed_ruff = validation.run_ruff(RunRuffArgs())
    assert passed_pytest.ok and passed_ruff.ok
    service = candidate / "app" / "service.py"
    service.write_text(f"import os\n{service.read_text(encoding='utf-8')}", encoding="utf-8")
    failed_ruff = validation.run_ruff(RunRuffArgs())
    assert not failed_ruff.ok and failed_ruff.error_code == "validation_failed"


def test_real_docker_timeout_and_large_output_are_bounded(candidate: Path) -> None:
    timeout_test = candidate / "tests" / "test_timeout.py"
    timeout_test.write_text("import time\n\ndef test_timeout():\n    time.sleep(20)\n", encoding="utf-8")
    validation = tools(candidate)
    timed_out = validation.run_pytest(
        RunPytestArgs(
            scope="targeted",
            targets=["tests/test_timeout.py::test_timeout"],
            timeout_seconds=5,
        )
    )
    assert not timed_out.ok and timed_out.error_code == "command_timeout"
    loud_test = candidate / "tests" / "test_loud.py"
    loud_test.write_text(
        "def test_loud():\n    print('x' * 1_100_000)\n    assert False\n",
        encoding="utf-8",
    )
    loud = validation.run_pytest(
        RunPytestArgs(
            scope="targeted",
            targets=["tests/test_loud.py::test_loud"],
        )
    )
    assert not loud.ok and loud.error_code == "validation_failed"
    assert loud.metadata["output_truncated"] and len(loud.output) < 20_100


def test_real_docker_security_and_candidate_immutability(candidate: Path) -> None:
    before_hashes = hashes(candidate)
    before_status = git(candidate, "status", "--porcelain=v1", "--untracked-files=all")
    result = tools(candidate).run_ruff(RunRuffArgs())
    assert result.ok
    assert result.metadata["container_uid"] != 0
    assert not result.metadata["candidate_writable"]
    assert not result.metadata["docker_socket_present"]
    assert result.metadata["network_mode"] == "none"
    assert hashes(candidate) == before_hashes
    assert git(candidate, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    containers = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=fastfix-validation-", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )
    assert containers.stdout.strip() == ""

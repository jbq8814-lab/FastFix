import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from fastfix.agents.repair import FastFixRepairAgent
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.repair.evaluation import evaluate_ff001_repair
from fastfix.repair.models import SubmitRepairArgs
from fastfix.tools.repair import build_repair_registry
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"


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


def hashes(repository: Path) -> dict[str, str]:
    return {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repository.rglob("*")
        if path.is_file()
    }


def action(tool: str, arguments: dict, number: int) -> dict:
    return {"tool": tool, "arguments": arguments, "tool_call_id": str(number)}


def test_ff001_complete_structured_repair(tmp_path: Path) -> None:
    canonical_before = hashes(FIXTURE)
    repository = tmp_path / "repository"
    shutil.copytree(FIXTURE, repository)
    for arguments in (
        ("git", "init", "-q"),
        ("git", "config", "user.name", "FastFix Tests"),
        ("git", "config", "user.email", "fastfix@example.invalid"),
        ("git", "config", "core.autocrlf", "false"),
        ("git", "add", "."),
        ("git", "commit", "-q", "-m", "baseline"),
    ):
        assert run(repository, *arguments).returncode == 0
    initial = run(repository, sys.executable, "-m", "pytest", "-q")
    assert initial.returncode == 1
    assert "1 failed, 1 passed" in initial.stdout + initial.stderr

    submission = {
        "summary": "Resolve the asynchronous service result.",
        "root_cause": "The route returned the service coroutine.",
        "changed_files": ["app/main.py"],
        "tests_run": ["targeted", "regression", "ruff"],
        "risk_notes": [],
        "confidence": 1.0,
    }
    actions = [
        action("inspect_fastapi_routes", {}, 1),
        action("read_file", {"path": "app/main.py"}, 2),
        action("read_file", {"path": "app/service.py"}, 3),
        action(
            "replace_text",
            {
                "path": "app/main.py",
                "old_text": "    return fetch_user(user_id)",
                "new_text": "    return await fetch_user(user_id)",
            },
            4,
        ),
        action(
            "run_pytest",
            {
                "scope": "targeted",
                "targets": ["tests/test_users.py::test_get_user_returns_user"],
            },
            5,
        ),
        action("run_pytest", {"scope": "regression"}, 6),
        action("run_ruff", {}, 7),
        action("show_git_diff", {}, 8),
        action("submit_repair", submission, 9),
    ]
    model = DeterministicToolcallModel(
        outputs=[make_toolcall_output(None, [{"id": item["tool_call_id"]}], [item]) for item in actions],
        cost_per_call=0,
    )
    environment = FastFixRepairEnvironment(
        registry=build_repair_registry(repository, python_executable=Path(sys.executable)),
        workspace=repository,
    )
    agent = FastFixRepairAgent(
        model,
        environment,
        system_template="Use structured repair tools.",
        instance_template="{{ task }}",
        cost_limit=0,
    )
    result = agent.run("FF-001")
    submitted = SubmitRepairArgs.model_validate(json.loads(result["submission"])["submission"])
    diff = run(repository, "git", "diff", "--no-ext-diff").stdout
    changed_files = run(repository, "git", "diff", "--name-only").stdout.splitlines()
    evaluation = evaluate_ff001_repair(
        submission=submitted,
        patch=diff,
        changed_files=changed_files,
        targeted_passed=environment.repair_state.targeted_test_revision == environment.repair_state.revision,
        regression_passed=environment.repair_state.regression_test_revision == environment.repair_state.revision,
        ruff_passed=environment.repair_state.ruff_revision == environment.repair_state.revision,
    )
    assert evaluation.resolved
    assert changed_files == ["app/main.py"]
    assert hashes(FIXTURE) == canonical_before

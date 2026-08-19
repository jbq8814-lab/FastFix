import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fastfix.agents.repair import FastFixRepairAgent
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.models.tool_call import FastFixLitellmModel
from fastfix.repair.evaluation import RepairEvaluation, evaluate_ff001_repair
from fastfix.repair.models import SubmitRepairArgs, get_reopen_repair_tool, get_submit_repair_tool
from fastfix.tools.repair import build_repair_registry
from minisweagent.config import get_config_from_spec

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-001-missing-await"
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"
CONFIG = ROOT / "src" / "fastfix" / "config" / "repair.yaml"
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd"}


def safe_environment() -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    return environment | {"NO_COLOR": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def run(command: list[str], cwd: Path, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        env=safe_environment(),
    )


def combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def source_hashes(repository: Path) -> dict[str, str]:
    return {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(repository.rglob("*"))
        if path.is_file()
        and not IGNORED_PARTS.intersection(path.relative_to(repository).parts)
        and path.suffix not in IGNORED_SUFFIXES
    }


def initialize_repository(repository: Path) -> None:
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "FastFix Repair"],
        ["git", "config", "user.email", "fastfix@example.invalid"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "buggy baseline"],
    ):
        result = run(command, repository)
        if result.returncode:
            raise RuntimeError(f"{' '.join(command)} failed:\n{combined_output(result)}")


def verify_initial_failure(repository: Path) -> str:
    result = run([sys.executable, "-m", "pytest", "-q"], repository)
    output = combined_output(result)
    if (
        result.returncode == 0
        or re.findall(r"(\d+) failed", output) != ["1"]
        or re.findall(r"(\d+) passed", output) != ["1"]
    ):
        raise RuntimeError(f"Initial fixture was not exactly 1 failed and 1 passed:\n{output}")
    return output


def model_cost_available(model_name: str) -> bool:
    import litellm

    try:
        info = litellm.get_model_info(model_name)
    except Exception:
        return False
    return info.get("input_cost_per_token") is not None and info.get("output_cost_per_token") is not None


def token_usage(trajectory: dict[str, Any]) -> dict[str, int | None]:
    input_tokens = 0
    output_tokens = 0
    found = False
    for message in trajectory.get("messages", []):
        usage = message.get("extra", {}).get("response", {}).get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        if prompt_tokens is not None:
            input_tokens += prompt_tokens
            found = True
        if completion_tokens is not None:
            output_tokens += completion_tokens
            found = True
    return {
        "input_tokens": input_tokens if found else None,
        "output_tokens": output_tokens if found else None,
    }


def parse_submission(value: str) -> SubmitRepairArgs | None:
    if not value:
        return None
    try:
        return SubmitRepairArgs.model_validate(json.loads(value)["submission"])
    except (json.JSONDecodeError, KeyError, TypeError, ValidationError):
        return None


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_results(
    output_dir: Path,
    *,
    summary: dict[str, Any],
    trajectory: dict[str, Any],
    submission: SubmitRepairArgs | None,
    evaluation: RepairEvaluation,
    tool_calls: list[dict[str, object]],
    patch: str,
    changed_files: list[str],
    pytest_log: str,
    ruff_log: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "trajectory.json", trajectory)
    write_json(
        output_dir / "submission.json",
        submission.model_dump(mode="json") if submission else None,
    )
    write_json(output_dir / "evaluation.json", evaluation.model_dump(mode="json"))
    write_json(output_dir / "tool-calls.json", tool_calls)
    (output_dir / "patch.diff").write_text(patch, encoding="utf-8")
    (output_dir / "changed-files.txt").write_text(
        "".join(f"{path}\n" for path in changed_files),
        encoding="utf-8",
    )
    (output_dir / "pytest.log").write_text(pytest_log, encoding="utf-8")
    (output_dir / "ruff.log").write_text(ruff_log, encoding="utf-8")


def validate_preflight(model_name: str, output_dir: Path) -> None:
    if not model_name.strip():
        raise ValueError("Model name must not be empty.")
    if os.getenv("OPENAI_BASE_URL") and "/" not in model_name:
        raise ValueError("Custom OPENAI_BASE_URL requires a provider-qualified model name.")
    if output_dir.exists():
        raise FileExistsError(f"Result directory already exists: {output_dir}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    validate_preflight(args.model, args.output_dir)

    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    config = get_config_from_spec(CONFIG)
    cost_available = model_cost_available(args.model)
    canonical_before = source_hashes(FIXTURE)

    with tempfile.TemporaryDirectory(prefix=".ff001-repair-", dir=ROOT / "benchmarks") as temp:
        repository = Path(temp) / "repository"
        shutil.copytree(FIXTURE, repository)
        initial_hashes = source_hashes(repository)
        initialize_repository(repository)
        verify_initial_failure(repository)

        registry = build_repair_registry(
            repository,
            python_executable=Path(sys.executable),
            allowed_source_paths=tuple(task["allowed_paths"]),
        )
        tool_schemas = [
            *registry.get_openai_tools(),
            get_submit_repair_tool(),
            get_reopen_repair_tool(),
        ]
        model = FastFixLitellmModel(
            model_name=args.model,
            tool_schemas=tool_schemas,
            allowed_tool_names={schema["function"]["name"] for schema in tool_schemas},
            cost_tracking="default" if cost_available else "ignore_errors",
            **config["model"],
        )
        environment = FastFixRepairEnvironment(
            registry=registry,
            workspace=repository,
            regression_targets=("tests",),
            ruff_paths=tuple(task["allowed_paths"]),
        )
        agent = FastFixRepairAgent(
            model,
            environment,
            output_path=args.output_dir / "trajectory.json",
            **config["agent"],
        )

        start = time.monotonic()
        agent_error = ""
        try:
            agent.run(
                task["task_id"],
                issue=issue,
                failing_test=task["failing_tests"][0],
                allowed_source_paths=", ".join(task["allowed_paths"]),
            )
        except Exception as error:
            agent_error = f"{type(error).__name__}: {error}"
        elapsed_seconds = round(time.monotonic() - start, 3)

        trajectory = agent.serialize()
        submission = parse_submission(trajectory["info"].get("submission", ""))
        patch_result = run(["git", "diff", "--no-ext-diff", "HEAD"], repository)
        changed_result = run(["git", "diff", "--name-only", "HEAD"], repository)
        changed_files = sorted(path for path in changed_result.stdout.splitlines() if path)
        pytest_result = run([sys.executable, "-m", "pytest", "-q"], repository)
        ruff_result = run([sys.executable, "-m", "ruff", "check", "app"], repository)
        state = environment.repair_state
        targeted_passed = (
            state.targeted_test_revision == state.revision
            and state.targeted_test_result is not None
            and state.targeted_test_result.get("returncode") == 0
        )
        regression_passed = (
            state.regression_test_revision == state.revision
            and state.regression_test_result is not None
            and state.regression_test_result.get("returncode") == 0
            and pytest_result.returncode == 0
        )
        ruff_passed = (
            state.ruff_revision == state.revision
            and state.ruff_result is not None
            and state.ruff_result.get("returncode") == 0
            and ruff_result.returncode == 0
        )
        evaluation = evaluate_ff001_repair(
            submission=submission,
            patch=patch_result.stdout,
            changed_files=changed_files,
            targeted_passed=targeted_passed,
            regression_passed=regression_passed,
            ruff_passed=ruff_passed,
        )
        model_stats = trajectory["info"]["model_stats"]
        environment_info = trajectory["info"]["fastfix_environment"]
        replace_text_calls = sum(call["tool_name"] == "replace_text" for call in environment.tool_call_history)
        apply_patch_failures = sum(
            call["tool_name"] == "apply_patch" and not call["ok"] and call["error_code"] != "patch_retry_limit"
            for call in environment.tool_call_history
        )
        patch_retry_limit_hits = sum(
            call["tool_name"] == "apply_patch" and call["error_code"] == "patch_retry_limit"
            for call in environment.tool_call_history
        )
        summary = {
            "task_id": task["task_id"],
            "run_id": args.output_dir.name,
            "agent": "fastfix-structured-repair",
            "system_revision": "safe-replace-v1",
            "evaluation_role": "development_regression",
            "metric_eligible": False,
            "metric_exclusion_reason": ("The editing strategy was changed after observing FF-001 run-001."),
            "model": args.model,
            "exit_status": trajectory["info"].get("exit_status", ""),
            "submitted": evaluation.submitted,
            "resolved": evaluation.resolved,
            "api_calls": model_stats["api_calls"],
            "instance_cost": model_stats["instance_cost"] if cost_available else None,
            "cost_status": "measured" if cost_available else "unavailable",
            **token_usage(trajectory),
            "elapsed_seconds": elapsed_seconds,
            "tool_call_count": environment_info["tool_call_count"],
            "tool_names": environment_info["tool_names"],
            "replace_text_calls": replace_text_calls,
            "apply_patch_failures": apply_patch_failures,
            "patch_retry_limit_hits": patch_retry_limit_hits,
            "patch_count": state.patch_count,
            "changed_files": changed_files,
            "targeted_tests_passed": targeted_passed,
            "regression_tests_passed": regression_passed,
            "ruff_passed": ruff_passed,
            "repository_changed": initial_hashes != source_hashes(repository),
            "canonical_fixture_unchanged": canonical_before == source_hashes(FIXTURE),
            "agent_error": agent_error or None,
        }
        write_results(
            args.output_dir,
            summary=summary,
            trajectory=trajectory,
            submission=submission,
            evaluation=evaluation,
            tool_calls=environment.tool_call_history,
            patch=patch_result.stdout,
            changed_files=changed_files,
            pytest_log=combined_output(pytest_result),
            ruff_log=combined_output(ruff_result),
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

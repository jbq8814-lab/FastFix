import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"
ISSUE = ROOT / "benchmarks" / "tasks" / "ff-001-missing-await" / "issue.md"
REQUIRED_SUMMARY_FIELDS = {
    "task_id",
    "run_id",
    "baseline",
    "model",
    "exit_status",
    "resolved",
    "api_calls",
    "instance_cost",
    "elapsed_seconds",
    "changed_files",
    "pytest_passed",
    "ruff_passed",
}
GENERATED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
GENERATED_SUFFIXES = {".pyc", ".pyo", ".pyd"}


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def docker_command(image: str, repository: Path, command: list[str]) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--mount",
        f"type=bind,source={repository.resolve()},target=/workspace",
        "-w",
        "/workspace",
        image,
        *command,
    ]


def extract_trajectory_metrics(trajectory: dict[str, Any]) -> dict[str, Any]:
    info = trajectory.get("info", {})
    model_stats = info.get("model_stats", {})
    api_calls = model_stats.get("api_calls", 0)
    return {
        "exit_status": info.get("exit_status", ""),
        "api_calls": api_calls,
        "execution_steps": api_calls,
        "instance_cost": model_stats.get("instance_cost"),
    }


def extract_token_usage(trajectory: dict[str, Any]) -> dict[str, int | None]:
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


def validate_model_name(model_name: str, custom_base_url: bool) -> None:
    if not model_name.strip():
        raise ValueError("Model name must not be empty.")
    if custom_base_url and "/" not in model_name:
        raise ValueError(f"Custom OPENAI_BASE_URL requires a provider-qualified model name; use openai/{model_name}.")


def validate_run_preflight(model_name: str, custom_base_url: bool, output_dir: Path) -> tuple[str, str]:
    validate_model_name(model_name, custom_base_url)
    if output_dir.exists():
        raise FileExistsError(f"Result directory already exists: {output_dir}")
    import litellm

    resolved_model, provider, *_ = litellm.get_llm_provider(model=model_name)
    return resolved_model, provider


def model_cost_available(model_name: str) -> bool:
    import litellm

    try:
        info = litellm.get_model_info(model_name)
    except Exception:
        return False
    return info.get("input_cost_per_token") is not None and info.get("output_cost_per_token") is not None


def normalize_instance_cost(instance_cost: float | None, cost_available: bool) -> float | None:
    return instance_cost if cost_available else None


def classify_run_eligibility(trajectory: dict[str, Any], agent_error: str) -> dict[str, Any]:
    assistant_response_count = sum(message.get("role") == "assistant" for message in trajectory.get("messages", []))
    if assistant_response_count:
        return {
            "benchmark_eligible": True,
            "benchmark_attempt": 1,
            "assistant_response_count": assistant_response_count,
            "failure_stage": None,
            "failure_category": None,
        }
    error = agent_error.lower()
    if "provider" in error:
        failure_stage = "model_provider_resolution"
    elif "authentication" in error or "unauthorized" in error:
        failure_stage = "model_authentication"
    else:
        failure_stage = "model_request_construction"
    return {
        "benchmark_eligible": False,
        "benchmark_attempt": None,
        "assistant_response_count": 0,
        "failure_stage": failure_stage,
        "failure_category": "configuration_error",
    }


def is_generated_path(path: str) -> bool:
    candidate = Path(path)
    return bool(GENERATED_PARTS.intersection(candidate.parts)) or candidate.suffix in GENERATED_SUFFIXES


def filter_changed_files(changed_files: list[str]) -> list[str]:
    return sorted({path for path in changed_files if path and not is_generated_path(path)})


def changed_files_allowed(changed_files: list[str]) -> bool:
    return bool(changed_files) and all(
        path.startswith("app/") and not path.startswith("tests/") for path in changed_files
    )


def calculate_resolved(
    patch: str,
    changed_files: list[str],
    *,
    pytest_passed: bool,
    ruff_passed: bool,
) -> bool:
    return bool(patch.strip()) and pytest_passed and ruff_passed and changed_files_allowed(changed_files)


def write_artifacts(
    output_dir: Path,
    *,
    summary: dict[str, Any],
    trajectory: dict[str, Any],
    patch: str,
    pytest_log: str,
    ruff_log: str,
    changed_files: list[str],
) -> None:
    missing = REQUIRED_SUMMARY_FIELDS - summary.keys()
    if missing:
        raise ValueError(f"Missing summary fields: {sorted(missing)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (output_dir / "trajectory.json").write_text(json.dumps(trajectory, indent=2) + "\n", encoding="utf-8")
    (output_dir / "patch.diff").write_text(patch, encoding="utf-8")
    (output_dir / "pytest.log").write_text(pytest_log, encoding="utf-8")
    (output_dir / "ruff.log").write_text(ruff_log, encoding="utf-8")
    (output_dir / "changed-files.txt").write_text(
        "".join(f"{path}\n" for path in changed_files),
        encoding="utf-8",
    )


def initialize_repository(repository: Path) -> str:
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "FastFix Baseline"],
        ["git", "config", "user.email", "fastfix@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "buggy baseline"],
    ):
        result = run(command, repository)
        if result.returncode:
            raise RuntimeError(f"{' '.join(command)} failed:\n{combined_output(result)}")
    return run(["git", "rev-parse", "HEAD"], repository).stdout.strip()


def verify_initial_failure(image: str, repository: Path) -> None:
    result = run(docker_command(image, repository, ["python", "-m", "pytest", "-q"]), ROOT)
    output = combined_output(result)
    if (
        result.returncode == 0
        or re.findall(r"(\d+) failed", output) != ["1"]
        or re.findall(r"(\d+) passed", output) != ["1"]
    ):
        raise RuntimeError(f"Initial fixture was not exactly 1 failed and 1 passed:\n{output}")


def load_interactive_agent_class() -> type:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.output import DummyOutput

    from minisweagent.agents import get_agent_class

    with create_app_session(output=DummyOutput()):
        return get_agent_class("interactive")


def run_agent(
    model_name: str,
    image: str,
    repository: Path,
    trajectory_path: Path,
    *,
    cost_available: bool,
) -> tuple[dict[str, Any], str]:
    from minisweagent.config import builtin_config_dir, get_config_from_spec
    from minisweagent.environments import get_environment
    from minisweagent.models import get_model
    from minisweagent.utils.serialize import recursive_merge

    config = recursive_merge(
        get_config_from_spec(builtin_config_dir / "mini.yaml"),
        {
            "agent": {
                "agent_class": "interactive",
                "mode": "yolo",
                "confirm_exit": False,
                "step_limit": 20,
                "cost_limit": 1.0,
                "wall_time_limit_seconds": 600,
                "output_path": trajectory_path,
            },
            "model": {
                "model_name": model_name,
                **({} if cost_available else {"cost_tracking": "ignore_errors"}),
            },
            "environment": {
                "environment_class": "docker",
                "image": image,
                "cwd": "/workspace",
                "timeout": 90,
                "run_args": [
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    f"type=bind,source={repository.resolve()},target=/workspace",
                ],
            },
        },
    )
    model = get_model(config=config["model"])
    environment = get_environment(config["environment"])
    try:
        agent_config = config["agent"].copy()
        agent_config.pop("agent_class")
        agent = load_interactive_agent_class()(model, environment, **agent_config)
        error = ""
        try:
            agent.run(ISSUE.read_text(encoding="utf-8"))
        except Exception as exception:
            error = f"{type(exception).__name__}: {exception}"
        return agent.serialize(), error
    finally:
        if environment.container_id:
            run(["docker", "rm", "-f", environment.container_id], ROOT)
            environment.container_id = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--image", default="fastfix-ff001-baseline:py312")
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    __import__("minisweagent")
    validate_run_preflight(args.model, bool(os.getenv("OPENAI_BASE_URL")), output_dir)
    cost_available = model_cost_available(args.model)
    with tempfile.TemporaryDirectory(prefix=".ff001-baseline-", dir=ROOT / "benchmarks") as temp:
        repository = Path(temp) / "repository"
        shutil.copytree(FIXTURE, repository)
        baseline_sha = initialize_repository(repository)
        verify_initial_failure(args.image, repository)

        start = time.monotonic()
        trajectory, agent_error = run_agent(
            args.model,
            args.image,
            repository,
            output_dir / "trajectory.json",
            cost_available=cost_available,
        )
        elapsed_seconds = round(time.monotonic() - start, 3)

        pytest_result = run(
            docker_command(args.image, repository, ["python", "-m", "pytest", "-q"]),
            ROOT,
        )
        ruff_result = run(
            docker_command(args.image, repository, ["python", "-m", "ruff", "check", "."]),
            ROOT,
        )
        patch_result = run(["git", "diff", "--binary", baseline_sha], repository)
        changed_result = run(["git", "diff", "--name-only", baseline_sha], repository)
        untracked_result = run(["git", "ls-files", "--others", "--exclude-standard"], repository)
        changed_files = filter_changed_files((changed_result.stdout + untracked_result.stdout).splitlines())
        metrics = extract_trajectory_metrics(trajectory)
        eligibility = classify_run_eligibility(trajectory, agent_error)
        run_id = output_dir.name
        run_sequence = int(run_id.removeprefix("run-"))
        summary = {
            "task_id": "FF-001",
            "run_id": run_id,
            "run_sequence": run_sequence,
            "baseline": "mini-swe-agent-v2.4.6",
            "model": args.model,
            **metrics,
            "instance_cost": normalize_instance_cost(metrics["instance_cost"], cost_available),
            "cost_status": "measured" if cost_available else "unavailable",
            "cost_limit_reliable": cost_available,
            **extract_token_usage(trajectory),
            **eligibility,
            "elapsed_seconds": elapsed_seconds,
            "changed_files": changed_files,
            "pytest_passed": pytest_result.returncode == 0,
            "ruff_passed": ruff_result.returncode == 0,
            "resolved": calculate_resolved(
                patch_result.stdout,
                changed_files,
                pytest_passed=pytest_result.returncode == 0,
                ruff_passed=ruff_result.returncode == 0,
            ),
            "agent_error": agent_error or None,
        }
        write_artifacts(
            output_dir,
            summary=summary,
            trajectory=trajectory,
            patch=patch_result.stdout,
            pytest_log=combined_output(pytest_result),
            ruff_log=combined_output(ruff_result),
            changed_files=changed_files,
        )
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

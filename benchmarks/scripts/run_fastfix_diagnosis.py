import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fastfix.agents.diagnosis import FastFixDiagnosisAgent
from fastfix.diagnosis.evaluation import DiagnosisEvaluation, evaluate_ff001_diagnosis
from fastfix.diagnosis.models import SubmitDiagnosisArgs, get_submit_diagnosis_tool
from fastfix.environments.tool_environment import FastFixToolEnvironment
from fastfix.models.tool_call import FastFixLitellmModel
from fastfix.tools.fastapi import build_readonly_registry
from minisweagent.config import get_config_from_spec

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-001-missing-await"
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"
CONFIG = ROOT / "src" / "fastfix" / "config" / "diagnosis.yaml"


def file_hashes(repository: Path) -> dict[str, str]:
    return {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(repository.rglob("*"))
        if path.is_file()
    }


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


def parse_diagnosis(submission: str) -> SubmitDiagnosisArgs | None:
    if not submission:
        return None
    try:
        return SubmitDiagnosisArgs.model_validate(json.loads(submission))
    except (json.JSONDecodeError, ValidationError):
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
    diagnosis: SubmitDiagnosisArgs | None,
    evaluation: DiagnosisEvaluation,
    tool_calls: list[dict[str, object]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "trajectory.json", trajectory)
    write_json(
        output_dir / "diagnosis.json",
        diagnosis.model_dump(mode="json") if diagnosis else None,
    )
    write_json(output_dir / "evaluation.json", evaluation.model_dump(mode="json"))
    write_json(output_dir / "tool-calls.json", tool_calls)


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

    with tempfile.TemporaryDirectory(prefix=".ff001-diagnosis-", dir=ROOT / "benchmarks") as temp:
        repository = Path(temp) / "repository"
        shutil.copytree(FIXTURE, repository)
        before = file_hashes(repository)
        registry = build_readonly_registry(repository)
        tool_schemas = [*registry.get_openai_tools(), get_submit_diagnosis_tool()]
        model = FastFixLitellmModel(
            model_name=args.model,
            tool_schemas=tool_schemas,
            allowed_tool_names={schema["function"]["name"] for schema in tool_schemas},
            cost_tracking="default" if cost_available else "ignore_errors",
            **config["model"],
        )
        environment = FastFixToolEnvironment(registry=registry, workspace=repository)
        agent = FastFixDiagnosisAgent(
            model,
            environment,
            output_path=args.output_dir / "trajectory.json",
            **config["agent"],
        )

        start = time.monotonic()
        agent_error = ""
        try:
            agent.run(
                task["title"],
                issue=issue,
                failing_test=task["failing_tests"][0],
            )
        except Exception as error:
            agent_error = f"{type(error).__name__}: {error}"
        elapsed_seconds = round(time.monotonic() - start, 3)

        trajectory = agent.serialize()
        diagnosis = parse_diagnosis(trajectory["info"].get("submission", ""))
        evaluation = evaluate_ff001_diagnosis(diagnosis)
        repository_unchanged = before == file_hashes(repository)
        environment_info = trajectory["info"]["fastfix_environment"]
        model_stats = trajectory["info"]["model_stats"]
        summary = {
            "task_id": task["task_id"],
            "run_id": args.output_dir.name,
            "agent": "fastfix-readonly-diagnosis",
            "model": args.model,
            "exit_status": trajectory["info"].get("exit_status", ""),
            "submitted": evaluation.submitted,
            "diagnosis_correct": evaluation.diagnosis_correct,
            "api_calls": model_stats["api_calls"],
            "instance_cost": model_stats["instance_cost"] if cost_available else None,
            "cost_status": "measured" if cost_available else "unavailable",
            **token_usage(trajectory),
            "elapsed_seconds": elapsed_seconds,
            "tool_call_count": environment_info["tool_call_count"],
            "tool_names": environment_info["tool_names"],
            "repository_unchanged": repository_unchanged,
            "agent_error": agent_error or None,
        }
        write_results(
            args.output_dir,
            summary=summary,
            trajectory=trajectory,
            diagnosis=diagnosis,
            evaluation=evaluation,
            tool_calls=environment.tool_call_history,
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastfix.agents.repair import FastFixRepairAgent
from fastfix.approval import ApprovalActionManager, ApprovalPackageManager
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.models.tool_call import FastFixLitellmModel
from fastfix.repair.models import get_reopen_repair_tool, get_submit_repair_tool
from fastfix.sandbox import DockerValidationBackend, ValidationBackend
from fastfix.tools.repair import build_repair_registry
from fastfix.workflows import SecureRepairStage, SecureRepairWorkflow
from fastfix.workspace import CandidateWorkspaceManager
from minisweagent.config import get_config_from_spec

if __package__:
    from .run_fastfix_secure import (
        PreflightResult,
        RuntimeSession,
        SecureRunnerError,
        _package_request,
        _validation_passed,
        _write_result_bundle,
        initialize_source,
        model_cost_available,
        remove_tree,
        repository_state,
        sanitize,
        source_hashes,
        token_usage,
        validate_preflight,
        write_json,
        write_runtime_session,
    )
else:
    from run_fastfix_secure import (
        PreflightResult,
        RuntimeSession,
        SecureRunnerError,
        _package_request,
        _validation_passed,
        _write_result_bundle,
        initialize_source,
        model_cost_available,
        remove_tree,
        repository_state,
        sanitize,
        source_hashes,
        token_usage,
        validate_preflight,
        write_json,
        write_runtime_session,
    )

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-002-path-parameter-mismatch"
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-002-path-parameter-mismatch"
CONFIG = ROOT / "src" / "fastfix" / "config" / "repair.yaml"
PROTOCOL_PATH = ROOT / "benchmarks" / "experiments" / "ff-002-route-ablation" / "protocol.json"
DEFAULT_RUNTIME_ROOT = ROOT / ".fastfix-runtime" / "experiments" / "ff-002-route-ablation"
DEFAULT_RESULTS_ROOT = ROOT / "benchmarks" / "results" / "ff-002-route-ablation"
ARM_ORDER = ("baseline", "route")
TASK_ID = "ff-002-path-parameter-mismatch"
ROUTE_GUIDANCE = (
    "When the repository uses FastAPI, you may use inspect_fastapi_routes to obtain a bounded static summary "
    "of routes, handlers, parameters, response models, dependencies, and direct calls.\n"
    "Treat awaited_calls and unawaited_calls as static observations, not proof that a call is correct or defective.\n"
    "Use the tool when route structure is relevant; do not call it mechanically for every task.\n"
)


@dataclass(frozen=True)
class AblationSettings:
    model_name: str
    image: str
    runtime_root: Path
    results_root: Path


@dataclass
class AblationDependencies:
    preflight: Callable[[str, str, Path], PreflightResult]
    validation_backend_factory: Callable[[Path, str], ValidationBackend]
    agent_factory: Callable[[FastFixRepairEnvironment, str, str, bool], FastFixRepairAgent]


class RetryCountingModel(FastFixLitellmModel):
    def __init__(self, **kwargs):
        self.provider_attempt_count = 0
        self.on_assistant_response: Callable[[], None] = lambda: None
        super().__init__(**kwargs)

    def _query(self, messages: list[dict[str, str]], **kwargs):
        self.provider_attempt_count += 1
        response = super()._query(messages, **kwargs)
        self.on_assistant_response()
        return response

    def serialize(self) -> dict:
        value = super().serialize()
        value["info"]["fastfix_model"]["provider_attempt_count"] = self.provider_attempt_count
        return value


def load_protocol() -> dict[str, Any]:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    if (
        protocol.get("task_id") != TASK_ID
        or protocol.get("arm_order") != list(ARM_ORDER)
        or protocol.get("result_labels", {}).get("metric_eligible") is not False
    ):
        raise SecureRunnerError("protocol_invalid", "The frozen experiment protocol is invalid.")
    return protocol


def agent_config(arm: str) -> dict[str, Any]:
    if arm not in ARM_ORDER:
        raise SecureRunnerError("arm_invalid", "Experiment arm is invalid.")
    config = get_config_from_spec(CONFIG)
    prompt = config["agent"]["system_template"]
    if prompt.count(ROUTE_GUIDANCE) != 1:
        raise SecureRunnerError("prompt_invalid", "The frozen route guidance was not found exactly once.")
    if arm == "baseline":
        config["agent"]["system_template"] = prompt.replace(ROUTE_GUIDANCE, "")
    return config


def arm_registry(
    workspace: Path,
    validation_backend: ValidationBackend,
    arm: str,
):
    if arm not in ARM_ORDER:
        raise SecureRunnerError("arm_invalid", "Experiment arm is invalid.")
    return build_repair_registry(
        workspace,
        validation_backend=validation_backend,
        include_route_inspection=arm == "route",
    )


def build_real_agent(
    environment: FastFixRepairEnvironment,
    arm: str,
    model_name: str,
    cost_available: bool,
) -> FastFixRepairAgent:
    config = agent_config(arm)
    schemas = [
        *environment.registry.get_openai_tools(),
        get_submit_repair_tool(),
        get_reopen_repair_tool(),
    ]
    model = RetryCountingModel(
        model_name=model_name,
        tool_schemas=schemas,
        allowed_tool_names={schema["function"]["name"] for schema in schemas},
        cost_tracking="default" if cost_available else "ignore_errors",
        **config["model"],
    )
    return FastFixRepairAgent(model, environment, **config["agent"])


def default_dependencies() -> AblationDependencies:
    return AblationDependencies(
        preflight=validate_preflight,
        validation_backend_factory=lambda candidate, image: DockerValidationBackend(candidate, image=image),
        agent_factory=build_real_agent,
    )


def _result_dir(settings: AblationSettings, arm: str) -> Path:
    return settings.results_root.resolve() / arm / "run-001"


def _attempted_arms(runtime_root: Path) -> set[str]:
    attempts = runtime_root.resolve() / "sessions"
    if not attempts.is_dir():
        return set()
    values = set()
    for path in attempts.glob("*/attempt.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SecureRunnerError("attempt_record_invalid", "An experiment attempt record is invalid.") from error
        if value.get("arm") not in ARM_ORDER:
            raise SecureRunnerError("attempt_record_invalid", "An experiment attempt record is invalid.")
        if value.get("assistant_response_count", 0) > 0:
            values.add(value["arm"])
    return values


def _guard_arm(settings: AblationSettings, arm: str) -> None:
    if arm not in ARM_ORDER:
        raise SecureRunnerError("arm_invalid", "Experiment arm is invalid.")
    runtime_root = settings.runtime_root.resolve()
    results_root = settings.results_root.resolve()
    fixture = FIXTURE.resolve()
    if (
        runtime_root == results_root
        or runtime_root.is_relative_to(results_root)
        or results_root.is_relative_to(runtime_root)
        or runtime_root == fixture
        or runtime_root.is_relative_to(fixture)
        or results_root == fixture
        or results_root.is_relative_to(fixture)
    ):
        raise SecureRunnerError(
            "unsafe_path", "Runtime and result roots must be isolated from each other and the fixture."
        )
    if _result_dir(settings, arm).exists() or arm in _attempted_arms(settings.runtime_root):
        raise SecureRunnerError("arm_already_attempted", "This experiment arm already has its unique attempt.")
    if arm == "route" and not (_result_dir(settings, "baseline") / "summary.json").is_file():
        raise SecureRunnerError("arm_order_invalid", "The baseline arm must finish before the route arm.")


def _materialize_protocol(results_root: Path) -> None:
    destination = results_root.resolve() / "protocol.json"
    source = PROTOCOL_PATH.read_bytes()
    if destination.exists():
        if destination.read_bytes() != source:
            raise SecureRunnerError(
                "protocol_mismatch", "The materialized protocol does not match the frozen protocol."
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(source)
    os.replace(temporary, destination)


def _retry_count(trajectory: dict[str, Any]) -> int:
    attempts = trajectory.get("info", {}).get("fastfix_model", {}).get("provider_attempt_count")
    api_calls = trajectory.get("info", {}).get("model_stats", {}).get("api_calls", 0)
    return max(int(attempts) - int(api_calls), 0) if attempts is not None else 0


def _structured_actions(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        action
        for message in trajectory.get("messages", [])
        for action in message.get("extra", {}).get("actions", [])
        if isinstance(action, dict)
    ]


def successful_read_files(
    trajectory: dict[str, Any],
    tool_calls: list[dict[str, object]],
) -> list[str]:
    paths = []
    for action, result in zip(_structured_actions(trajectory), tool_calls):
        if action.get("tool") != "read_file" or result.get("tool_name") != "read_file" or result.get("ok") is not True:
            continue
        arguments = action.get("arguments")
        path = arguments.get("path") if isinstance(arguments, dict) else None
        if isinstance(path, str) and path not in paths:
            paths.append(path)
    return paths


def provider_failure_summary(trajectory: dict[str, Any]) -> dict[str, object]:
    retry_count = _retry_count(trajectory)
    for message in reversed(trajectory.get("messages", [])):
        if message.get("role") != "exit":
            continue
        extra = message.get("extra", {})
        exception = str(extra.get("exception_str", ""))
        if not exception or not any(marker in exception for marker in ("litellm.", "OpenAIException", "OpenAIError")):
            break
        status = re.search(r"(?:Error code|status code)\D*(\d{3})", exception, flags=re.IGNORECASE)
        return {
            "provider_retry_count": retry_count,
            "last_provider_error_type": extra.get("exit_status") or None,
            "last_provider_status_code": int(status.group(1)) if status else None,
            "provider_failure_exhausted": retry_count > 0,
        }
    return {
        "provider_retry_count": retry_count,
        "last_provider_error_type": None,
        "last_provider_status_code": None,
        "provider_failure_exhausted": False,
    }


def _validate_retry_config(protocol: dict[str, Any]) -> None:
    value = os.getenv(protocol["provider_retry"]["stop_after_attempt_env_name"], "10")
    if not value.isdecimal() or int(value) != protocol["provider_retry"]["stop_after_attempt"]:
        raise SecureRunnerError("retry_config_mismatch", "Provider retry configuration differs from the protocol.")


def run_arm(
    settings: AblationSettings,
    arm: str,
    *,
    dependencies: AblationDependencies | None = None,
) -> tuple[int, dict[str, Any] | None]:
    protocol = load_protocol()
    _guard_arm(settings, arm)
    dependencies = dependencies or default_dependencies()
    output_dir = _result_dir(settings, arm)
    preflight = dependencies.preflight(settings.model_name, settings.image, output_dir)
    _validate_retry_config(protocol)
    _materialize_protocol(settings.results_root)

    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    canonical_before = source_hashes(FIXTURE)
    runtime_root = settings.runtime_root.resolve()
    session_id = str(uuid4())
    session_root = runtime_root / "sessions" / session_id
    source = session_root / "source"
    candidates = session_root / "candidates"
    packages = session_root / "approval-packages"
    actions = session_root / "approval-actions"
    for path in (candidates, packages, actions):
        path.mkdir(parents=True)
    shutil.copytree(FIXTURE, source)
    initialize_source(source)
    source_before = repository_state(source)
    package_manager = ApprovalPackageManager(packages, allowed_source_paths=tuple(task["allowed_paths"]))
    agent_holder: dict[str, Any] = {}
    cost_available = model_cost_available(settings.model_name)

    def mark_attempt() -> None:
        write_json(
            session_root / "attempt.json",
            {
                "arm": arm,
                "assistant_response_count": 1,
                "session_id": session_id,
            },
        )

    def create_agent(environment: FastFixRepairEnvironment):
        agent = dependencies.agent_factory(environment, arm, settings.model_name, cost_available)
        if isinstance(agent.model, RetryCountingModel):
            agent.model.on_assistant_response = mark_attempt
        agent.extra_template_vars |= {
            "issue": issue,
            "failing_test": task["failing_tests"][0],
            "allowed_source_paths": ", ".join(task["allowed_paths"]),
        }
        agent_holder["agent"] = agent
        agent_holder["environment"] = environment
        return agent

    workflow = SecureRepairWorkflow(
        candidate_manager=CandidateWorkspaceManager(candidates),
        validation_backend_factory=lambda candidate: dependencies.validation_backend_factory(candidate, settings.image),
        agent_factory=create_agent,
        package_manager=package_manager,
        action_manager=ApprovalActionManager(
            actions,
            package_manager=package_manager,
            allowed_source_paths=tuple(task["allowed_paths"]),
        ),
        allowed_source_paths=tuple(task["allowed_paths"]),
        include_route_inspection=arm == "route",
    )
    started = time.monotonic()
    session = workflow.start(task_id=TASK_ID, source=source, task=TASK_ID)
    elapsed_seconds = round(time.monotonic() - started, 3)
    agent = agent_holder.get("agent")
    environment = agent_holder.get("environment")
    trajectory = agent.serialize() if agent is not None else {"info": {}, "messages": []}
    assistant_response_count = sum(message.get("role") == "assistant" for message in trajectory.get("messages", []))
    if not assistant_response_count:
        remove_tree(session_root)
        return 2, None

    mark_attempt()
    state = environment.repair_state
    tool_calls = environment.tool_call_history
    package = session.approval_package if session.result.stage == SecureRepairStage.APPROVAL_PENDING else None
    changed_files = _package_request(package)["changed_files"] if package is not None else sorted(state.changed_files)
    targeted_passed = _validation_passed(state.targeted_test_result)
    regression_passed = _validation_passed(state.regression_test_result)
    ruff_passed = _validation_passed(state.ruff_result, ruff=True)
    sequence = [str(call["tool_name"]) for call in tool_calls]
    validation_results = [state.targeted_test_result, state.regression_test_result, state.ruff_result]
    image_ids = [
        result.get("image_id")
        for result in validation_results
        if isinstance(result, dict) and isinstance(result.get("image_id"), str)
    ]
    source_after = repository_state(source)
    summary = {
        "task_id": TASK_ID,
        "arm": arm,
        "run_id": "run-001",
        "session_id": session_id,
        "system_revision": protocol["secure_workflow_version"],
        **protocol["result_labels"],
        "model": settings.model_name,
        "exit_status": session.result.repair_exit_status or trajectory.get("info", {}).get("exit_status", ""),
        "submitted": session.result.submitted,
        "approval_pending": package is not None,
        "resolved_candidate": (
            package is not None
            and targeted_passed
            and regression_passed
            and ruff_passed
            and changed_files == ["app/main.py"]
        ),
        "assistant_response_count": assistant_response_count,
        "api_calls": trajectory.get("info", {}).get("model_stats", {}).get("api_calls", 0),
        "instance_cost": (
            trajectory.get("info", {}).get("model_stats", {}).get("instance_cost") if cost_available else None
        ),
        **token_usage(trajectory),
        "elapsed_seconds": elapsed_seconds,
        "tool_call_count": len(tool_calls),
        "tool_sequence": sequence,
        "read_file_calls": sequence.count("read_file"),
        "unique_files_read": successful_read_files(trajectory, tool_calls),
        "search_code_calls": sequence.count("search_code"),
        "inspect_fastapi_routes_calls": sequence.count("inspect_fastapi_routes"),
        "replace_text_calls": sequence.count("replace_text"),
        "apply_patch_calls": sequence.count("apply_patch"),
        "patch_failures": state.total_patch_failures,
        **provider_failure_summary(trajectory),
        "targeted_tests_passed": targeted_passed,
        "regression_tests_passed": regression_passed,
        "ruff_passed": ruff_passed,
        "changed_files": changed_files,
        "validation_revision": state.revision,
        "sandbox_image_id": image_ids[0] if len(set(image_ids)) == 1 and image_ids else None,
        "source_unchanged": source_before == source_after,
        "canonical_fixture_unchanged": canonical_before == source_hashes(FIXTURE),
        "approval_request_id": session.result.approval_request_id,
        "failure_stage": session.result.failure_stage,
        "failure_category": session.result.failure_category,
        "preflight_image_id": preflight.image_id,
    }
    request = _package_request(package) if package is not None else {}
    write_runtime_session(
        session_root / "session.json",
        RuntimeSession(
            session_id=session_id,
            task_id=TASK_ID,
            run_id=f"{arm}/run-001",
            status=session.result.stage.value,
            source_path=source.resolve(),
            source_head=session.result.source_head or source_before[0],
            source_branch=source_before[1],
            candidate_parent=candidates.resolve(),
            candidate_path=session.candidate.path.resolve() if package is not None else None,
            package_root=packages.resolve(),
            approval_package_path=package.resolve() if package is not None else None,
            actions_root=actions.resolve(),
            output_path=output_dir,
            approval_request_id=session.result.approval_request_id,
            patch_sha256=request.get("patch_sha256"),
        ),
    )
    replacements = {
        str(session_root.resolve()): "<runtime>",
        str(source.resolve()): "<source>",
        str(Path.home()): "<home>",
    }
    if session.candidate is not None:
        replacements[str(session.candidate.path.resolve())] = "<candidate>"
    _write_result_bundle(
        output_dir,
        summary=summary,
        trajectory=trajectory,
        tool_calls=tool_calls,
        changed_files=changed_files,
        package=package,
        failure=(
            None
            if package is not None
            else {
                "failure_stage": session.result.failure_stage,
                "failure_category": session.result.failure_category,
                "exit_status": summary["exit_status"],
            }
        ),
        replacements=replacements,
    )
    return (0 if package is not None else 1), summary


def inspect_experiment(settings: AblationSettings) -> dict[str, Any]:
    protocol = load_protocol()
    arms = {}
    for arm in ARM_ORDER:
        summary = _result_dir(settings, arm) / "summary.json"
        arms[arm] = json.loads(summary.read_text(encoding="utf-8")) if summary.is_file() else {"status": "not_run"}
    return sanitize(
        {"protocol_version": protocol["protocol_version"], "task_id": TASK_ID, "arms": arms},
        {str(Path.home()): "<home>"},
    )


def compare(settings: AblationSettings) -> dict[str, Any]:
    protocol = load_protocol()
    destination = settings.results_root.resolve() / "comparison.json"
    if destination.exists():
        raise SecureRunnerError("comparison_exists", "Comparison already exists.")
    summaries = {}
    for arm in ARM_ORDER:
        path = _result_dir(settings, arm) / "summary.json"
        if not path.is_file():
            raise SecureRunnerError("comparison_incomplete", "Both experiment arms must finish before comparison.")
        summaries[arm] = json.loads(path.read_text(encoding="utf-8"))
    metrics = {arm: {name: summaries[arm].get(name) for name in protocol["metrics"]} for arm in ARM_ORDER}
    differences = {
        name: metrics["route"][name] - metrics["baseline"][name]
        for name in protocol["metrics"]
        if type(metrics["route"][name]) in {int, float} and type(metrics["baseline"][name]) in {int, float}
    }
    value = {
        "protocol_version": protocol["protocol_version"],
        "task_id": TASK_ID,
        "arms": metrics,
        "differences_route_minus_baseline": differences,
        "interpretation": None,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    write_json(temporary, value)
    os.replace(temporary, destination)
    return value


def preflight(settings: AblationSettings) -> dict[str, Any]:
    protocol = load_protocol()
    _validate_retry_config(protocol)
    result = validate_preflight(settings.model_name, settings.image, _result_dir(settings, "baseline"))
    return {
        "provider": result.provider,
        "resolved_model": result.resolved_model,
        "docker_server_version": result.docker_server_version,
        "image_id": result.image_id,
        "arm_order": list(ARM_ORDER),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the frozen FF-002 route inspection ablation.")
    parser.add_argument("--model", default=os.getenv("MSWEA_MODEL_NAME", ""))
    parser.add_argument("--image", default=load_protocol()["docker_image_reference"])
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    run = commands.add_parser("run")
    run.add_argument("--arm", choices=ARM_ORDER, required=True)
    commands.add_parser("inspect")
    commands.add_parser("compare")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = AblationSettings(args.model, args.image, args.runtime_root, args.results_root)
    try:
        if args.command == "preflight":
            value = preflight(settings)
            code = 0
        elif args.command == "run":
            code, value = run_arm(settings, args.arm)
        elif args.command == "inspect":
            value = inspect_experiment(settings)
            code = 0
        else:
            value = compare(settings)
            code = 0
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return code
    except SecureRunnerError as error:
        print(json.dumps({"error": error.code}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastfix.agents.repair import FastFixRepairAgent
from fastfix.approval import ApprovalActionManager, ApprovalPackageManager
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.repair.models import get_reopen_repair_tool, get_submit_repair_tool
from fastfix.sandbox import DockerValidationBackend, ValidationBackend
from fastfix.security.result_publication import read_publication_state
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
        write_runtime_session,
    )
    from .run_ff002_route_ablation import (
        RetryCountingModel,
        provider_failure_summary,
        successful_read_files,
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
        write_runtime_session,
    )
    from run_ff002_route_ablation import (
        RetryCountingModel,
        provider_failure_summary,
        successful_read_files,
    )

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "src" / "fastfix" / "config" / "repair.yaml"
ATTEMPT_ID = "run-001"
LEASE_SCHEMA_VERSION = 1
RUNNER_VERSION = "task-baseline-runner-v2"
SNAPSHOT_CACHE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
SYSTEM_SNAPSHOT_PATHS = (
    "src/fastfix/__init__.py",
    "src/fastfix/agents",
    "src/fastfix/analysis",
    "src/fastfix/approval",
    "src/fastfix/config/repair.yaml",
    "src/fastfix/diagnosis",
    "src/fastfix/environments",
    "src/fastfix/models",
    "src/fastfix/repair",
    "src/fastfix/sandbox",
    "src/fastfix/security",
    "src/fastfix/tools",
    "src/fastfix/workflows",
    "src/fastfix/workspace",
    "src/minisweagent/__init__.py",
    "src/minisweagent/exceptions.py",
    "src/minisweagent/agents/__init__.py",
    "src/minisweagent/agents/default.py",
    "src/minisweagent/config/__init__.py",
    "src/minisweagent/models/__init__.py",
    "src/minisweagent/models/litellm_model.py",
    "src/minisweagent/models/utils",
    "src/minisweagent/utils/__init__.py",
    "src/minisweagent/utils/log.py",
    "src/minisweagent/utils/serialize.py",
    "benchmarks/scripts/run_task_baseline.py",
    "benchmarks/scripts/run_fastfix_secure.py",
    "benchmarks/scripts/run_ff002_route_ablation.py",
)


@dataclass(frozen=True)
class TaskBaselineContext:
    protocol_path: Path
    protocol: dict[str, Any]
    task_dir: Path
    fixture: Path
    default_runtime_root: Path
    default_results_root: Path

    @property
    def task_id(self) -> str:
        return self.protocol["task_id"]


@dataclass(frozen=True)
class BaselineSettings:
    model_name: str
    image: str
    runtime_root: Path
    results_root: Path


@dataclass
class BaselineDependencies:
    preflight: Callable[[str, str, Path], PreflightResult]
    validation_backend_factory: Callable[[Path, str], ValidationBackend]
    agent_factory: Callable[[FastFixRepairEnvironment, str, bool], FastFixRepairAgent]


def _relative_path(value: object, *, code: str) -> Path:
    if not isinstance(value, str):
        raise SecureRunnerError(code, "A required repository-relative path is invalid.")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise SecureRunnerError(code, "A required repository-relative path is invalid.")
    return path


def _validate_commands(protocol: dict[str, Any], task: dict[str, Any]) -> None:
    commands = protocol.get("validation_commands")
    if not isinstance(commands, dict) or commands != {
        "targeted": ["pytest", "-q", *task["failing_tests"]],
        "regression": ["pytest", "-q", "tests"],
        "ruff": ["ruff", "check", *task["allowed_paths"]],
    }:
        raise SecureRunnerError("validation_command_invalid", "Validation commands are not controlled.")


def load_context(protocol_path: Path) -> TaskBaselineContext:
    path = protocol_path.resolve()
    experiments = (ROOT / "benchmarks" / "experiments").resolve()
    if (
        path.name != "protocol.json"
        or path.is_symlink()
        or not path.is_file()
        or not path.is_relative_to(experiments)
        or path.parent.parent != experiments
    ):
        raise SecureRunnerError("protocol_path_invalid", "Protocol must be a repository experiment protocol.")
    try:
        protocol = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SecureRunnerError("protocol_invalid", "Protocol JSON is invalid.") from error
    if not isinstance(protocol, dict):
        raise SecureRunnerError("protocol_invalid", "Protocol must be a JSON object.")
    required = {
        "protocol_version",
        "experiment_name",
        "task_id",
        "task_commit",
        "system_commit",
        "secure_workflow_version",
        "model_env_name",
        "docker_image_reference",
        "maximum_iterations",
        "include_route_inspection",
        "include_pydantic_inspection",
        "timeouts",
        "provider_retry",
        "validation_commands",
        "expected_changed_files",
        "metrics",
        "result_labels",
    }
    if missing := sorted(required - protocol.keys()):
        raise SecureRunnerError("protocol_missing_field", f"Protocol is missing required fields: {', '.join(missing)}.")
    if (
        not isinstance(protocol["timeouts"], dict)
        or protocol["timeouts"].get("agent_wall_seconds") != 600
        or protocol["timeouts"].get("pytest_seconds") != 60
        or protocol["timeouts"].get("ruff_seconds") != 60
        or not isinstance(protocol["provider_retry"], dict)
        or not isinstance(protocol["provider_retry"].get("stop_after_attempt"), int)
        or protocol["provider_retry"]["stop_after_attempt"] < 1
        or re.fullmatch(
            r"[A-Z][A-Z0-9_]*",
            str(protocol["provider_retry"].get("stop_after_attempt_env_name", "")),
        )
        is None
        or not isinstance(protocol["result_labels"], dict)
        or not isinstance(protocol["metrics"], list)
        or not all(isinstance(metric, str) and metric for metric in protocol["metrics"])
        or not isinstance(protocol["maximum_iterations"], int)
        or protocol["maximum_iterations"] < 1
        or re.fullmatch(r"[A-Z][A-Z0-9_]*", str(protocol["model_env_name"])) is None
        or not isinstance(protocol["docker_image_reference"], str)
        or not protocol["docker_image_reference"]
        or re.fullmatch(r"[0-9a-f]{40}", str(protocol["task_commit"])) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(protocol["system_commit"])) is None
    ):
        raise SecureRunnerError("protocol_invalid", "Protocol configuration is invalid.")
    task_id = protocol["task_id"]
    if not isinstance(task_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", task_id) is None:
        raise SecureRunnerError("protocol_invalid", "Protocol task ID is invalid.")
    task_dir = (ROOT / "benchmarks" / "tasks" / task_id).resolve()
    if not task_dir.is_dir() or not task_dir.is_relative_to((ROOT / "benchmarks" / "tasks").resolve()):
        raise SecureRunnerError("task_path_invalid", "Protocol task directory is invalid.")
    task_file = task_dir / "task.json"
    issue_file = task_dir / "issue.md"
    if not task_file.is_file() or not issue_file.is_file():
        raise SecureRunnerError("task_path_invalid", "Task metadata or issue is missing.")
    try:
        task = json.loads(task_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SecureRunnerError("task_invalid", "Task metadata JSON is invalid.") from error
    if not isinstance(task, dict):
        raise SecureRunnerError("task_invalid", "Task metadata must be a JSON object.")
    fixture = (ROOT / _relative_path(task.get("fixture_repo"), code="fixture_path_invalid")).resolve()
    fixture_root = (ROOT / "benchmarks" / "fixture_repos").resolve()
    if not fixture.is_dir() or fixture.is_symlink() or not fixture.is_relative_to(fixture_root):
        raise SecureRunnerError("fixture_path_invalid", "Task fixture path is invalid.")
    for key in ("issue_file", "gold_patch"):
        referenced = (ROOT / _relative_path(task.get(key), code="task_path_invalid")).resolve()
        if (
            not referenced.is_file()
            or not referenced.is_relative_to(task_dir)
            or (key == "issue_file" and referenced != issue_file)
        ):
            raise SecureRunnerError("task_path_invalid", "Task reference path is invalid.")
    if (
        not isinstance(task.get("failing_tests"), list)
        or not task["failing_tests"]
        or not isinstance(task.get("allowed_paths"), list)
        or not task["allowed_paths"]
        or any(
            not isinstance(path, str) or re.fullmatch(r"[A-Za-z0-9_.-]+", path) is None or not (fixture / path).is_dir()
            for path in task["allowed_paths"]
        )
        or any(
            not isinstance(test, str)
            or not test.startswith("tests/")
            or ".." in Path(test.split("::", 1)[0]).parts
            or any(character in test for character in (";", "\n", "\r"))
            for test in task["failing_tests"]
        )
        or protocol["include_pydantic_inspection"] is not False
        or not isinstance(protocol["include_route_inspection"], bool)
        or protocol["result_labels"].get("metric_eligible") is not False
    ):
        raise SecureRunnerError("protocol_invalid", "Protocol or task metadata is invalid.")
    expected = protocol["expected_changed_files"]
    if (
        not isinstance(expected, list)
        or not expected
        or expected != task.get("buggy_files")
        or any(
            _relative_path(value, code="protocol_invalid").parts[0] not in task["allowed_paths"] for value in expected
        )
    ):
        raise SecureRunnerError("protocol_invalid", "Expected changed files are invalid.")
    _validate_commands(protocol, task)
    experiment = path.parent.name
    return TaskBaselineContext(
        protocol_path=path,
        protocol=protocol,
        task_dir=task_dir,
        fixture=fixture,
        default_runtime_root=ROOT / ".fastfix-runtime" / "experiments" / experiment,
        default_results_root=ROOT / "benchmarks" / "results" / experiment,
    )


def load_protocol(protocol_path: Path) -> dict[str, Any]:
    return load_context(protocol_path).protocol


def _git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        capture_output=True,
        check=False,
    )


def _commit_exists(commit: str) -> bool:
    return _git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def _commit_file(commit: str, path: str, *, code: str) -> bytes:
    result = _git("show", f"{commit}:{path}")
    if result.returncode:
        raise SecureRunnerError(code, "A required frozen file is absent from the declared commit.")
    return result.stdout


def _snapshot_files(commit: str, paths: tuple[str, ...], *, code: str) -> set[str]:
    result = _git("ls-tree", "-r", "--name-only", "-z", commit, "--", *paths)
    if result.returncode:
        raise SecureRunnerError(code, "The declared Git snapshot cannot be inspected.")
    files = {value.decode("utf-8") for value in result.stdout.split(b"\0") if value}
    if any(not any(path == root or path.startswith(f"{root}/") for path in files) for root in paths):
        raise SecureRunnerError(code, "A required frozen path is absent from the declared commit.")
    return files


def _working_files(paths: tuple[str, ...]) -> set[str]:
    files: set[str] = set()
    for value in paths:
        path = ROOT / value
        if path.is_file() or path.is_symlink():
            files.add(value)
        elif path.is_dir():
            files.update(
                item.relative_to(ROOT).as_posix()
                for item in path.rglob("*")
                if (item.is_file() or item.is_symlink()) and not SNAPSHOT_CACHE_DIRS.intersection(item.parts)
            )
    return files


def _validate_snapshot(commit: str, paths: tuple[str, ...], *, code: str) -> None:
    if _snapshot_files(commit, paths, code=code) != _working_files(paths):
        raise SecureRunnerError(code, "The working snapshot file set differs from the declared commit.")
    if _git("diff", "--no-ext-diff", "--quiet", commit, "--", *paths).returncode:
        raise SecureRunnerError(code, "The working snapshot content differs from the declared commit.")


def _task_snapshot_paths(protocol: dict[str, Any]) -> tuple[str, ...]:
    task_root = f"benchmarks/tasks/{protocol['task_id']}"
    try:
        task = json.loads(
            _commit_file(
                protocol["task_commit"],
                f"{task_root}/task.json",
                code="task_snapshot_mismatch",
            )
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecureRunnerError("task_snapshot_mismatch", "Frozen task metadata is invalid.") from error
    if not isinstance(task, dict):
        raise SecureRunnerError("task_snapshot_mismatch", "Frozen task metadata is invalid.")
    fixture = _relative_path(task.get("fixture_repo"), code="task_snapshot_mismatch").as_posix()
    if not fixture.startswith("benchmarks/fixture_repos/"):
        raise SecureRunnerError("task_snapshot_mismatch", "Frozen fixture path is invalid.")
    paths = [task_root, fixture]
    extra = protocol.get("task_snapshot_paths", [])
    if not isinstance(extra, list):
        raise SecureRunnerError("protocol_invalid", "Task snapshot paths are invalid.")
    paths.extend(_relative_path(value, code="protocol_invalid").as_posix() for value in extra)
    return tuple(dict.fromkeys(paths))


def verify_snapshots(protocol: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    if not _commit_exists(protocol["task_commit"]):
        raise SecureRunnerError("task_commit_not_found", "The declared task commit does not exist.")
    if not _commit_exists(protocol["system_commit"]):
        raise SecureRunnerError("system_commit_not_found", "The declared system commit does not exist.")
    task_paths = _task_snapshot_paths(protocol)
    _validate_snapshot(protocol["task_commit"], task_paths, code="task_snapshot_mismatch")
    _validate_snapshot(protocol["system_commit"], SYSTEM_SNAPSHOT_PATHS, code="system_snapshot_mismatch")
    return {"task": task_paths, "system": SYSTEM_SNAPSHOT_PATHS}


def agent_config(context: TaskBaselineContext) -> dict[str, Any]:
    config = get_config_from_spec(CONFIG)
    protocol = context.protocol
    if (
        config["agent"]["step_limit"] != protocol["maximum_iterations"]
        or config["agent"]["wall_time_limit_seconds"] != protocol["timeouts"]["agent_wall_seconds"]
    ):
        raise SecureRunnerError("config_mismatch", "The current repair configuration differs from the protocol.")
    return config


def baseline_registry(
    workspace: Path,
    validation_backend: ValidationBackend,
    context: TaskBaselineContext,
):
    return build_repair_registry(
        workspace,
        validation_backend=validation_backend,
        include_route_inspection=context.protocol["include_route_inspection"],
    )


def build_real_agent(
    environment: FastFixRepairEnvironment,
    context: TaskBaselineContext,
    model_name: str,
    cost_available: bool,
) -> FastFixRepairAgent:
    config = agent_config(context)
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


def default_dependencies(context: TaskBaselineContext) -> BaselineDependencies:
    return BaselineDependencies(
        preflight=validate_preflight,
        validation_backend_factory=lambda candidate, image: DockerValidationBackend(candidate, image=image),
        agent_factory=lambda environment, model, cost: build_real_agent(environment, context, model, cost),
    )


def _result_dir(settings: BaselineSettings) -> Path:
    return settings.results_root.resolve() / ATTEMPT_ID


def _protocol_hash(protocol: dict[str, Any]) -> str:
    return sha256(json.dumps(protocol, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _lease_dir(runtime_root: Path, task_id: str, attempt_id: str = ATTEMPT_ID) -> Path:
    return runtime_root.resolve() / "attempt-leases" / task_id / attempt_id


def _lease_value(protocol_path: Path, protocol: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    try:
        display_path = protocol_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        display_path = str(protocol_path.resolve())
    return {
        "schema_version": LEASE_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "task_id": protocol["task_id"],
        "attempt_id": attempt_id,
        "protocol_path": display_path,
        "protocol_sha256": _protocol_hash(protocol),
        "task_commit": protocol["task_commit"],
        "system_commit": protocol["system_commit"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "process_id": os.getpid(),
        "hostname": socket.gethostname(),
    }


def _read_lease(path: Path, protocol: dict[str, Any], attempt_id: str = ATTEMPT_ID) -> dict[str, Any]:
    try:
        value = json.loads((path / "lease.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SecureRunnerError("attempt_lease_invalid", "The attempt lease is incomplete or invalid.") from error
    expected = {
        "schema_version": LEASE_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "task_id": protocol["task_id"],
        "attempt_id": attempt_id,
        "protocol_sha256": _protocol_hash(protocol),
        "task_commit": protocol["task_commit"],
        "system_commit": protocol["system_commit"],
    }
    if (
        not isinstance(value, dict)
        or any(value.get(key) != expected_value for key, expected_value in expected.items())
        or not isinstance(value.get("protocol_path"), str)
        or not isinstance(value.get("created_at"), str)
        or not isinstance(value.get("process_id"), int)
        or not isinstance(value.get("hostname"), str)
    ):
        raise SecureRunnerError("attempt_lease_invalid", "The attempt lease is incomplete or invalid.")
    return value


def acquire_attempt_lease(
    runtime_root: Path,
    protocol_path: Path,
    protocol: dict[str, Any],
    attempt_id: str = ATTEMPT_ID,
) -> Path:
    path = _lease_dir(runtime_root, protocol["task_id"], attempt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError as error:
        try:
            _read_lease(path, protocol, attempt_id)
        except SecureRunnerError:
            raise
        raise SecureRunnerError(
            "run_already_attempted", "This task already has its unique baseline attempt."
        ) from error
    value = _lease_value(protocol_path, protocol, attempt_id)
    with (path / "lease.json").open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    return path


def _attempted(runtime_root: Path, task_id: str) -> bool:
    attempts = runtime_root.resolve() / "sessions"
    if not attempts.is_dir():
        return False
    for path in attempts.glob("*/attempt.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SecureRunnerError("attempt_record_invalid", "An FF-003 attempt record is invalid.") from error
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("task_id"), str)
            or not isinstance(value.get("assistant_response_count"), int)
            or value["assistant_response_count"] < 1
        ):
            raise SecureRunnerError("attempt_record_invalid", "A task baseline attempt record is invalid.")
        if value["task_id"] == task_id:
            return True
    return False


def _guard_paths(settings: BaselineSettings, context: TaskBaselineContext) -> None:
    runtime_root = settings.runtime_root.resolve()
    results_root = settings.results_root.resolve()
    fixture = context.fixture
    if (
        runtime_root == results_root
        or runtime_root.is_relative_to(results_root)
        or results_root.is_relative_to(runtime_root)
        or runtime_root == fixture
        or runtime_root.is_relative_to(fixture)
        or fixture.is_relative_to(runtime_root)
        or results_root == fixture
        or results_root.is_relative_to(fixture)
        or fixture.is_relative_to(results_root)
    ):
        raise SecureRunnerError(
            "unsafe_path",
            "Runtime and result roots must be isolated from each other and the fixture.",
        )


def _session_completed(runtime_root: Path, task_id: str) -> bool:
    sessions = runtime_root.resolve() / "sessions"
    if not sessions.is_dir():
        return False
    for path in sessions.glob("*/session.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("task_id") == task_id and value.get("run_id") == ATTEMPT_ID:
            return True
    return False


def attempt_status(settings: BaselineSettings, context: TaskBaselineContext) -> dict[str, Any]:
    publication = read_publication_state(_result_dir(settings))
    if publication is not None:
        return {
            "state": "result_incomplete",
            "can_run": False,
            "publication": publication,
        }
    summary = _result_dir(settings) / "summary.json"
    if summary.is_file():
        return {"state": "published", "can_run": False}
    if _result_dir(settings).exists():
        return {"state": "result_incomplete", "can_run": False}
    lease = _lease_dir(settings.runtime_root, context.task_id)
    if lease.exists():
        try:
            metadata = _read_lease(lease, context.protocol)
        except SecureRunnerError as error:
            return {"state": "lease_invalid", "can_run": False, "error": error.code}
        if _session_completed(settings.runtime_root, context.task_id):
            return {"state": "completed", "can_run": False, "lease": metadata}
        return {
            "state": "lease_acquired",
            "can_run": False,
            "disposition": "running_or_interrupted",
            "lease": metadata,
        }
    if _attempted(settings.runtime_root, context.task_id):
        return {"state": "legacy_attempt", "can_run": False}
    return {"state": "not_started", "can_run": True}


def _guard_available(settings: BaselineSettings, context: TaskBaselineContext) -> None:
    status = attempt_status(settings, context)
    if not status["can_run"]:
        code = "attempt_lease_invalid" if status["state"] == "lease_invalid" else "run_already_attempted"
        raise SecureRunnerError(code, "This task already has its unique baseline attempt.")


def _validate_retry_config(protocol: dict[str, Any]) -> None:
    value = os.getenv(protocol["provider_retry"]["stop_after_attempt_env_name"], "10")
    if not value.isdecimal() or int(value) != protocol["provider_retry"]["stop_after_attempt"]:
        raise SecureRunnerError("retry_config_mismatch", "Provider retry configuration differs from the protocol.")


def _materialize_protocol(results_root: Path, context: TaskBaselineContext) -> None:
    destination = results_root.resolve() / "protocol.json"
    source = context.protocol_path.read_bytes()
    if destination.exists():
        if destination.read_bytes() != source:
            raise SecureRunnerError("protocol_mismatch", "The materialized protocol differs from the frozen protocol.")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(source)
    os.replace(temporary, destination)


def _mark_attempt(path: Path, session_id: str, task_id: str) -> None:
    if path.exists():
        return
    value = {
        "task_id": task_id,
        "run_id": "run-001",
        "assistant_response_count": 1,
        "session_id": session_id,
    }
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def run_baseline(
    settings: BaselineSettings,
    *,
    protocol_path: Path,
    dependencies: BaselineDependencies | None = None,
) -> tuple[int, dict[str, Any] | None]:
    context = load_context(protocol_path)
    protocol = context.protocol
    dependencies = dependencies or default_dependencies(context)
    verify_snapshots(protocol)
    _guard_paths(settings, context)
    _guard_available(settings, context)
    acquire_attempt_lease(settings.runtime_root, context.protocol_path, protocol)
    output_dir = _result_dir(settings)
    preflight_result = dependencies.preflight(settings.model_name, settings.image, output_dir)
    _validate_retry_config(protocol)
    _materialize_protocol(settings.results_root, context)

    task = json.loads((context.task_dir / "task.json").read_text(encoding="utf-8"))
    issue = (context.task_dir / "issue.md").read_text(encoding="utf-8")
    canonical_before = source_hashes(context.fixture)
    runtime_root = settings.runtime_root.resolve()
    session_id = str(uuid4())
    session_root = runtime_root / "sessions" / session_id
    source = session_root / "source"
    candidates = session_root / "candidates"
    packages = session_root / "approval-packages"
    actions = session_root / "approval-actions"
    for path in (candidates, packages, actions):
        path.mkdir(parents=True)
    shutil.copytree(context.fixture, source)
    initialize_source(source)
    source_before = repository_state(source)
    package_manager = ApprovalPackageManager(packages, allowed_source_paths=tuple(task["allowed_paths"]))
    agent_holder: dict[str, Any] = {}
    cost_available = model_cost_available(settings.model_name)
    attempt_path = session_root / "attempt.json"

    def mark_attempt() -> None:
        _mark_attempt(attempt_path, session_id, context.task_id)

    def create_agent(environment: FastFixRepairEnvironment):
        agent = dependencies.agent_factory(environment, settings.model_name, cost_available)
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
        include_route_inspection=protocol["include_route_inspection"],
    )
    started = time.monotonic()
    session = workflow.start(task_id=context.task_id, source=source, task=context.task_id)
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
        "task_id": context.task_id,
        "run_id": "run-001",
        "session_id": session_id,
        "system_revision": protocol["secure_workflow_version"],
        **protocol["result_labels"],
        "model": settings.model_name,
        "status": session.result.stage.value,
        "exit_status": session.result.repair_exit_status or trajectory.get("info", {}).get("exit_status", ""),
        "submitted": session.result.submitted,
        "approval_pending": package is not None,
        "resolved_candidate": (
            package is not None
            and targeted_passed
            and regression_passed
            and ruff_passed
            and changed_files == protocol["expected_changed_files"]
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
        "canonical_fixture_unchanged": canonical_before == source_hashes(context.fixture),
        "approval_request_id": session.result.approval_request_id,
        "failure_stage": session.result.failure_stage,
        "failure_category": session.result.failure_category,
        "preflight_image_id": preflight_result.image_id,
    }
    request = _package_request(package) if package is not None else {}
    write_runtime_session(
        session_root / "session.json",
        RuntimeSession(
            session_id=session_id,
            task_id=context.task_id,
            run_id="run-001",
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
        str(runtime_root): "<runtime>",
        str(settings.results_root.resolve()): "<results>",
        str(settings.runtime_root.resolve().parent): "<temporary-root>",
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


def inspect_baseline(
    settings: BaselineSettings,
    *,
    protocol_path: Path,
) -> dict[str, Any]:
    context = load_context(protocol_path)
    summary = _result_dir(settings) / "summary.json"
    attempt = attempt_status(settings, context)
    try:
        verify_snapshots(context.protocol)
        snapshots = {"state": "valid"}
    except SecureRunnerError as error:
        snapshots = {"state": "invalid", "error": error.code}
    return sanitize(
        {
            "protocol_version": context.protocol["protocol_version"],
            "task_id": context.task_id,
            "snapshots": snapshots,
            "attempt": attempt,
            "run": (
                json.loads(summary.read_text(encoding="utf-8"))
                if attempt["state"] == "published"
                else {"status": ("publication_incomplete" if attempt["state"] == "result_incomplete" else "not_run")}
            ),
        },
        {
            str(settings.runtime_root.resolve()): "<runtime>",
            str(settings.results_root.resolve()): "<results>",
            str(Path.home()): "<home>",
        },
    )


def preflight(
    settings: BaselineSettings,
    *,
    protocol_path: Path,
    dependencies: BaselineDependencies | None = None,
) -> dict[str, Any]:
    context = load_context(protocol_path)
    protocol = context.protocol
    dependencies = dependencies or default_dependencies(context)
    verify_snapshots(protocol)
    _guard_paths(settings, context)
    _validate_retry_config(protocol)
    attempt = attempt_status(settings, context)
    if not attempt["can_run"]:
        return {"attempt": attempt, "can_run": False}
    result = dependencies.preflight(
        settings.model_name,
        settings.image,
        _result_dir(settings),
    )
    return {
        "provider": result.provider,
        "resolved_model": result.resolved_model,
        "docker_server_version": result.docker_server_version,
        "image_id": result.image_id,
        "attempt": attempt,
        "can_run": True,
    }


def build_parser(
    *,
    default_protocol: Path | None = None,
    expose_protocol: bool = True,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a frozen FastFix single-task baseline protocol.")
    if expose_protocol:
        parser.add_argument("--protocol", type=Path, default=default_protocol, required=default_protocol is None)
    else:
        parser.set_defaults(protocol=default_protocol)
    parser.add_argument("--model")
    parser.add_argument("--image")
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--results-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    commands.add_parser("run")
    commands.add_parser("inspect")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    default_protocol: Path | None = None,
    expose_protocol: bool = True,
) -> int:
    args = build_parser(default_protocol=default_protocol, expose_protocol=expose_protocol).parse_args(argv)
    try:
        context = load_context(args.protocol)
        settings = BaselineSettings(
            args.model or os.getenv(context.protocol["model_env_name"], ""),
            args.image or context.protocol["docker_image_reference"],
            args.runtime_root or context.default_runtime_root,
            args.results_root or context.default_results_root,
        )
        if args.command == "preflight":
            value = preflight(settings, protocol_path=context.protocol_path)
            code = 0
        elif args.command == "run":
            code, value = run_baseline(settings, protocol_path=context.protocol_path)
        else:
            value = inspect_baseline(settings, protocol_path=context.protocol_path)
            code = 0
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return code
    except SecureRunnerError as error:
        print(json.dumps({"error": error.code}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from fastfix.agents.repair import FastFixRepairAgent
from fastfix.approval import (
    ApprovalActionManager,
    ApprovalDecision,
    ApprovalPackageManager,
)
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.models.tool_call import FastFixLitellmModel
from fastfix.repair.evaluation import evaluate_ff001_repair
from fastfix.repair.models import SubmitRepairArgs, get_reopen_repair_tool, get_submit_repair_tool
from fastfix.sandbox import DockerValidationBackend, ValidationBackend
from fastfix.security.result_publication import (
    ResultPublicationError,
    create_result_manifest,
    publish_result_bundle,
)
from fastfix.workflows import SecureRepairStage, SecureRepairWorkflow
from fastfix.workspace import CandidateWorkspaceManager
from minisweagent.config import get_config_from_spec

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-001-missing-await"
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"
CONFIG = ROOT / "src" / "fastfix" / "config" / "repair.yaml"
DEFAULT_RUNTIME_ROOT = ROOT / ".fastfix-runtime"
DEFAULT_OUTPUT = ROOT / "benchmarks" / "results" / "fastfix-secure" / "ff-001" / "run-001"
DEFAULT_IMAGE = "fastfix-validation:ff001-v1"
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".pyd"}
SECRET_KEYS = {"api_key", "authorization", "authorization_header", "openai_api_key"}


class SecureRunnerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RuntimeSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    session_id: str
    task_id: str
    run_id: str
    status: str
    source_path: Path
    source_head: str
    source_branch: str | None
    candidate_parent: Path
    candidate_path: Path | None
    package_root: Path
    approval_package_path: Path | None
    actions_root: Path
    output_path: Path
    approval_request_id: str | None = None
    patch_sha256: str | None = None


class PreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    resolved_model: str
    docker_executable: str
    docker_server_version: str
    image_id: str


@dataclass(frozen=True)
class PrepareSettings:
    model_name: str
    image: str
    runtime_root: Path
    output_dir: Path


@dataclass
class PrepareDependencies:
    preflight: Callable[[str, str, Path], PreflightResult]
    validation_backend_factory: Callable[[Path, str], ValidationBackend]
    agent_factory: Callable[[FastFixRepairEnvironment, str, bool], FastFixRepairAgent]


def safe_environment() -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed} | {
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }


def _remove_readonly(function, path: str, _: object) -> None:
    target = Path(path)
    if not target.exists():
        return
    target.chmod(target.stat().st_mode | stat.S_IWRITE)
    function(path)


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_remove_readonly)


def run_git(
    repository: Path,
    arguments: list[str],
    *,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise SecureRunnerError("git_not_found", "Git executable was not found.")
    result = subprocess.run(
        [git, "-C", str(repository), *arguments],
        cwd=repository.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        shell=False,
        env=safe_environment(),
    )
    if result.returncode not in allowed_returncodes:
        raise SecureRunnerError("git_command_failed", "Git command failed.")
    return result


def repository_state(repository: Path) -> tuple[str, str | None, str]:
    head = run_git(repository, ["rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
    branch = run_git(
        repository,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        allowed_returncodes=(0, 1),
    )
    status = run_git(repository, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
    return head, branch.stdout.strip() or None, status


def source_hashes(repository: Path) -> dict[str, str]:
    return {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(repository.rglob("*"))
        if path.is_file()
        and not IGNORED_PARTS.intersection(path.relative_to(repository).parts)
        and path.suffix not in IGNORED_SUFFIXES
    }


def initialize_source(repository: Path) -> None:
    for arguments in (
        ["init", "-q"],
        ["config", "user.name", "FastFix Secure Runner"],
        ["config", "user.email", "fastfix@example.invalid"],
        ["config", "core.autocrlf", "false"],
        ["add", "."],
        ["commit", "-q", "-m", "buggy baseline"],
    ):
        run_git(repository, arguments)


def _run_docker(executable: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
            env=safe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SecureRunnerError("docker_unavailable", "Docker command could not be completed.") from error


def validate_preflight(model_name: str, image: str, output_dir: Path) -> PreflightResult:
    if output_dir.exists() or output_dir.is_symlink():
        raise SecureRunnerError("result_exists", "Result directory already exists.")
    if not model_name.strip():
        raise SecureRunnerError("model_missing", "Model name must not be empty.")
    if os.getenv("OPENAI_BASE_URL") and "/" not in model_name:
        raise SecureRunnerError("provider_invalid", "Custom OpenAI base URL requires a provider-qualified model.")
    import litellm

    try:
        resolved_model, provider, *_ = litellm.get_llm_provider(model=model_name)
    except Exception as error:
        raise SecureRunnerError("provider_invalid", "Unable to resolve the model provider.") from error
    if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise SecureRunnerError("api_key_missing", "OpenAI API key is not configured.")
    executable = shutil.which("docker")
    if executable is None:
        raise SecureRunnerError("docker_unavailable", "Docker executable was not found.")
    version = _run_docker(executable, ["version", "--format", "{{json .Server}}"])
    if version.returncode:
        raise SecureRunnerError("docker_unavailable", "Docker daemon is unavailable.")
    try:
        server = json.loads(version.stdout)
    except json.JSONDecodeError as error:
        raise SecureRunnerError("docker_protocol_error", "Docker returned invalid server metadata.") from error
    if not isinstance(server, dict) or server.get("Os") != "linux":
        raise SecureRunnerError("docker_configuration_error", "Docker must use Linux containers.")
    inspected = _run_docker(executable, ["image", "inspect", image])
    if inspected.returncode:
        raise SecureRunnerError("image_missing", "Configured validation image is unavailable.")
    try:
        image_id = json.loads(inspected.stdout)[0]["Id"]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
        raise SecureRunnerError("docker_protocol_error", "Docker returned invalid image metadata.") from error
    if not isinstance(image_id, str) or not image_id:
        raise SecureRunnerError("docker_protocol_error", "Docker image ID is invalid.")
    return PreflightResult(
        provider=provider,
        resolved_model=resolved_model,
        docker_executable=executable,
        docker_server_version=str(server.get("Version", "")),
        image_id=image_id,
    )


def model_cost_available(model_name: str) -> bool:
    import litellm

    try:
        info = litellm.get_model_info(model_name)
    except Exception:
        return False
    return info.get("input_cost_per_token") is not None and info.get("output_cost_per_token") is not None


def build_real_agent(
    environment: FastFixRepairEnvironment,
    model_name: str,
    cost_available: bool,
) -> FastFixRepairAgent:
    config = get_config_from_spec(CONFIG)
    schemas = [
        *environment.registry.get_openai_tools(),
        get_submit_repair_tool(),
        get_reopen_repair_tool(),
    ]
    model = FastFixLitellmModel(
        model_name=model_name,
        tool_schemas=schemas,
        allowed_tool_names={schema["function"]["name"] for schema in schemas},
        cost_tracking="default" if cost_available else "ignore_errors",
        **config["model"],
    )
    return FastFixRepairAgent(model, environment, **config["agent"])


def default_dependencies() -> PrepareDependencies:
    return PrepareDependencies(
        preflight=validate_preflight,
        validation_backend_factory=lambda candidate, image: DockerValidationBackend(candidate, image=image),
        agent_factory=build_real_agent,
    )


def token_usage(trajectory: dict[str, Any]) -> dict[str, int | None]:
    input_tokens = output_tokens = 0
    found = False
    for message in trajectory.get("messages", []):
        usage = message.get("extra", {}).get("response", {}).get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        if prompt_tokens is not None:
            input_tokens += int(prompt_tokens)
            found = True
        if completion_tokens is not None:
            output_tokens += int(completion_tokens)
            found = True
    return {
        "input_tokens": input_tokens if found else None,
        "output_tokens": output_tokens if found else None,
    }


def parse_submission(trajectory: dict[str, Any]) -> SubmitRepairArgs | None:
    value = trajectory.get("info", {}).get("submission", "")
    if not value:
        return None
    try:
        return SubmitRepairArgs.model_validate(json.loads(value)["submission"])
    except (json.JSONDecodeError, KeyError, TypeError, ValidationError):
        return None


def _secret_values() -> list[str]:
    return [
        value
        for key, value in os.environ.items()
        if any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "AUTH")) and len(value) >= 8
    ]


def sanitize(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: None if key.casefold() in SECRET_KEYS else sanitize(item, replacements) for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item, replacements) for item in value]
    if isinstance(value, str):
        sanitized = value
        for original, replacement in replacements.items():
            if original:
                sanitized = sanitized.replace(original, replacement)
        for secret in _secret_values():
            sanitized = sanitized.replace(secret, "<redacted>")
        return sanitized
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_runtime_session(path: Path, state: RuntimeSession) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def load_runtime_session(runtime_root: Path, session_id: str) -> tuple[Path, RuntimeSession]:
    try:
        UUID(session_id)
    except ValueError as error:
        raise SecureRunnerError("session_invalid", "Session ID is invalid.") from error
    root = runtime_root.resolve()
    session_root = root / "sessions" / session_id
    path = session_root / "session.json"
    if session_root.is_symlink() or path.is_symlink() or not path.is_file():
        raise SecureRunnerError("session_missing", "Runtime session was not found.")
    state = RuntimeSession.model_validate_json(path.read_text(encoding="utf-8"))
    expected = {
        state.source_path.resolve(): (session_root / "source").resolve(),
        state.candidate_parent.resolve(): (session_root / "candidates").resolve(),
        state.package_root.resolve(): (session_root / "approval-packages").resolve(),
        state.actions_root.resolve(): (session_root / "approval-actions").resolve(),
    }
    if state.session_id != session_id or any(actual != required for actual, required in expected.items()):
        raise SecureRunnerError("session_invalid", "Runtime session paths are invalid.")
    if state.candidate_path is not None and not state.candidate_path.resolve().is_relative_to(
        state.candidate_parent.resolve()
    ):
        raise SecureRunnerError("session_invalid", "Candidate path is outside the runtime session.")
    if state.approval_package_path is not None and not state.approval_package_path.resolve().is_relative_to(
        state.package_root.resolve()
    ):
        raise SecureRunnerError("session_invalid", "Approval package path is outside the runtime session.")
    return path, state


def _validation_passed(result: dict[str, object] | None, *, ruff: bool = False) -> bool:
    if result is None:
        return False
    passed = result.get("returncode") == 0 and result.get("timed_out") is False and result.get("error_code") is None
    return passed and (not ruff or result.get("passed") is True)


def _package_request(package: Path) -> dict[str, Any]:
    return json.loads((package / "approval-request.json").read_text(encoding="utf-8"))


def _write_result_bundle(
    output_dir: Path,
    *,
    summary: dict[str, Any],
    trajectory: dict[str, Any],
    tool_calls: list[dict[str, object]],
    changed_files: list[str],
    package: Path | None,
    failure: dict[str, Any] | None,
    replacements: dict[str, str],
    publisher: Callable[[Path, Path, dict[str, str]], None] = publish_result_bundle,
) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        write_json(temporary / "summary.json", sanitize(summary, replacements))
        write_json(temporary / "trajectory.json", sanitize(trajectory, replacements))
        write_json(temporary / "tool-calls.json", sanitize(tool_calls, replacements))
        (temporary / "changed-files.txt").write_text(
            "".join(f"{path}\n" for path in changed_files),
            encoding="utf-8",
            newline="\n",
        )
        if package is not None:
            for name in ("patch.diff", "approval-request.json", "validation-summary.json"):
                (temporary / name).write_bytes((package / name).read_bytes())
        else:
            write_json(
                temporary / "validation-summary.json",
                {
                    "targeted_tests_passed": summary["targeted_tests_passed"],
                    "regression_tests_passed": summary["regression_tests_passed"],
                    "ruff_passed": summary["ruff_passed"],
                    "validation_revision": summary["validation_revision"],
                    "sandbox_image_id": summary["sandbox_image_id"],
                },
            )
        if failure is not None:
            write_json(temporary / "failure.json", failure)
        expected_files = {
            "summary.json",
            "trajectory.json",
            "tool-calls.json",
            "changed-files.txt",
            "validation-summary.json",
        }
        if package is not None:
            expected_files |= {"patch.diff", "approval-request.json"}
        if failure is not None:
            expected_files.add("failure.json")
        publisher(temporary, output_dir, create_result_manifest(temporary, expected_files))
    except ResultPublicationError as error:
        if temporary.exists():
            remove_tree(temporary)
        raise SecureRunnerError(error.code, str(error)) from error
    except BaseException:
        if temporary.exists():
            remove_tree(temporary)
        raise


def prepare(
    settings: PrepareSettings,
    *,
    dependencies: PrepareDependencies | None = None,
) -> tuple[int, dict[str, Any] | None]:
    dependencies = dependencies or default_dependencies()
    preflight = dependencies.preflight(settings.model_name, settings.image, settings.output_dir)
    runtime_root = settings.runtime_root.resolve()
    output_dir = settings.output_dir.resolve()
    if output_dir == runtime_root or output_dir.is_relative_to(runtime_root):
        raise SecureRunnerError("unsafe_output", "Result directory must be outside the runtime root.")
    if output_dir == FIXTURE.resolve() or output_dir.is_relative_to(FIXTURE.resolve()):
        raise SecureRunnerError("unsafe_output", "Result directory must be outside the canonical fixture.")
    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    issue = (TASK_DIR / "issue.md").read_text(encoding="utf-8")
    canonical_before = source_hashes(FIXTURE)
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
    candidate_manager = CandidateWorkspaceManager(candidates)
    package_manager = ApprovalPackageManager(packages, allowed_source_paths=tuple(task["allowed_paths"]))
    agent_holder: dict[str, Any] = {}
    cost_available = model_cost_available(settings.model_name)

    def create_agent(environment: FastFixRepairEnvironment):
        agent = dependencies.agent_factory(environment, settings.model_name, cost_available)
        agent.extra_template_vars |= {
            "issue": issue,
            "failing_test": task["failing_tests"][0],
            "allowed_source_paths": ", ".join(task["allowed_paths"]),
        }
        agent_holder["agent"] = agent
        agent_holder["environment"] = environment
        return agent

    workflow = SecureRepairWorkflow(
        candidate_manager=candidate_manager,
        validation_backend_factory=lambda candidate: dependencies.validation_backend_factory(
            candidate,
            settings.image,
        ),
        agent_factory=create_agent,
        package_manager=package_manager,
        action_manager=ApprovalActionManager(
            actions,
            package_manager=package_manager,
            allowed_source_paths=tuple(task["allowed_paths"]),
        ),
        allowed_source_paths=tuple(task["allowed_paths"]),
    )
    started = time.monotonic()
    session = workflow.start(
        task_id=task["task_id"],
        source=source,
        task=task["task_id"],
    )
    elapsed_seconds = round(time.monotonic() - started, 3)
    agent = agent_holder.get("agent")
    environment = agent_holder.get("environment")
    trajectory = agent.serialize() if agent is not None else {"info": {}, "messages": []}
    assistant_response_count = sum(message.get("role") == "assistant" for message in trajectory.get("messages", []))
    if not assistant_response_count:
        print(
            json.dumps(
                {
                    "attempt_started": False,
                    "failure_stage": session.result.failure_stage,
                    "failure_category": session.result.failure_category,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        remove_tree(session_root)
        return 2, None

    state = environment.repair_state
    tool_calls = environment.tool_call_history
    package = session.approval_package if session.result.stage == SecureRepairStage.APPROVAL_PENDING else None
    patch = (package / "patch.diff").read_text(encoding="utf-8") if package is not None else ""
    changed_files = _package_request(package)["changed_files"] if package is not None else sorted(state.changed_files)
    targeted_passed = _validation_passed(state.targeted_test_result)
    regression_passed = _validation_passed(state.regression_test_result)
    ruff_passed = _validation_passed(state.ruff_result, ruff=True)
    submission = parse_submission(trajectory)
    evaluation = evaluate_ff001_repair(
        submission=submission,
        patch=patch,
        changed_files=changed_files,
        targeted_passed=targeted_passed,
        regression_passed=regression_passed,
        ruff_passed=ruff_passed,
    )
    validation_results = [
        state.targeted_test_result,
        state.regression_test_result,
        state.ruff_result,
    ]
    image_ids = [
        result.get("image_id")
        for result in validation_results
        if isinstance(result, dict) and isinstance(result.get("image_id"), str)
    ]
    source_after = repository_state(source)
    summary = {
        "task_id": task["task_id"],
        "run_id": settings.output_dir.name,
        "session_id": session_id,
        "system_revision": "secure-workflow-v1",
        "evaluation_role": "development_regression",
        "metric_eligible": False,
        "metric_exclusion_reason": "FF-001 was used during development of this workflow.",
        "model": settings.model_name,
        "exit_status": session.result.repair_exit_status or trajectory.get("info", {}).get("exit_status", ""),
        "submitted": session.result.submitted,
        "approval_pending": package is not None,
        "resolved_candidate": evaluation.resolved and package is not None,
        "assistant_response_count": assistant_response_count,
        "api_calls": trajectory.get("info", {}).get("model_stats", {}).get("api_calls", 0),
        "instance_cost": (
            trajectory.get("info", {}).get("model_stats", {}).get("instance_cost") if cost_available else None
        ),
        "cost_status": "measured" if cost_available else "unavailable",
        **token_usage(trajectory),
        "elapsed_seconds": elapsed_seconds,
        "tool_call_count": len(tool_calls),
        "changed_files": changed_files,
        "targeted_tests_passed": targeted_passed,
        "regression_tests_passed": regression_passed,
        "ruff_passed": ruff_passed,
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
    runtime_state = RuntimeSession(
        session_id=session_id,
        task_id=task["task_id"],
        run_id=settings.output_dir.name,
        status=session.result.stage.value,
        source_path=source.resolve(),
        source_head=session.result.source_head or source_before[0],
        source_branch=source_before[1],
        candidate_parent=candidates.resolve(),
        candidate_path=(
            session.candidate.path.resolve() if package is not None and session.candidate is not None else None
        ),
        package_root=packages.resolve(),
        approval_package_path=package.resolve() if package is not None else None,
        actions_root=actions.resolve(),
        output_path=settings.output_dir.resolve(),
        approval_request_id=session.result.approval_request_id,
        patch_sha256=request.get("patch_sha256"),
    )
    write_runtime_session(session_root / "session.json", runtime_state)
    replacements = {
        str(session_root.resolve()): "<runtime>",
        str(source.resolve()): "<source>",
        str(Path.home()): "<home>",
    }
    if session.candidate is not None:
        replacements[str(session.candidate.path.resolve())] = "<candidate>"
    failure = (
        None
        if package is not None
        else {
            "failure_stage": session.result.failure_stage,
            "failure_category": session.result.failure_category,
            "exit_status": summary["exit_status"],
        }
    )
    _write_result_bundle(
        settings.output_dir.resolve(),
        summary=summary,
        trajectory=trajectory,
        tool_calls=tool_calls,
        changed_files=changed_files,
        package=package,
        failure=failure,
        replacements=replacements,
    )
    return 0 if package is not None else 1, summary


def _runtime_managers(state: RuntimeSession):
    package_manager = ApprovalPackageManager(state.package_root)
    return package_manager, ApprovalActionManager(
        state.actions_root,
        package_manager=package_manager,
    )


def inspect_session(runtime_root: Path, session_id: str) -> dict[str, Any]:
    _, state = load_runtime_session(runtime_root, session_id)
    package_manager, _ = _runtime_managers(state)
    request = (
        package_manager.verify_package(state.approval_package_path) if state.approval_package_path is not None else None
    )
    return {
        "session_id": state.session_id,
        "task_id": state.task_id,
        "run_id": state.run_id,
        "status": state.status,
        "approval_request_id": state.approval_request_id,
        "patch_sha256": request.patch_sha256 if request is not None else state.patch_sha256,
    }


def decide_session(
    runtime_root: Path,
    session_id: str,
    *,
    decision: str,
    actor: str,
    expected_patch_sha256: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    path, state = load_runtime_session(runtime_root, session_id)
    if state.status != SecureRepairStage.APPROVAL_PENDING.value:
        raise SecureRunnerError("session_state_invalid", "Session is not pending approval.")
    if state.candidate_path is None or state.approval_package_path is None:
        raise SecureRunnerError("session_invalid", "Pending session is incomplete.")
    candidate_manager = CandidateWorkspaceManager(state.candidate_parent)
    candidate = candidate_manager.recover(
        source=state.source_path,
        candidate=state.candidate_path,
        source_head=state.source_head,
        source_branch=state.source_branch,
    )
    _, action_manager = _runtime_managers(state)
    result = action_manager.decide(
        package=state.approval_package_path,
        source=state.source_path,
        candidate=candidate,
        decision=ApprovalDecision(
            decision=decision,
            request_id=state.approval_request_id,
            expected_patch_sha256=expected_patch_sha256 if decision == "approve" else None,
            actor=actor,
            note=note,
        ),
    )
    state.status = SecureRepairStage.APPLIED.value if result.status == "approved" else SecureRepairStage.REJECTED.value
    state.candidate_path = None if result.cleanup_warning is None else state.candidate_path
    write_runtime_session(path, state)
    return inspect_session(runtime_root, session_id)


def rollback_session(
    runtime_root: Path,
    session_id: str,
    *,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    path, state = load_runtime_session(runtime_root, session_id)
    if state.status != SecureRepairStage.APPLIED.value or state.approval_package_path is None:
        raise SecureRunnerError("session_state_invalid", "Session does not contain an applied repair.")
    _, action_manager = _runtime_managers(state)
    action_manager.rollback(
        package=state.approval_package_path,
        source=state.source_path,
        request_id=state.approval_request_id,
        actor=actor,
        note=note,
    )
    state.status = SecureRepairStage.ROLLED_BACK.value
    write_runtime_session(path, state)
    return inspect_session(runtime_root, session_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run and resume the FastFix secure repair workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--model", default=os.getenv("MSWEA_MODEL_NAME", ""))
    prepare_parser.add_argument("--image", default=DEFAULT_IMAGE)
    prepare_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    prepare_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    for command in ("inspect", "approve", "reject", "rollback"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
        command_parser.add_argument("--session-id", required=True)
        if command != "inspect":
            command_parser.add_argument("--actor", required=True)
            command_parser.add_argument("--note", default="")
        if command == "approve":
            command_parser.add_argument("--expected-patch-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            code, summary = prepare(
                PrepareSettings(
                    model_name=args.model,
                    image=args.image,
                    runtime_root=args.runtime_root,
                    output_dir=args.output_dir,
                )
            )
            print(json.dumps(summary or {"attempt_started": False}, ensure_ascii=False, indent=2, sort_keys=True))
            return code
        if args.command == "inspect":
            result = inspect_session(args.runtime_root, args.session_id)
        elif args.command in {"approve", "reject"}:
            result = decide_session(
                args.runtime_root,
                args.session_id,
                decision=args.command,
                actor=args.actor,
                expected_patch_sha256=getattr(args, "expected_patch_sha256", None),
                note=args.note,
            )
        else:
            result = rollback_session(
                args.runtime_root,
                args.session_id,
                actor=args.actor,
                note=args.note,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except SecureRunnerError as error:
        print(json.dumps({"error": error.code}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from fastfix.agents.repair import FastFixRepairAgent
from fastfix.approval import (
    ApprovalActionManager,
    ApprovalDecision,
    ApprovalPackageManager,
)
from fastfix.repair.state import RepairSessionState
from fastfix.sandbox import DockerValidationBackend, ValidationExecution
from fastfix.workflows import SecureRepairStage, SecureRepairWorkflow, SecureRepairWorkflowError
from fastfix.workspace import CandidateWorkspaceManager
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"
IMAGE = "fastfix-validation:ff001-v1"


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
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


def source_repository(parent: Path, name: str = "source repository") -> Path:
    repository = parent / name
    shutil.copytree(FIXTURE, repository)
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "FastFix Tests"),
        ("config", "user.email", "fastfix@example.invalid"),
        ("config", "core.autocrlf", "false"),
        ("add", "."),
        ("commit", "-q", "-m", "baseline"),
    ):
        assert git(repository, *arguments).returncode == 0
    return repository


def tool_action(tool: str, arguments: dict, number: int) -> dict:
    return {"tool": tool, "arguments": arguments, "tool_call_id": str(number)}


def repair_actions() -> list[dict]:
    submission = {
        "summary": "Resolve the asynchronous service result.",
        "root_cause": "The route returned the service coroutine.",
        "changed_files": ["app/main.py"],
        "tests_run": ["targeted", "regression", "ruff"],
        "risk_notes": [],
        "confidence": 1.0,
    }
    return [
        tool_action("read_file", {"path": "app/main.py"}, 1),
        tool_action(
            "replace_text",
            {
                "path": "app/main.py",
                "old_text": "    return fetch_user(user_id)",
                "new_text": "    return await fetch_user(user_id)",
            },
            2,
        ),
        tool_action(
            "run_pytest",
            {
                "scope": "targeted",
                "targets": ["tests/test_users.py::test_get_user_returns_user"],
            },
            3,
        ),
        tool_action("run_pytest", {"scope": "regression"}, 4),
        tool_action("run_ruff", {}, 5),
        tool_action("show_git_diff", {}, 6),
        tool_action("submit_repair", submission, 7),
    ]


def agent_factory(actions: list[dict] | None = None):
    scripted = actions or repair_actions()

    def create(environment):
        model = DeterministicToolcallModel(
            outputs=[make_toolcall_output(None, [{"id": item["tool_call_id"]}], [item]) for item in scripted],
            cost_per_call=0,
        )
        return FastFixRepairAgent(
            model,
            environment,
            system_template="Use structured repair tools.",
            instance_template="{{ task }}",
            step_limit=len(scripted),
            cost_limit=0,
        )

    return create


class RecordingValidationBackend:
    def __init__(self, calls: list[tuple[str, list[str]]], *, passed: bool = True):
        self.calls = calls
        self.passed = passed

    def run(
        self,
        *,
        tool: Literal["pytest", "ruff"],
        arguments: list[str],
        timeout_seconds: int,
    ) -> ValidationExecution:
        self.calls.append((tool, arguments))
        return ValidationExecution(
            returncode=0 if self.passed else 1,
            output="1 passed\n" if self.passed else "1 failed\n",
            duration_seconds=0.01,
            error_code=None if self.passed else "validation_failed",
            metadata={"image_id": "sha256:scripted", "network_mode": "none"},
        )


class StaleValidationPackageManager(ApprovalPackageManager):
    def create(
        self,
        *,
        task_id: str,
        source: Path,
        candidate: Path,
        source_head: str,
        repair_state: RepairSessionState,
    ) -> Path:
        repair_state.revision += 1
        return super().create(
            task_id=task_id,
            source=source,
            candidate=candidate,
            source_head=source_head,
            repair_state=repair_state,
        )


class VerificationFailureActionManager(ApprovalActionManager):
    def _live_patch(self, source: Path) -> str:
        return ""


def workflow(
    root: Path,
    *,
    passed: bool = True,
    actions: list[dict] | None = None,
    package_class: type[ApprovalPackageManager] = ApprovalPackageManager,
    action_class: type[ApprovalActionManager] = ApprovalActionManager,
) -> tuple[SecureRepairWorkflow, list[tuple[str, list[str]]]]:
    candidates = root / "candidate workspaces"
    packages = root / "approval packages"
    approvals = root / "approval actions"
    for path in (candidates, packages, approvals):
        path.mkdir(parents=True)
    calls: list[tuple[str, list[str]]] = []
    package_manager = package_class(packages)
    return (
        SecureRepairWorkflow(
            candidate_manager=CandidateWorkspaceManager(candidates),
            validation_backend_factory=lambda _: RecordingValidationBackend(calls, passed=passed),
            agent_factory=agent_factory(actions),
            package_manager=package_manager,
            action_manager=action_class(approvals, package_manager=package_manager),
        ),
        calls,
    )


def approve_decision(session) -> ApprovalDecision:
    request = json.loads((session.approval_package / "approval-request.json").read_text(encoding="utf-8"))
    return ApprovalDecision(
        decision="approve",
        request_id=request["request_id"],
        expected_patch_sha256=request["patch_sha256"],
        actor="reviewer",
    )


def reject_decision(session) -> ApprovalDecision:
    return ApprovalDecision(
        decision="reject",
        request_id=session.result.approval_request_id,
        actor="reviewer",
    )


def test_approve_and_rollback_complete_isolated_workflow(tmp_path: Path) -> None:
    canonical_before = hashes(FIXTURE)
    source = source_repository(tmp_path)
    source_head = git(source, "rev-parse", "HEAD").stdout.strip()
    runner, calls = workflow(tmp_path / "workflow root")

    session = runner.start(task_id="FF-001", source=source, task="Repair FF-001")

    assert session.result.stage == SecureRepairStage.APPROVAL_PENDING, session.result
    assert session.result.approval_status == "pending"
    assert session.result.application_status == "not_applied"
    assert session.result.validation_epoch == 1
    assert session.candidate is not None and session.candidate.path.is_dir()
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    assert git(source, "rev-parse", "HEAD").stdout.strip() == source_head
    assert "return await fetch_user(user_id)" not in (source / "app" / "main.py").read_text(encoding="utf-8")
    assert "return await fetch_user(user_id)" in (session.candidate.path / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    assert (session.approval_package / "patch.diff").read_text(encoding="utf-8") == git(
        session.candidate.path,
        "diff",
        "--no-ext-diff",
        "--no-color",
        "HEAD",
        "--",
    ).stdout.replace("\r\n", "\n")
    assert calls == [
        ("pytest", ["-q", "tests/test_users.py::test_get_user_returns_user"]),
        ("pytest", ["-q", "tests"]),
        ("ruff", ["check", "app"]),
    ]

    assert runner.decide(session, approve_decision(session)).stage == SecureRepairStage.APPLIED, session.result
    assert "return await fetch_user(user_id)" in (source / "app" / "main.py").read_text(encoding="utf-8")
    assert git(source, "diff", "--cached", "--quiet").returncode == 0
    assert git(source, "rev-parse", "HEAD").stdout.strip() == source_head
    assert not session.candidate.path.exists()

    result = runner.rollback(session, actor="reviewer")
    assert result.stage == SecureRepairStage.COMPLETED
    assert result.rollback_status == "rolled_back"
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    assert git(source, "rev-parse", "HEAD").stdout.strip() == source_head
    assert hashes(FIXTURE) == canonical_before


def test_reject_never_changes_source_and_sessions_are_independent(tmp_path: Path) -> None:
    source = source_repository(tmp_path)
    runner, _ = workflow(tmp_path / "workflow root")

    first = runner.start(task_id="FF-001-first", source=source, task="Repair FF-001")
    second = runner.start(task_id="FF-001-second", source=source, task="Repair FF-001")

    assert first.result.session_id != second.result.session_id
    assert first.result.approval_request_id != second.result.approval_request_id
    assert first.candidate.path != second.candidate.path
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    assert runner.decide(first, reject_decision(first)).stage == SecureRepairStage.COMPLETED
    assert runner.decide(second, reject_decision(second)).approval_status == "rejected"
    assert not first.candidate.path.exists() and not second.candidate.path.exists()
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_repair_and_docker_failures_do_not_create_packages(tmp_path: Path) -> None:
    source = source_repository(tmp_path)
    read_only = [tool_action("read_file", {"path": "app/main.py"}, 1)]
    repair_runner, _ = workflow(tmp_path / "repair failure", actions=read_only)
    docker_runner, calls = workflow(tmp_path / "docker failure", passed=False)

    repair = repair_runner.start(task_id="FF-001-repair-failure", source=source, task="Fail")
    docker = docker_runner.start(task_id="FF-001-docker-failure", source=source, task="Fail")

    assert repair.result.stage == SecureRepairStage.FAILED
    assert repair.result.failure_stage == "repair"
    assert repair.approval_package is None and not repair.candidate.path.exists()
    assert docker.result.stage == SecureRepairStage.FAILED
    assert docker.result.failure_stage == "validation"
    assert docker.result.failure_category == "validation_failed"
    assert docker.approval_package is None and not docker.candidate.path.exists()
    assert calls and all(tool in {"pytest", "ruff"} for tool, _ in calls)
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_stale_validation_and_failed_approve_restore_source_and_cleanup(tmp_path: Path) -> None:
    source = source_repository(tmp_path)
    stale_runner, _ = workflow(
        tmp_path / "stale validation",
        package_class=StaleValidationPackageManager,
    )
    failed_runner, _ = workflow(
        tmp_path / "failed approval",
        action_class=VerificationFailureActionManager,
    )

    stale = stale_runner.start(task_id="FF-001-stale", source=source, task="Repair")
    failed = failed_runner.start(task_id="FF-001-approval-failure", source=source, task="Repair")

    assert stale.result.stage == SecureRepairStage.FAILED
    assert stale.result.failure_category == "validation_incomplete"
    assert stale.approval_package is None and not stale.candidate.path.exists()
    result = failed_runner.decide(failed, approve_decision(failed))
    assert result.stage == SecureRepairStage.FAILED
    assert result.failure_stage == "approval_action"
    assert result.application_status == "not_applied"
    assert not failed.candidate.path.exists()
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_explicit_decision_and_transition_guards_and_safe_result(tmp_path: Path) -> None:
    source = source_repository(tmp_path)
    runner, _ = workflow(tmp_path / "workflow root")
    session = runner.start(task_id="FF-001", source=source, task="Repair")

    payload = session.result.model_dump_json()
    assert session.result.stage == SecureRepairStage.APPROVAL_PENDING
    assert str(session.candidate.path) not in payload
    assert str(source) not in payload
    assert "API_KEY" not in payload
    with pytest.raises(SecureRepairWorkflowError, match="Only an applied session"):
        runner.rollback(session, actor="reviewer")
    runner.decide(session, reject_decision(session))
    with pytest.raises(SecureRepairWorkflowError, match="not awaiting"):
        runner.decide(session, reject_decision(session))


@pytest.mark.docker
def test_real_docker_approve_and_rollback_workflow(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    if "docker" not in request.config.option.markexpr:
        pytest.skip("Run explicitly with -m docker.")
    canonical_before = hashes(FIXTURE)
    source = source_repository(tmp_path)
    root = tmp_path / "docker workflow"
    candidates = root / "candidates"
    packages = root / "packages"
    actions = root / "actions"
    for path in (candidates, packages, actions):
        path.mkdir(parents=True)
    package_manager = ApprovalPackageManager(packages)
    runner = SecureRepairWorkflow(
        candidate_manager=CandidateWorkspaceManager(candidates),
        validation_backend_factory=lambda candidate: DockerValidationBackend(candidate, image=IMAGE),
        agent_factory=agent_factory(),
        package_manager=package_manager,
        action_manager=ApprovalActionManager(actions, package_manager=package_manager),
    )

    session = runner.start(task_id="FF-001-docker", source=source, task="Repair FF-001")

    assert session.result.stage == SecureRepairStage.APPROVAL_PENDING, session.result
    validation = json.loads((session.approval_package / "validation-summary.json").read_text(encoding="utf-8"))
    assert validation["sandbox_image_id"].startswith("sha256:")
    assert validation["sandbox_network_mode"] == "none"
    assert runner.decide(session, approve_decision(session)).stage == SecureRepairStage.APPLIED
    assert runner.rollback(session, actor="reviewer").stage == SecureRepairStage.COMPLETED
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    assert hashes(FIXTURE) == canonical_before

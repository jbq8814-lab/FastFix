from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from fastfix.approval import (
    ApprovalActionError,
    ApprovalActionManager,
    ApprovalDecision,
    ApprovalPackageError,
    ApprovalPackageManager,
)
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.repair.state import RepairPhase
from fastfix.sandbox import ValidationBackend
from fastfix.tools.repair import build_repair_registry
from fastfix.workspace import CandidateWorkspace, CandidateWorkspaceError, CandidateWorkspaceManager


class SecureRepairWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SecureRepairStage(str, Enum):
    CREATED = "created"
    CANDIDATE_READY = "candidate_ready"
    REPAIR_FINISHED = "repair_finished"
    APPROVAL_PENDING = "approval_pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"
    FAILED = "failed"


class RepairAgent(Protocol):
    def run(self, task: str) -> dict: ...


class SecureRepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    task_id: str
    source_head: str | None = None
    stage: SecureRepairStage = SecureRepairStage.CREATED
    state_history: list[SecureRepairStage] = Field(default_factory=lambda: [SecureRepairStage.CREATED])
    repair_exit_status: str | None = None
    submitted: bool = False
    validation_revision: int | None = None
    validation_epoch: int | None = None
    approval_request_id: str | None = None
    approval_status: str = "not_created"
    application_status: str = "not_applied"
    rollback_status: str = "not_requested"
    cleanup_status: str = "not_created"
    failure_stage: str | None = None
    failure_category: str | None = None


@dataclass
class SecureRepairSession:
    result: SecureRepairResult
    source: Path
    candidate: CandidateWorkspace | None = None
    approval_package: Path | None = None


_TRANSITIONS = {
    SecureRepairStage.CREATED: {SecureRepairStage.CANDIDATE_READY, SecureRepairStage.FAILED},
    SecureRepairStage.CANDIDATE_READY: {SecureRepairStage.REPAIR_FINISHED, SecureRepairStage.FAILED},
    SecureRepairStage.REPAIR_FINISHED: {SecureRepairStage.APPROVAL_PENDING, SecureRepairStage.FAILED},
    SecureRepairStage.APPROVAL_PENDING: {
        SecureRepairStage.APPROVED,
        SecureRepairStage.REJECTED,
        SecureRepairStage.FAILED,
    },
    SecureRepairStage.APPROVED: {SecureRepairStage.APPLIED},
    SecureRepairStage.REJECTED: {SecureRepairStage.COMPLETED},
    SecureRepairStage.APPLIED: {
        SecureRepairStage.ROLLED_BACK,
        SecureRepairStage.COMPLETED,
        SecureRepairStage.FAILED,
    },
    SecureRepairStage.ROLLED_BACK: {SecureRepairStage.COMPLETED},
    SecureRepairStage.COMPLETED: set(),
    SecureRepairStage.FAILED: set(),
}


class SecureRepairWorkflow:
    def __init__(
        self,
        *,
        candidate_manager: CandidateWorkspaceManager,
        validation_backend_factory: Callable[[Path], ValidationBackend],
        agent_factory: Callable[[FastFixRepairEnvironment], RepairAgent],
        package_manager: ApprovalPackageManager,
        action_manager: ApprovalActionManager,
        allowed_source_paths: tuple[str, ...] = ("app",),
        test_paths: tuple[str, ...] = ("tests",),
        include_route_inspection: bool = True,
    ):
        self.candidate_manager = candidate_manager
        self.validation_backend_factory = validation_backend_factory
        self.agent_factory = agent_factory
        self.package_manager = package_manager
        self.action_manager = action_manager
        self.allowed_source_paths = allowed_source_paths
        self.test_paths = test_paths
        self.include_route_inspection = include_route_inspection

    @staticmethod
    def _transition(session: SecureRepairSession, stage: SecureRepairStage) -> None:
        current = session.result.stage
        if stage not in _TRANSITIONS[current]:
            raise SecureRepairWorkflowError(
                "invalid_transition",
                f"Cannot move secure repair session from {current.value} to {stage.value}.",
            )
        session.result.stage = stage
        session.result.state_history.append(stage)

    def _cleanup(self, session: SecureRepairSession) -> None:
        if session.candidate is None:
            session.result.cleanup_status = "not_created"
            return
        try:
            session.candidate.cleanup()
        except CandidateWorkspaceError:
            session.result.cleanup_status = "failed"
            return
        session.result.cleanup_status = "cleaned"

    def _fail(self, session: SecureRepairSession, stage: str, category: str) -> SecureRepairSession:
        session.result.failure_stage = stage
        session.result.failure_category = category
        self._cleanup(session)
        self._transition(session, SecureRepairStage.FAILED)
        return session

    @staticmethod
    def _error_code(error: BaseException, fallback: str) -> str:
        code = getattr(error, "code", None)
        return code if isinstance(code, str) else fallback

    def start(self, *, task_id: str, source: Path, task: str) -> SecureRepairSession:
        session = SecureRepairSession(
            result=SecureRepairResult(session_id=str(uuid4()), task_id=task_id),
            source=source.resolve(),
        )
        try:
            session.candidate = self.candidate_manager.create(source)
        except CandidateWorkspaceError as error:
            return self._fail(session, "candidate_creation", error.code)
        session.result.source_head = session.candidate.source_head
        session.result.cleanup_status = "pending"
        self._transition(session, SecureRepairStage.CANDIDATE_READY)

        try:
            environment = FastFixRepairEnvironment(
                registry=build_repair_registry(
                    session.candidate.path,
                    validation_backend=self.validation_backend_factory(session.candidate.path),
                    allowed_source_paths=self.allowed_source_paths,
                    test_paths=self.test_paths,
                    include_route_inspection=self.include_route_inspection,
                ),
                workspace=session.candidate.path,
                regression_targets=self.test_paths,
                ruff_paths=self.allowed_source_paths,
            )
            repair = self.agent_factory(environment).run(task)
        except BaseException as error:
            return self._fail(session, "repair", self._error_code(error, "agent_exception"))

        session.result.repair_exit_status = str(repair.get("exit_status", "Unknown"))
        session.result.submitted = (
            session.result.repair_exit_status == "Submitted" and environment.repair_state.phase == RepairPhase.SUBMITTED
        )
        session.result.validation_revision = environment.repair_state.revision
        session.result.validation_epoch = environment.repair_state.validation_epoch
        self._transition(session, SecureRepairStage.REPAIR_FINISHED)
        if not session.result.submitted:
            validation_errors = [
                str(call["error_code"])
                for call in environment.tool_call_history
                if call["tool_name"] in {"run_pytest", "run_ruff"} and call["error_code"]
            ]
            return self._fail(
                session,
                "validation" if validation_errors else "repair",
                validation_errors[-1] if validation_errors else session.result.repair_exit_status.lower(),
            )

        try:
            session.approval_package = self.package_manager.create(
                task_id=task_id,
                source=session.source,
                candidate=session.candidate.path,
                source_head=session.candidate.source_head,
                repair_state=environment.repair_state,
            )
            request = self.package_manager.verify_package(session.approval_package)
        except ApprovalPackageError as error:
            return self._fail(session, "approval_package", error.code)
        except BaseException:
            return self._fail(session, "approval_package", "package_creation_failed")

        session.result.approval_request_id = request.request_id
        session.result.approval_status = "pending"
        self._transition(session, SecureRepairStage.APPROVAL_PENDING)
        return session

    def decide(self, session: SecureRepairSession, decision: ApprovalDecision) -> SecureRepairResult:
        if (
            session.result.stage != SecureRepairStage.APPROVAL_PENDING
            or session.candidate is None
            or session.approval_package is None
        ):
            raise SecureRepairWorkflowError("invalid_transition", "Session is not awaiting an approval decision.")
        try:
            action = self.action_manager.decide(
                package=session.approval_package,
                source=session.source,
                candidate=session.candidate,
                decision=decision,
            )
        except ApprovalActionError as error:
            return self._fail(session, "approval_action", error.code).result

        session.result.cleanup_status = "failed" if action.cleanup_warning else "cleaned"
        if action.status == "rejected":
            session.result.approval_status = "rejected"
            self._transition(session, SecureRepairStage.REJECTED)
            self._transition(session, SecureRepairStage.COMPLETED)
            return session.result
        session.result.approval_status = "approved"
        session.result.application_status = "applied"
        self._transition(session, SecureRepairStage.APPROVED)
        self._transition(session, SecureRepairStage.APPLIED)
        return session.result

    def complete(self, session: SecureRepairSession) -> SecureRepairResult:
        if session.result.stage != SecureRepairStage.APPLIED:
            raise SecureRepairWorkflowError("invalid_transition", "Only an applied session can be completed.")
        self._transition(session, SecureRepairStage.COMPLETED)
        return session.result

    def rollback(
        self,
        session: SecureRepairSession,
        *,
        actor: str,
        note: str = "",
    ) -> SecureRepairResult:
        if (
            session.result.stage != SecureRepairStage.APPLIED
            or session.result.approval_request_id is None
            or session.approval_package is None
        ):
            raise SecureRepairWorkflowError("invalid_transition", "Only an applied session can be rolled back.")
        try:
            self.action_manager.rollback(
                package=session.approval_package,
                source=session.source,
                request_id=session.result.approval_request_id,
                actor=actor,
                note=note,
            )
        except ApprovalActionError as error:
            session.result.rollback_status = "failed"
            session.result.failure_stage = "rollback"
            session.result.failure_category = error.code
            self._transition(session, SecureRepairStage.FAILED)
            return session.result
        session.result.rollback_status = "rolled_back"
        self._transition(session, SecureRepairStage.ROLLED_BACK)
        self._transition(session, SecureRepairStage.COMPLETED)
        return session.result

    def cleanup(self, session: SecureRepairSession) -> None:
        self._cleanup(session)

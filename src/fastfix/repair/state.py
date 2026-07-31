from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RepairPhase(str, Enum):
    DIAGNOSING = "diagnosing"
    PATCHED = "patched"
    TARGET_VALIDATED = "target_validated"
    REGRESSION_VALIDATED = "regression_validated"
    READY_TO_SUBMIT = "ready_to_submit"
    SUBMITTED = "submitted"


READY_TO_SUBMIT_ACTIONS = (
    "show_git_diff",
    "submit_repair",
    "rollback_changes",
    "reopen_repair",
)


class RepairSessionState(BaseModel):
    phase: RepairPhase = RepairPhase.DIAGNOSING
    revision: int = 0
    patch_count: int = 0
    changed_files: list[str] = Field(default_factory=list)
    targeted_test_revision: int | None = None
    regression_test_revision: int | None = None
    ruff_revision: int | None = None
    targeted_test_result: dict[str, Any] | None = None
    regression_test_result: dict[str, Any] | None = None
    ruff_result: dict[str, Any] | None = None
    validation_epoch: int = 0
    consecutive_patch_failures: int = 0
    total_patch_failures: int = 0
    patch_temporarily_blocked: bool = False
    last_edit_error: str | None = None
    reopen_count: int = 0
    last_reopen_reason: str | None = None
    reopen_history: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def patch_permanently_disabled(self) -> bool:
        return self.total_patch_failures >= 6

    @property
    def regression_scope_complete(self) -> bool:
        return self.regression_test_result is not None and self.regression_test_result.get("scope_complete") is True

    @property
    def ruff_scope_complete(self) -> bool:
        return self.ruff_result is not None and self.ruff_result.get("scope_complete") is True

    def _validation_status(self, revision: int | None, result: dict[str, Any] | None) -> str:
        if revision is None or result is None:
            return "missing"
        return (
            "current"
            if revision == self.revision and result.get("validation_epoch") == self.validation_epoch
            else "stale"
        )

    @staticmethod
    def _validation_passed(result: dict[str, Any] | None) -> bool:
        return result is not None and result.get("returncode") == 0 and result.get("timed_out") is not True

    @property
    def validation_status(self) -> dict[str, dict[str, Any]]:
        return {
            "targeted": {
                "status": self._validation_status(self.targeted_test_revision, self.targeted_test_result),
                "revision": self.targeted_test_revision,
                "passed": self._validation_passed(self.targeted_test_result),
            },
            "regression": {
                "status": self._validation_status(self.regression_test_revision, self.regression_test_result),
                "revision": self.regression_test_revision,
                "passed": self._validation_passed(self.regression_test_result),
                "scope_complete": self.regression_scope_complete,
            },
            "ruff": {
                "status": self._validation_status(self.ruff_revision, self.ruff_result),
                "revision": self.ruff_revision,
                "passed": self._validation_passed(self.ruff_result),
                "scope_complete": self.ruff_scope_complete,
            },
        }

    @property
    def ready_to_submit(self) -> bool:
        return (
            bool(self.changed_files)
            and self._validation_passed(self.targeted_test_result)
            and self._validation_passed(self.regression_test_result)
            and self._validation_passed(self.ruff_result)
            and self.regression_scope_complete
            and self.ruff_scope_complete
            and all(
                self._validation_status(revision, result) == "current"
                for revision, result in (
                    (self.targeted_test_revision, self.targeted_test_result),
                    (self.regression_test_revision, self.regression_test_result),
                    (self.ruff_revision, self.ruff_result),
                )
            )
        )

    def _clear_validation(self) -> None:
        self.validation_epoch += 1
        self.targeted_test_revision = None
        self.regression_test_revision = None
        self.ruff_revision = None
        self.targeted_test_result = None
        self.regression_test_result = None
        self.ruff_result = None

    def _refresh_phase(self) -> None:
        if self.ready_to_submit:
            self.phase = RepairPhase.READY_TO_SUBMIT
        elif (
            self._validation_status(self.targeted_test_revision, self.targeted_test_result) == "current"
            and self._validation_passed(self.targeted_test_result)
            and self._validation_status(self.regression_test_revision, self.regression_test_result) == "current"
            and self._validation_passed(self.regression_test_result)
            and self.regression_scope_complete
        ):
            self.phase = RepairPhase.REGRESSION_VALIDATED
        elif self._validation_status(
            self.targeted_test_revision, self.targeted_test_result
        ) == "current" and self._validation_passed(self.targeted_test_result):
            self.phase = RepairPhase.TARGET_VALIDATED
        elif self.changed_files:
            self.phase = RepairPhase.PATCHED
        else:
            self.phase = RepairPhase.DIAGNOSING

    def record_patch(self, metadata: dict[str, Any]) -> None:
        self.revision += 1
        self.patch_count += 1
        self.changed_files = sorted(set(metadata["changed_files"]))
        self.consecutive_patch_failures = 0
        self.patch_temporarily_blocked = False
        self.last_edit_error = None
        self._clear_validation()
        self._refresh_phase()

    def record_edit_failure(self, error_code: str | None, *, patch: bool) -> None:
        self.last_edit_error = error_code
        if not patch:
            return
        self.consecutive_patch_failures += 1
        self.total_patch_failures += 1
        if self.consecutive_patch_failures >= 3:
            self.patch_temporarily_blocked = True

    def record_patch_recovery(self) -> None:
        if not self.patch_permanently_disabled:
            self.patch_temporarily_blocked = False

    def record_pytest(self, metadata: dict[str, Any]) -> None:
        scope = metadata["scope"]
        if scope == "targeted":
            self.targeted_test_revision = self.revision
            self.targeted_test_result = metadata | {"validation_epoch": self.validation_epoch}
        elif scope == "regression":
            self.regression_test_revision = self.revision
            self.regression_test_result = metadata | {"validation_epoch": self.validation_epoch}
        self._refresh_phase()

    def record_ruff(self, metadata: dict[str, Any]) -> None:
        self.ruff_revision = self.revision
        self.ruff_result = metadata | {"validation_epoch": self.validation_epoch}
        self._refresh_phase()

    def record_rollback(self, metadata: dict[str, Any] | None = None) -> None:
        self.revision += 1
        self.changed_files = sorted(set((metadata or {}).get("changed_files", [])))
        self.consecutive_patch_failures = 0
        self.patch_temporarily_blocked = False
        self.last_edit_error = None
        self._clear_validation()
        self.phase = RepairPhase.DIAGNOSING

    def record_reopen(self, reason: str) -> None:
        if self.phase != RepairPhase.READY_TO_SUBMIT or not self.ready_to_submit or not reason.strip():
            raise ValueError("Repair is not ready to reopen.")
        previous_validation_epoch = self.validation_epoch
        self.reopen_count += 1
        self.last_reopen_reason = reason
        self._clear_validation()
        self.reopen_history.append(
            {
                "reopen_count": self.reopen_count,
                "revision": self.revision,
                "reason": reason,
                "validation_epoch_before": previous_validation_epoch,
                "validation_epoch_after": self.validation_epoch,
            }
        )
        self.phase = RepairPhase.PATCHED

    def record_submission(self) -> None:
        if not self.ready_to_submit:
            raise ValueError("Repair is not ready to submit.")
        self.phase = RepairPhase.SUBMITTED


def valid_repair_actions(state: RepairSessionState, available_actions: tuple[str, ...]) -> tuple[str, ...]:
    if state.phase == RepairPhase.READY_TO_SUBMIT:
        return tuple(name for name in READY_TO_SUBMIT_ACTIONS if name in available_actions)
    if state.phase == RepairPhase.SUBMITTED:
        return ()
    return tuple(name for name in available_actions if name != "reopen_repair")

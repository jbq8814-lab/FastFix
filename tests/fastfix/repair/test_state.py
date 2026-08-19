import pytest

from fastfix.repair.state import RepairPhase, RepairSessionState


def metadata(scope: str | None = None) -> dict:
    if scope == "regression":
        return {"scope": scope, "scope_complete": True, "returncode": 0, "timed_out": False}
    return {"scope": scope, "returncode": 0, "timed_out": False} if scope else {"changed_files": ["app/main.py"]}


def ruff_metadata() -> dict:
    return {"passed": True, "scope_complete": True, "returncode": 0, "timed_out": False}


def test_initial_state_and_patch_revision() -> None:
    state = RepairSessionState()
    assert state.phase == RepairPhase.DIAGNOSING and state.revision == 0
    state.record_patch(metadata())
    assert state.phase == RepairPhase.PATCHED
    assert state.revision == 1 and state.patch_count == 1
    assert state.changed_files == ["app/main.py"]


def test_validation_advances_to_ready() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    state.record_pytest(metadata("targeted"))
    assert state.phase == RepairPhase.TARGET_VALIDATED
    state.record_pytest(metadata("regression"))
    assert state.phase == RepairPhase.REGRESSION_VALIDATED
    state.record_ruff(ruff_metadata())
    assert state.phase == RepairPhase.READY_TO_SUBMIT and state.ready_to_submit
    state.record_submission()
    assert state.phase == RepairPhase.SUBMITTED


def test_second_patch_invalidates_all_old_validation() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    state.record_pytest(metadata("targeted"))
    state.record_pytest(metadata("regression"))
    state.record_ruff(ruff_metadata())
    old_revision = state.revision
    state.record_patch({"changed_files": ["app/main.py", "app/service.py"]})
    assert state.revision == old_revision + 1
    assert state.phase == RepairPhase.PATCHED and not state.ready_to_submit
    assert state.changed_files == ["app/main.py", "app/service.py"]
    assert state.targeted_test_revision is None
    assert state.regression_test_revision is None
    assert state.ruff_revision is None


def test_old_revision_validation_cannot_validate_new_patch() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    state.record_pytest(metadata("targeted"))
    state.record_patch(metadata())
    state.regression_test_revision = state.revision
    state.ruff_revision = state.revision
    state._refresh_phase()
    assert not state.ready_to_submit


def test_current_revision_with_incomplete_scope_is_not_ready() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    state.record_pytest(metadata("targeted"))
    state.record_pytest({"scope": "regression", "scope_complete": False, "returncode": 0})
    state.record_ruff({"passed": True, "scope_complete": False, "returncode": 0})
    assert state.regression_test_revision == state.ruff_revision == state.revision
    assert state.phase == RepairPhase.TARGET_VALIDATED
    assert not state.ready_to_submit


def test_rollback_clears_state_and_increments_revision() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    state.record_pytest(metadata("targeted"))
    state.record_rollback()
    assert state.revision == 2
    assert state.phase == RepairPhase.DIAGNOSING
    assert state.changed_files == [] and not state.ready_to_submit


def test_changed_files_replace_previous_revision_snapshot() -> None:
    state = RepairSessionState()
    state.record_patch({"changed_files": ["app/main.py", "app/service.py"]})
    state.record_patch({"changed_files": ["app/main.py"]})
    assert state.changed_files == ["app/main.py"]
    state.record_patch({"changed_files": []})
    assert state.changed_files == []
    state.record_patch({"changed_files": ["app/service.py"]})
    assert state.changed_files == ["app/service.py"]


def test_patch_failures_block_and_successful_edit_resets_consecutive_failures() -> None:
    state = RepairSessionState()
    for _ in range(3):
        state.record_edit_failure("patch_apply_failed", patch=True)
    assert state.patch_temporarily_blocked
    assert state.consecutive_patch_failures == 3
    assert state.total_patch_failures == 3
    state.record_patch_recovery()
    assert not state.patch_temporarily_blocked
    state.record_patch(metadata())
    assert state.consecutive_patch_failures == 0
    assert state.total_patch_failures == 3
    assert not state.patch_temporarily_blocked
    assert state.last_edit_error is None


def test_six_patch_failures_permanently_disable_patch() -> None:
    state = RepairSessionState()
    for _ in range(6):
        state.record_edit_failure("patch_invalid", patch=True)
        state.record_patch_recovery()
    assert state.patch_permanently_disabled
    assert state.total_patch_failures == 6
    assert state.patch_temporarily_blocked


def test_failed_non_patch_edit_does_not_advance_revision() -> None:
    state = RepairSessionState()
    state.record_edit_failure("text_not_found", patch=False)
    assert state.revision == 0 and state.patch_count == 0
    assert state.last_edit_error == "text_not_found"


def test_rollback_resets_current_failure_state_but_preserves_total() -> None:
    state = RepairSessionState()
    state.record_edit_failure("patch_apply_failed", patch=True)
    state.record_rollback()
    assert state.total_patch_failures == 1
    assert state.consecutive_patch_failures == 0
    assert not state.patch_temporarily_blocked
    assert state.last_edit_error is None


def test_reopen_preserves_revision_and_diff_but_clears_validation() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    state.record_pytest(metadata("targeted"))
    state.record_pytest(metadata("regression"))
    state.record_ruff(ruff_metadata())
    state.record_reopen("Review uncovered another edge case.")
    assert state.phase == RepairPhase.PATCHED
    assert state.revision == 1 and state.changed_files == ["app/main.py"]
    assert state.validation_status["targeted"]["status"] == "missing"
    assert state.validation_status["regression"]["status"] == "missing"
    assert state.validation_status["ruff"]["status"] == "missing"
    assert state.reopen_count == 1
    assert state.last_reopen_reason == "Review uncovered another edge case."
    assert state.reopen_history == [
        {
            "reopen_count": 1,
            "revision": 1,
            "reason": "Review uncovered another edge case.",
            "validation_epoch_before": 1,
            "validation_epoch_after": 2,
        }
    ]


def test_reopen_epoch_prevents_same_revision_validation_reuse() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    state.record_pytest(metadata("targeted"))
    state.record_pytest(metadata("regression"))
    state.record_ruff(ruff_metadata())
    old_results = state.targeted_test_result, state.regression_test_result, state.ruff_result
    state.record_reopen("Review requires another validation cycle.")
    state.targeted_test_revision = state.regression_test_revision = state.ruff_revision = state.revision
    state.targeted_test_result, state.regression_test_result, state.ruff_result = old_results
    state._refresh_phase()
    assert state.revision == 1 and state.validation_epoch == 2
    assert all(item["status"] == "stale" for item in state.validation_status.values())
    assert not state.ready_to_submit


def test_repeated_reopen_requires_revalidation_and_preserves_audit_history() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    for reason in ("First review.", "Second review."):
        state.record_pytest(metadata("targeted"))
        state.record_pytest(metadata("regression"))
        state.record_ruff(ruff_metadata())
        state.record_reopen(reason)
        with pytest.raises(ValueError, match="not ready to reopen"):
            state.record_reopen("Immediate duplicate.")
    assert state.revision == 1 and state.reopen_count == 2
    assert state.validation_epoch == 3
    assert [entry["reason"] for entry in state.reopen_history] == ["First review.", "Second review."]


def test_validation_status_distinguishes_current_missing_and_stale() -> None:
    state = RepairSessionState()
    state.record_patch(metadata())
    state.record_pytest(metadata("targeted"))
    state.regression_test_revision = 0
    state.regression_test_result = metadata("regression")
    assert state.validation_status["targeted"]["status"] == "current"
    assert state.validation_status["regression"]["status"] == "stale"
    assert state.validation_status["ruff"]["status"] == "missing"

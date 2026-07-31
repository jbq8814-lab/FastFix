import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fastfix.environments.tool_environment import FastFixToolEnvironment
from fastfix.repair.models import ReopenRepairArgs, SubmitRepairArgs
from fastfix.repair.state import RepairPhase, RepairSessionState, valid_repair_actions
from fastfix.security.paths import WorkspacePathPolicy
from fastfix.tools.models import ToolResult
from fastfix.tools.registry import ToolRegistry
from minisweagent.exceptions import Submitted
from minisweagent.utils.serialize import recursive_merge


class FastFixRepairEnvironment(FastFixToolEnvironment):
    PATCH_RETRY_MESSAGE = (
        "Repeated patch attempts failed. Re-read the target file and use replace_text for a localized edit, "
        "or rollback before trying another patch."
    )

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        workspace: Path,
        regression_targets: tuple[str, ...] = ("tests",),
        ruff_paths: tuple[str, ...] = ("app",),
    ):
        super().__init__(registry=registry, workspace=workspace)
        self.repair_state = RepairSessionState()
        self.regression_targets = self._normalize_paths(workspace, regression_targets)
        self.ruff_paths = self._normalize_paths(workspace, ruff_paths)

    @staticmethod
    def _normalize_paths(workspace: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
        policy = WorkspacePathPolicy(workspace, allowed_paths=paths)
        return tuple(policy.to_relative(policy.resolve(path, must_exist=True)) for path in paths)

    @staticmethod
    def _scope_complete(
        result: dict[str, Any] | None,
        *,
        actual: str,
        required: str,
        expected: tuple[str, ...],
    ) -> bool:
        return (
            result is not None
            and result.get("scope_complete") is True
            and result.get(actual) == list(expected)
            and result.get(required) == list(expected)
        )

    @property
    def tool_names(self) -> tuple[str, ...]:
        return (*self.registry.names(), "submit_repair", "reopen_repair")

    @property
    def valid_actions(self) -> tuple[str, ...]:
        return valid_repair_actions(self.repair_state, self.tool_names)

    @property
    def state_card(self) -> dict[str, Any]:
        return {
            "revision": self.repair_state.revision,
            "validation_epoch": self.repair_state.validation_epoch,
            "phase": self.repair_state.phase.value,
            "changed_files": self.repair_state.changed_files,
            "ready_to_submit": self.repair_state.ready_to_submit,
            "validation": self.repair_state.validation_status,
            "valid_actions": list(self.valid_actions),
            "ready_lock": self.repair_state.phase == RepairPhase.READY_TO_SUBMIT,
            "patch_failures": {
                "consecutive": self.repair_state.consecutive_patch_failures,
                "total": self.repair_state.total_patch_failures,
                "temporarily_blocked": self.repair_state.patch_temporarily_blocked,
                "permanently_disabled": self.repair_state.patch_permanently_disabled,
            },
            "last_edit_error": self.repair_state.last_edit_error,
            "reopen_count": self.repair_state.reopen_count,
        }

    def _output(self, result: ToolResult) -> dict[str, Any]:
        output = super()._output(result)
        state_card = self.state_card
        output["output"] = (
            f"{output['output']}\n[FastFix Repair State]\n"
            f"{json.dumps(state_card, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        )
        output["extra"]["state_card"] = state_card
        output["extra"]["repair_revision"] = self.repair_state.revision
        output["extra"]["validation_epoch"] = self.repair_state.validation_epoch
        return output

    def _changed_files(self) -> list[str]:
        git = shutil.which("git")
        if git is None:
            raise ValueError("Git executable was not found.")
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        result = subprocess.run(
            [git, "diff", "--name-only", "HEAD"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
            env={key: value for key, value in os.environ.items() if key.upper() in allowed},
        )
        if result.returncode:
            raise RuntimeError("Unable to inspect changed files.")
        return sorted(path for path in result.stdout.splitlines() if path)

    def _validation_missing(self, submission: SubmitRepairArgs, changed_files: list[str]) -> list[str]:
        state = self.repair_state
        missing = []
        if not changed_files:
            missing.append("non-empty diff")
        if state._validation_status(state.targeted_test_revision, state.targeted_test_result) != "current":
            missing.append("targeted pytest")
        elif not state._validation_passed(state.targeted_test_result):
            missing.append("targeted pytest pass")
        if state._validation_status(state.regression_test_revision, state.regression_test_result) != "current":
            missing.append("regression pytest")
        elif not state._validation_passed(state.regression_test_result):
            missing.append("regression pytest pass")
        elif not self._scope_complete(
            state.regression_test_result,
            actual="targets",
            required="required_targets",
            expected=self.regression_targets,
        ):
            missing.append("complete regression scope")
        if state._validation_status(state.ruff_revision, state.ruff_result) != "current":
            missing.append("ruff")
        elif not state._validation_passed(state.ruff_result):
            missing.append("ruff pass")
        elif not self._scope_complete(
            state.ruff_result,
            actual="paths",
            required="required_paths",
            expected=self.ruff_paths,
        ):
            missing.append("complete ruff scope")
        if sorted(submission.changed_files) != changed_files:
            missing.append("changed file consistency")
        if any(path.startswith("tests/") for path in changed_files):
            missing.append("tests unchanged")
        if changed_files != sorted(state.changed_files):
            missing.append("allowed source paths")
        return missing

    def _synchronize_edit_result(self, result: ToolResult) -> ToolResult:
        changed_files = self._changed_files()
        reported = result.metadata.get("changed_files")
        self.repair_state.record_patch({"changed_files": changed_files})
        if reported == changed_files:
            return result
        return ToolResult(
            tool_name=result.tool_name,
            ok=False,
            error_code="changed_files_mismatch",
            output="The edit result did not match the current Git diff.",
            metadata={"changed_files": changed_files, "reported_changed_files": reported},
        )

    def _synchronize_rollback_result(self, result: ToolResult) -> ToolResult:
        changed_files = self._changed_files()
        reported = result.metadata.get("changed_files")
        if result.ok:
            self.repair_state.record_rollback({"changed_files": changed_files})
        elif changed_files != self.repair_state.changed_files:
            self.repair_state.record_patch({"changed_files": changed_files})
        if reported == changed_files or not result.ok and reported is None:
            return result
        return ToolResult(
            tool_name=result.tool_name,
            ok=False,
            error_code="changed_files_mismatch",
            output="The rollback result did not match the current Git diff.",
            metadata={"changed_files": changed_files, "reported_changed_files": reported},
        )

    def _submit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            submission = SubmitRepairArgs.model_validate(arguments)
        except ValidationError:
            result = ToolResult(
                tool_name="submit_repair",
                ok=False,
                error_code="invalid_arguments",
                output="Invalid repair submission arguments.",
            )
            self._record(result)
            return self._output(result)

        changed_files = self._changed_files()
        missing = self._validation_missing(submission, changed_files)
        if missing or not self.repair_state.ready_to_submit:
            result = ToolResult(
                tool_name="submit_repair",
                ok=False,
                error_code="validation_incomplete",
                output=f"Repair validation is incomplete: {', '.join(dict.fromkeys(missing))}.",
                metadata={"missing": list(dict.fromkeys(missing)), "changed_files": changed_files},
            )
            self._record(result)
            return self._output(result)

        self.repair_state.record_submission()
        payload = {
            "submission": submission.model_dump(mode="json"),
            "repair_state": self.repair_state.model_dump(mode="json"),
            "changed_files": changed_files,
            "validation": {
                "targeted": self.repair_state.targeted_test_result,
                "regression": self.repair_state.regression_test_result,
                "ruff": self.repair_state.ruff_result,
            },
        }
        result = ToolResult(
            tool_name="submit_repair",
            ok=True,
            output="Repair submitted.",
            metadata={"changed_files": changed_files},
        )
        self._record(result)
        payload_json = json.dumps(payload, ensure_ascii=False)
        raise Submitted(
            {
                "role": "exit",
                "content": payload_json,
                "extra": {
                    "exit_status": "Submitted",
                    "submission": payload_json,
                    "repair": payload,
                },
            }
        )

    def _reopen(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            reopen = ReopenRepairArgs.model_validate(arguments)
        except ValidationError:
            result = ToolResult(
                tool_name="reopen_repair",
                ok=False,
                error_code="invalid_arguments",
                output="Invalid reopen arguments.",
            )
            self._record(result)
            return self._output(result)
        self.repair_state.record_reopen(reopen.reason)
        result = ToolResult(
            tool_name="reopen_repair",
            ok=True,
            output="Repair reopened; validation for the current revision was cleared.",
            metadata={
                "revision": self.repair_state.revision,
                "reopen_count": self.repair_state.reopen_count,
                "reason": reopen.reason,
            },
        )
        self._record(result)
        return self._output(result)

    def execute(self, action: dict) -> dict[str, Any]:
        tool_name = action.get("tool", "")
        arguments = action.get("arguments", {})
        if tool_name in self.tool_names and tool_name not in self.valid_actions:
            ready_locked = self.repair_state.phase == RepairPhase.READY_TO_SUBMIT
            result = ToolResult(
                tool_name=tool_name,
                ok=False,
                error_code="repair_ready_locked" if ready_locked else "repair_not_ready_to_reopen",
                output=(
                    "Repair is ready to submit. Submit, inspect the diff, rollback, or reopen it explicitly."
                    if ready_locked
                    else "Repair can only be reopened from ready_to_submit."
                ),
            )
            self._record(result)
            return self._output(result)
        if tool_name == "submit_repair":
            return self._submit(arguments)
        if tool_name == "reopen_repair":
            return self._reopen(arguments)
        if tool_name == "apply_patch" and (
            self.repair_state.patch_temporarily_blocked or self.repair_state.patch_permanently_disabled
        ):
            result = ToolResult(
                tool_name=tool_name,
                ok=False,
                error_code="patch_retry_limit",
                output=self.PATCH_RETRY_MESSAGE,
                metadata={
                    "total_patch_failures": self.repair_state.total_patch_failures,
                    "permanently_disabled": self.repair_state.patch_permanently_disabled,
                },
            )
            self._record(result)
            return self._output(result)

        result = self.registry.execute(tool_name, arguments)
        synchronized = tool_name in {"apply_patch", "replace_text"} and (
            result.ok or result.metadata.get("diff_may_have_changed") is True
        )
        if synchronized:
            result = self._synchronize_edit_result(result)
        elif tool_name == "rollback_changes":
            result = self._synchronize_rollback_result(result)
        self._record(result)
        if tool_name == "run_pytest" and result.metadata.get("scope") in {"targeted", "regression"}:
            self.repair_state.record_pytest(
                result.metadata
                | {
                    "ok": result.ok,
                    "error_code": result.error_code,
                    "validation_epoch": self.repair_state.validation_epoch,
                }
            )
        elif tool_name == "run_ruff" and "returncode" in result.metadata:
            self.repair_state.record_ruff(
                result.metadata
                | {
                    "ok": result.ok,
                    "error_code": result.error_code,
                    "validation_epoch": self.repair_state.validation_epoch,
                }
            )
        elif result.ok:
            if tool_name in {"read_file", "show_git_diff"}:
                self.repair_state.record_patch_recovery()
        elif tool_name in {"apply_patch", "replace_text"} and not synchronized:
            self.repair_state.record_edit_failure(
                result.error_code,
                patch=tool_name == "apply_patch",
            )
        return self._output(result)

    def serialize(self) -> dict[str, object]:
        return recursive_merge(
            super().serialize(),
            {
                "info": {
                    "repair_state": self.repair_state.model_dump(mode="json"),
                    "repair_state_card": self.state_card,
                }
            },
        )

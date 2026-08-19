import subprocess
from pathlib import Path

import pytest

from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.repair.state import RepairPhase
from fastfix.tools.editing import (
    ApplyPatchArgs,
    ReplaceTextArgs,
    RollbackChangesArgs,
    ShowGitDiffArgs,
    WorkspaceGitTools,
)
from fastfix.tools.fastapi import FastApiTools
from fastfix.tools.models import ToolResult
from fastfix.tools.registry import ToolRegistry, ToolSpec
from fastfix.tools.repository import ReadFileArgs, RepositoryTools, SearchCodeArgs, ShowTreeArgs
from fastfix.tools.validation import RunPytestArgs, RunRuffArgs
from minisweagent.exceptions import Submitted


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        shell=False,
    )


def workspace(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "FastFix Tests")
    git(tmp_path, "config", "user.email", "fastfix@example.invalid")
    git(tmp_path, "config", "core.autocrlf", "false")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def validation_result(arguments, tool_name: str, *, ok: bool = True) -> ToolResult:
    scope = getattr(arguments, "scope", None)
    targets = (arguments.targets or ["tests"]) if scope else None
    paths = (arguments.paths or ["app"]) if tool_name == "run_ruff" else None
    return ToolResult(
        tool_name=tool_name,
        ok=ok,
        output="1 passed" if ok else "1 failed",
        error_code=None if ok else "validation_failed",
        metadata={
            **(
                {
                    "scope": scope,
                    "targets": targets,
                    **(
                        {
                            "required_targets": ["tests"],
                            "scope_complete": targets == ["tests"],
                        }
                        if scope == "regression"
                        else {}
                    ),
                }
                if scope
                else {
                    "paths": paths,
                    "required_paths": ["app"],
                    "scope_complete": paths == ["app"],
                }
            ),
            "returncode": 0 if ok else 1,
            "passed": (True if tool_name == "run_ruff" else 1) if ok else False,
            "timed_out": False,
        },
    )


def registry(repository_path: Path, *, validation_ok: bool = True) -> ToolRegistry:
    repository = RepositoryTools(repository_path)
    editing = WorkspaceGitTools(repository_path)
    result = ToolRegistry()
    for spec in (
        ToolSpec("show_tree", "Tree.", ShowTreeArgs, repository.show_tree),
        ToolSpec("read_file", "Read.", ReadFileArgs, repository.read_file),
        ToolSpec("search_code", "Search.", SearchCodeArgs, repository.search_code),
    ):
        result.register(spec)
    FastApiTools(repository_path, allowed_paths=("app",)).register(result)
    for spec in (
        ToolSpec("replace_text", "Replace.", ReplaceTextArgs, editing.replace_text),
        ToolSpec("apply_patch", "Patch.", ApplyPatchArgs, editing.apply_patch),
        ToolSpec(
            "run_pytest",
            "Test.",
            RunPytestArgs,
            lambda args: validation_result(args, "run_pytest", ok=validation_ok),
        ),
        ToolSpec(
            "run_ruff",
            "Ruff.",
            RunRuffArgs,
            lambda args: validation_result(args, "run_ruff", ok=validation_ok),
        ),
        ToolSpec("show_git_diff", "Diff.", ShowGitDiffArgs, editing.show_git_diff),
        ToolSpec("rollback_changes", "Rollback.", RollbackChangesArgs, editing.rollback_changes),
    ):
        result.register(spec)
    return result


def action(tool: str, arguments: dict | None = None) -> dict:
    return {"tool": tool, "arguments": arguments or {}, "tool_call_id": tool}


def patch() -> str:
    return (
        "diff --git a/app/main.py b/app/main.py\n"
        "--- a/app/main.py\n"
        "+++ b/app/main.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )


def submit_arguments(changed_files: list[str] | None = None) -> dict:
    return {
        "summary": "Repair.",
        "root_cause": "Incorrect value.",
        "changed_files": changed_files or ["app/main.py"],
        "tests_run": ["targeted", "regression", "ruff"],
        "risk_notes": [],
        "confidence": 1.0,
    }


def prepared_environment(tmp_path: Path, *, validation_ok: bool = True) -> FastFixRepairEnvironment:
    repository = workspace(tmp_path)
    return FastFixRepairEnvironment(
        registry=registry(repository, validation_ok=validation_ok),
        workspace=repository,
    )


def prepared_two_file_environment(tmp_path: Path) -> FastFixRepairEnvironment:
    repository = workspace(tmp_path)
    (repository / "app" / "service.py").write_text("result = 1\n", encoding="utf-8")
    git(repository, "add", "app/service.py")
    git(repository, "commit", "--amend", "--no-edit", "-q")
    return FastFixRepairEnvironment(registry=registry(repository), workspace=repository)


def apply_and_validate(environment: FastFixRepairEnvironment, count: int = 3) -> None:
    environment.execute(action("apply_patch", {"patch": patch()}))
    if count >= 1:
        environment.execute(
            action(
                "run_pytest",
                {"scope": "targeted", "targets": ["tests/test_main.py::test_ok"]},
            )
        )
    if count >= 2:
        environment.execute(action("run_pytest", {"scope": "regression"}))
    if count >= 3:
        environment.execute(action("run_ruff"))


def test_tool_calls_update_repair_state_and_rollback(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    assert environment.repair_state.phase == RepairPhase.READY_TO_SUBMIT
    environment.execute(action("rollback_changes", {"reason": "test"}))
    assert environment.repair_state.phase == RepairPhase.DIAGNOSING
    assert environment.repair_state.changed_files == []
    assert [call["tool_name"] for call in environment.tool_call_history] == [
        "apply_patch",
        "run_pytest",
        "run_pytest",
        "run_ruff",
        "rollback_changes",
    ]


@pytest.mark.parametrize(
    ("validation_count", "missing"),
    [
        (0, "targeted pytest"),
        (1, "regression pytest"),
        (2, "ruff"),
    ],
)
def test_incomplete_validation_cannot_submit(
    tmp_path: Path,
    validation_count: int,
    missing: str,
) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment, validation_count)
    result = environment.execute(action("submit_repair", submit_arguments()))
    assert result["returncode"] == 1
    assert result["extra"]["error_code"] == "validation_incomplete"
    assert missing in result["output"]


def test_submission_changed_files_must_match_real_diff(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    result = environment.execute(action("submit_repair", submit_arguments(["app/other.py"])))
    assert result["extra"]["error_code"] == "validation_incomplete"
    assert "changed file consistency" in result["output"]


def test_test_modification_is_rejected_at_submission(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok():\n    assert False\n", encoding="utf-8")
    result = environment.execute(action("submit_repair", submit_arguments(["app/main.py", "tests/test_main.py"])))
    assert result["extra"]["error_code"] == "validation_incomplete"
    assert "tests unchanged" in result["output"]


def test_complete_validation_submits_structured_payload(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    with pytest.raises(Submitted) as submitted:
        environment.execute(action("submit_repair", submit_arguments()))
    message = submitted.value.messages[0]
    assert message["extra"]["repair"]["changed_files"] == ["app/main.py"]
    assert message["extra"]["repair"]["repair_state"]["phase"] == "submitted"
    assert environment.tool_call_history[-1]["tool_name"] == "submit_repair"
    serialized = environment.serialize()
    assert str(tmp_path.resolve()) not in str(serialized)


def test_incomplete_regression_and_ruff_scope_block_submission_until_full_validation(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    environment.execute(action("apply_patch", {"patch": patch()}))
    environment.execute(
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_ok"]},
        )
    )
    environment.execute(
        action(
            "run_pytest",
            {"scope": "regression", "targets": ["tests/test_main.py"]},
        )
    )
    environment.execute(action("run_ruff", {"paths": ["app/other_dir"]}))
    assert environment.repair_state.regression_test_revision == environment.repair_state.revision
    assert environment.repair_state.ruff_revision == environment.repair_state.revision

    denied = environment.execute(action("submit_repair", submit_arguments()))
    assert denied["extra"]["error_code"] == "validation_incomplete"
    assert denied["extra"]["metadata"]["missing"] == ["complete regression scope", "complete ruff scope"]

    environment.execute(action("run_pytest", {"scope": "regression", "targets": ["tests"]}))
    environment.execute(action("run_ruff", {"paths": ["app"]}))
    with pytest.raises(Submitted):
        environment.execute(action("submit_repair", submit_arguments()))


def test_replace_success_advances_revision_and_invalidates_validation(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    revision = environment.repair_state.revision
    locked = environment.execute(
        action(
            "replace_text",
            {
                "path": "app/main.py",
                "old_text": "value = 2",
                "new_text": "value = 3",
            },
        )
    )
    assert locked["extra"]["error_code"] == "repair_ready_locked"
    assert environment.repair_state.revision == revision
    environment.execute(action("reopen_repair", {"reason": "A further change is required."}))
    result = environment.execute(
        action(
            "replace_text",
            {
                "path": "app/main.py",
                "old_text": "value = 2",
                "new_text": "value = 3",
            },
        )
    )
    assert result["returncode"] == 0
    assert environment.repair_state.revision == revision + 1
    assert environment.repair_state.targeted_test_revision is None
    assert environment.repair_state.regression_test_revision is None
    assert environment.repair_state.ruff_revision is None


def test_local_revert_synchronizes_diff_invalidates_validation_and_can_revalidate(
    tmp_path: Path,
) -> None:
    environment = prepared_two_file_environment(tmp_path)
    for path, old_text, new_text in (
        ("app/main.py", "value = 1", "value = 2"),
        ("app/service.py", "result = 1", "result = 2"),
    ):
        assert (
            environment.execute(action("replace_text", {"path": path, "old_text": old_text, "new_text": new_text}))[
                "returncode"
            ]
            == 0
        )
    assert environment.repair_state.changed_files == ["app/main.py", "app/service.py"]
    for tool, arguments in (
        ("run_pytest", {"scope": "targeted", "targets": ["tests/test_main.py::test_ok"]}),
        ("run_pytest", {"scope": "regression"}),
        ("run_ruff", {}),
    ):
        environment.execute(action(tool, arguments))
    revision = environment.repair_state.revision
    environment.execute(action("reopen_repair", {"reason": "Revert the unnecessary service edit."}))
    reverted = environment.execute(
        action(
            "replace_text",
            {"path": "app/service.py", "old_text": "result = 2", "new_text": "result = 1"},
        )
    )
    assert reverted["returncode"] == 0
    assert environment.repair_state.revision == revision + 1
    assert environment.repair_state.changed_files == ["app/main.py"]
    assert environment.repair_state.targeted_test_revision is None
    assert environment.repair_state.regression_test_revision is None
    assert environment.repair_state.ruff_revision is None
    denied = environment.execute(action("submit_repair", submit_arguments(["app/main.py"])))
    assert denied["extra"]["error_code"] == "validation_incomplete"
    for tool, arguments in (
        ("run_pytest", {"scope": "targeted", "targets": ["tests/test_main.py::test_ok"]}),
        ("run_pytest", {"scope": "regression"}),
        ("run_ruff", {}),
    ):
        environment.execute(action(tool, arguments))
    with pytest.raises(Submitted):
        environment.execute(action("submit_repair", submit_arguments(["app/main.py"])))


def test_full_revert_clears_changed_files_and_reedit_adds_file_again(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    environment.execute(
        action("replace_text", {"path": "app/main.py", "old_text": "value = 1", "new_text": "value = 2"})
    )
    environment.execute(
        action("replace_text", {"path": "app/main.py", "old_text": "value = 2", "new_text": "value = 1"})
    )
    assert environment.repair_state.changed_files == []
    assert environment.repair_state.phase == RepairPhase.DIAGNOSING
    environment.execute(
        action("replace_text", {"path": "app/main.py", "old_text": "value = 1", "new_text": "value = 3"})
    )
    assert environment.repair_state.changed_files == ["app/main.py"]


def test_mismatched_edit_metadata_is_rejected_after_true_diff_sync(tmp_path: Path) -> None:
    repository = workspace(tmp_path)
    mismatched = ToolRegistry()

    def replace(arguments: ReplaceTextArgs) -> ToolResult:
        (repository / arguments.path).write_text("value = 2\n", encoding="utf-8")
        return ToolResult(
            tool_name="replace_text",
            ok=True,
            metadata={"changed_files": ["app/service.py"]},
        )

    mismatched.register(ToolSpec("replace_text", "Replace.", ReplaceTextArgs, replace))
    environment = FastFixRepairEnvironment(registry=mismatched, workspace=repository)
    result = environment.execute(
        action("replace_text", {"path": "app/main.py", "old_text": "value = 1", "new_text": "value = 2"})
    )
    assert result["extra"]["error_code"] == "changed_files_mismatch"
    assert result["extra"]["metadata"]["reported_changed_files"] == ["app/service.py"]
    assert environment.repair_state.changed_files == ["app/main.py"]
    assert environment.repair_state.revision == 1


def test_failed_replace_does_not_advance_revision(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    result = environment.execute(
        action(
            "replace_text",
            {
                "path": "app/main.py",
                "old_text": "missing",
                "new_text": "replacement",
            },
        )
    )
    assert result["extra"]["error_code"] == "text_not_found"
    assert environment.repair_state.revision == 0


def fail_patch(environment: FastFixRepairEnvironment) -> dict:
    return environment.execute(action("apply_patch", {"patch": "invalid patch"}))


def test_three_patch_failures_trigger_limit_and_read_unlocks_once(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    for _ in range(3):
        assert fail_patch(environment)["extra"]["error_code"] == "patch_invalid"
    blocked = fail_patch(environment)
    assert blocked["extra"]["error_code"] == "patch_retry_limit"
    assert environment.repair_state.revision == 0
    environment.execute(action("read_file", {"path": "app/main.py"}))
    assert fail_patch(environment)["extra"]["error_code"] == "patch_invalid"
    assert environment.repair_state.patch_temporarily_blocked


def test_six_failures_permanently_disable_patch_but_replace_still_works(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    for _ in range(6):
        if environment.repair_state.patch_temporarily_blocked:
            environment.execute(action("show_git_diff"))
        assert fail_patch(environment)["extra"]["error_code"] == "patch_invalid"
    assert environment.repair_state.patch_permanently_disabled
    environment.execute(action("read_file", {"path": "app/main.py"}))
    assert fail_patch(environment)["extra"]["error_code"] == "patch_retry_limit"
    replaced = environment.execute(
        action(
            "replace_text",
            {
                "path": "app/main.py",
                "old_text": "value = 1",
                "new_text": "value = 2",
            },
        )
    )
    assert replaced["returncode"] == 0
    assert environment.repair_state.revision == 1
    assert environment.repair_state.patch_permanently_disabled


def test_rollback_clears_current_patch_failure_state_but_preserves_total(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    fail_patch(environment)
    environment.execute(action("rollback_changes", {"reason": "recover"}))
    assert environment.repair_state.total_patch_failures == 1
    assert environment.repair_state.consecutive_patch_failures == 0
    assert environment.repair_state.last_edit_error is None


def test_ready_lock_rejects_route_analysis_without_state_changes(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    before = environment.repair_state.model_dump()
    result = environment.execute(action("inspect_fastapi_routes"))
    assert result["extra"]["error_code"] == "repair_ready_locked"
    assert environment.repair_state.model_dump() == before
    denied = environment.execute(action("inspect_fastapi_routes", {"path": ".env"}))
    assert denied["returncode"] == 1
    assert denied["extra"]["error_code"] == "repair_ready_locked"
    assert environment.repair_state.model_dump() == before


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "replace_text",
            {"path": "app/main.py", "old_text": "value = 2", "new_text": "value = 3"},
        ),
        ("apply_patch", {"patch": "invalid patch"}),
        ("read_file", {"path": "app/main.py"}),
        ("search_code", {"query": "value"}),
        ("show_tree", {}),
        ("run_pytest", {"scope": "regression"}),
        ("run_ruff", {}),
        ("inspect_fastapi_routes", {}),
    ],
)
def test_ready_lock_preserves_candidate_revision_and_validation(
    tmp_path: Path,
    tool: str,
    arguments: dict,
) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    before = environment.repair_state.model_dump()
    before_content = (tmp_path / "app" / "main.py").read_text(encoding="utf-8")
    result = environment.execute(action(tool, arguments))
    assert result["extra"]["error_code"] == "repair_ready_locked"
    assert environment.repair_state.model_dump() == before
    assert (tmp_path / "app" / "main.py").read_text(encoding="utf-8") == before_content
    assert result["extra"]["state_card"]["valid_actions"] == list(environment.valid_actions)


def test_reopen_preserves_diff_clears_validation_and_allows_edit(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    before_content = (tmp_path / "app" / "main.py").read_text(encoding="utf-8")
    revision = environment.repair_state.revision
    reopened = environment.execute(action("reopen_repair", {"reason": "Review requires another edit."}))
    assert reopened["returncode"] == 0
    assert environment.repair_state.revision == revision
    assert (tmp_path / "app" / "main.py").read_text(encoding="utf-8") == before_content
    assert all(item["status"] == "missing" for item in environment.state_card["validation"].values())
    edited = environment.execute(
        action(
            "replace_text",
            {"path": "app/main.py", "old_text": "value = 2", "new_text": "value = 3"},
        )
    )
    assert edited["returncode"] == 0
    assert environment.repair_state.revision == revision + 1
    assert environment.repair_state.reopen_count == 1


def test_submit_rejects_pre_reopen_validation_from_same_revision(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    state = environment.repair_state
    old_results = state.targeted_test_result, state.regression_test_result, state.ruff_result
    environment.execute(action("reopen_repair", {"reason": "Require fresh validation."}))
    state.targeted_test_revision = state.regression_test_revision = state.ruff_revision = state.revision
    state.targeted_test_result, state.regression_test_result, state.ruff_result = old_results
    denied = environment.execute(action("submit_repair", submit_arguments()))
    assert denied["extra"]["error_code"] == "validation_incomplete"
    assert denied["extra"]["metadata"]["missing"] == ["targeted pytest", "regression pytest", "ruff"]


def test_reopen_requires_non_blank_reason(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    apply_and_validate(environment)
    before = environment.repair_state.model_dump()
    denied = environment.execute(action("reopen_repair", {"reason": "   "}))
    assert denied["extra"]["error_code"] == "invalid_arguments"
    assert environment.repair_state.model_dump() == before


def test_state_card_policy_is_shared_with_execution_layer(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    assert environment.state_card["valid_actions"] == list(environment.valid_actions)
    assert "reopen_repair" not in environment.valid_actions
    denied = environment.execute(action("reopen_repair", {"reason": "Too early."}))
    assert denied["extra"]["error_code"] == "repair_not_ready_to_reopen"
    apply_and_validate(environment)
    assert environment.state_card["valid_actions"] == [
        "show_git_diff",
        "submit_repair",
        "rollback_changes",
        "reopen_repair",
    ]
    assert environment.execute(action("show_git_diff"))["returncode"] == 0


def test_validation_failure_is_current_in_state_card(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path, validation_ok=False)
    environment.execute(action("apply_patch", {"patch": patch()}))
    failed = environment.execute(
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_ok"]},
        )
    )
    targeted = failed["extra"]["state_card"]["validation"]["targeted"]
    assert failed["extra"]["error_code"] == "validation_failed"
    assert targeted == {
        "status": "current",
        "revision": environment.repair_state.revision,
        "passed": False,
    }
    assert "validation_failed" in failed["output"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("run_pytest", {}),
        ("run_ruff", {"timeout_seconds": 0}),
    ],
)
def test_invalid_validation_arguments_preserve_state(
    tmp_path: Path,
    tool: str,
    arguments: dict,
) -> None:
    environment = prepared_environment(tmp_path)
    environment.execute(action("apply_patch", {"patch": patch()}))
    before = environment.repair_state.model_dump()
    denied = environment.execute(action(tool, arguments))
    assert denied["extra"]["error_code"] == "invalid_arguments"
    assert environment.repair_state.model_dump() == before


def test_route_analysis_does_not_unlock_patch_limit(tmp_path: Path) -> None:
    environment = prepared_environment(tmp_path)
    for _ in range(3):
        fail_patch(environment)
    assert environment.repair_state.patch_temporarily_blocked
    revision = environment.repair_state.revision
    result = environment.execute(action("inspect_fastapi_routes"))
    assert result["returncode"] == 0
    assert environment.repair_state.revision == revision
    assert environment.repair_state.patch_temporarily_blocked
    denied = environment.execute(action("inspect_fastapi_routes", {"path": ".env"}))
    assert denied["returncode"] == 1
    assert environment.repair_state.patch_temporarily_blocked
    assert fail_patch(environment)["extra"]["error_code"] == "patch_retry_limit"

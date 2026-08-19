import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

from fastfix.agents.repair import FastFixRepairAgent
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.sandbox import ValidationExecution
from fastfix.tools.repair import build_repair_registry
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output


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
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text(
        "from app.main import value\n\ndef test_value():\n    assert value == 2\n",
        encoding="utf-8",
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "FastFix Tests")
    git(tmp_path, "config", "user.email", "fastfix@example.invalid")
    git(tmp_path, "config", "core.autocrlf", "false")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def patch() -> str:
    return (
        "diff --git a/app/main.py b/app/main.py\n"
        "--- a/app/main.py\n"
        "+++ b/app/main.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )


def submission() -> dict:
    return {
        "summary": "Correct the value.",
        "root_cause": "The source value was incorrect.",
        "changed_files": ["app/main.py"],
        "tests_run": ["targeted", "regression", "ruff"],
        "risk_notes": [],
        "confidence": 1.0,
    }


def action(tool: str, arguments: dict, number: int) -> dict:
    return {"tool": tool, "arguments": arguments, "tool_call_id": str(number)}


def outputs(actions: list[dict]) -> list[dict]:
    return [make_toolcall_output(None, [{"id": item["tool_call_id"]}], [item]) for item in actions]


def build_agent(tmp_path: Path, actions: list[dict], **config) -> FastFixRepairAgent:
    repository = workspace(tmp_path)
    environment = FastFixRepairEnvironment(
        registry=build_repair_registry(repository, python_executable=Path(sys.executable)),
        workspace=repository,
    )
    return FastFixRepairAgent(
        DeterministicToolcallModel(outputs=outputs(actions), cost_per_call=0),
        environment,
        system_template="Use structured repair tools.",
        instance_template="{{ task }}",
        cost_limit=0,
        **config,
    )


class RecordingToolcallModel(DeterministicToolcallModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queries: list[list[dict]] = []

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        self.queries.append(copy.deepcopy(messages))
        return super().query(messages, **kwargs)


class FailingRecordingModel(RecordingToolcallModel):
    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        self.queries.append(copy.deepcopy(messages))
        raise RuntimeError("scripted provider failure")


class RecordingValidationBackend:
    def __init__(self, *, passed: bool = True, output_chars: int = 0):
        self.passed = passed
        self.output_chars = output_chars

    def run(
        self,
        *,
        tool: Literal["pytest", "ruff"],
        arguments: list[str],
        timeout_seconds: int,
    ) -> ValidationExecution:
        outcome = "1 passed\n" if self.passed else "1 failed\n"
        return ValidationExecution(
            returncode=0 if self.passed else 1,
            output=f"{tool} validation\n{'v' * self.output_chars}\n{outcome}",
            duration_seconds=0.01,
            error_code=None if self.passed else "validation_failed",
            metadata={"image_id": "sha256:scripted", "network_mode": "none"},
        )


def build_recording_agent(
    repository: Path,
    actions: list[dict],
    *,
    backend: RecordingValidationBackend,
    **config,
) -> tuple[FastFixRepairAgent, RecordingToolcallModel]:
    environment = FastFixRepairEnvironment(
        registry=build_repair_registry(repository, validation_backend=backend),
        workspace=repository,
    )
    model = RecordingToolcallModel(outputs=outputs(actions), cost_per_call=0)
    return (
        FastFixRepairAgent(
            model,
            environment,
            system_template="Use structured repair tools.",
            instance_template="{{ task }}",
            cost_limit=0,
            **config,
        ),
        model,
    )


def test_agent_completes_structured_validation_loop(tmp_path: Path) -> None:
    actions = [
        action("show_tree", {}, 1),
        action("read_file", {"path": "app/main.py"}, 2),
        action("apply_patch", {"patch": patch()}, 3),
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_value"]},
            4,
        ),
        action("run_pytest", {"scope": "regression"}, 5),
        action("run_ruff", {}, 6),
        action("show_git_diff", {}, 7),
        action("submit_repair", submission(), 8),
    ]
    agent = build_agent(tmp_path, actions)
    result = agent.run("repair")
    trajectory = agent.serialize()
    assert result["exit_status"] == "Submitted"
    assert json.loads(result["submission"])["changed_files"] == ["app/main.py"]
    assert trajectory["info"]["fastfix"] == {
        "mode": "structured_repair",
        "shell_enabled": False,
        "validation_gate_enabled": True,
        "context_projection_enabled": True,
    }
    assert trajectory["info"]["repair_state"]["phase"] == "submitted"
    assert "bash" not in agent.env.tool_names
    assert [call["tool_name"] for call in agent.env.tool_call_history] == [item["tool"] for item in actions]
    assert sum(message.get("role") == "tool" for message in trajectory["messages"]) == 7


def test_agent_cannot_submit_without_validation(tmp_path: Path) -> None:
    agent = build_agent(
        tmp_path,
        [
            action("apply_patch", {"patch": patch()}, 1),
            action("submit_repair", submission(), 2),
        ],
        step_limit=2,
    )
    assert agent.run("repair")["exit_status"] == "LimitsExceeded"
    assert agent.env.tool_call_history[-1]["error_code"] == "validation_incomplete"
    FastFixRepairAgent._validate_tool_protocol(agent.messages)


def replace_actions(*, include_patch_failures: bool = False) -> list[dict]:
    actions = [action("read_file", {"path": "app/main.py"}, 1)]
    if include_patch_failures:
        actions.extend(action("apply_patch", {"patch": "invalid patch"}, number) for number in range(2, 5))
    number = len(actions) + 1
    actions.extend(
        [
            action(
                "replace_text",
                {
                    "path": "app/main.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                },
                number,
            ),
            action(
                "run_pytest",
                {"scope": "targeted", "targets": ["tests/test_main.py::test_value"]},
                number + 1,
            ),
            action("run_pytest", {"scope": "regression"}, number + 2),
            action("run_ruff", {}, number + 3),
            action("show_git_diff", {}, number + 4),
            action("submit_repair", submission(), number + 5),
        ]
    )
    return actions


def test_agent_repairs_with_exact_replacement(tmp_path: Path) -> None:
    actions = replace_actions()
    agent = build_agent(tmp_path, actions)
    assert agent.run("repair")["exit_status"] == "Submitted"
    assert [call["tool_name"] for call in agent.env.tool_call_history] == [item["tool"] for item in actions]
    assert agent.env.repair_state.patch_count == 1
    assert "bash" not in agent.env.tool_names


def test_agent_recovers_from_three_patch_failures_with_replace(tmp_path: Path) -> None:
    actions = replace_actions(include_patch_failures=True)
    agent = build_agent(tmp_path, actions)
    assert agent.run("repair")["exit_status"] == "Submitted"
    assert agent.env.repair_state.total_patch_failures == 3
    assert agent.env.repair_state.patch_count == 1
    assert agent.env.tool_call_history[3]["error_code"] == "patch_invalid"


def test_ready_to_submit_prevents_post_validation_edit(tmp_path: Path) -> None:
    actions = [
        action("read_file", {"path": "app/main.py"}, 1),
        action(
            "replace_text",
            {"path": "app/main.py", "old_text": "value = 1", "new_text": "value = 2"},
            2,
        ),
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_value"]},
            3,
        ),
        action("run_pytest", {"scope": "regression"}, 4),
        action("run_ruff", {}, 5),
        action(
            "replace_text",
            {"path": "app/main.py", "old_text": "value = 2", "new_text": "value = 3"},
            6,
        ),
        action("show_git_diff", {}, 7),
        action("submit_repair", submission(), 8),
    ]
    agent = build_agent(tmp_path, actions)
    assert agent.run("repair")["exit_status"] == "Submitted"
    blocked = agent.env.tool_call_history[5]
    assert blocked["tool_name"] == "replace_text"
    assert blocked["error_code"] == "repair_ready_locked"
    assert agent.env.repair_state.revision == 1
    assert agent.env.repair_state.ready_to_submit
    assert (tmp_path / "app" / "main.py").read_text(encoding="utf-8") == "value = 2\n"
    assert agent.env.tool_call_history[-2]["tool_name"] == "show_git_diff"
    assert agent.env.tool_call_history[-1]["tool_name"] == "submit_repair"


def test_projection_accepts_ordered_multiple_tool_results_without_mutating_history(tmp_path: Path) -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1"}, {"id": "call-2"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "first failed"},
        {"role": "tool", "tool_call_id": "call-2", "content": "second locked"},
        {"role": "user", "content": "continue"},
    ]
    agent = build_agent(tmp_path, [])
    agent.messages = copy.deepcopy(messages)
    projected, _ = agent.project_messages()
    FastFixRepairAgent._validate_tool_protocol(projected)
    assert projected == messages and agent.messages == messages


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "tool", "tool_call_id": "orphan", "content": "orphan"}],
        [
            {"role": "assistant", "tool_calls": [{"id": "1"}, {"id": "2"}]},
            {"role": "tool", "tool_call_id": "2", "content": "out of order"},
        ],
        [{"role": "assistant", "tool_calls": [{"id": "missing"}]}],
        [
            {"role": "assistant", "tool_calls": [{"id": "duplicate"}, {"id": "duplicate"}]},
            {"role": "tool", "tool_call_id": "duplicate", "content": "first"},
            {"role": "tool", "tool_call_id": "duplicate", "content": "second"},
        ],
    ],
)
def test_projection_rejects_invalid_tool_protocol(messages: list[dict]) -> None:
    with pytest.raises(ValueError):
        FastFixRepairAgent._validate_tool_protocol(messages)


def test_provider_exception_preserves_auditable_trajectory_and_protocol(tmp_path: Path) -> None:
    repository = workspace(tmp_path)
    environment = FastFixRepairEnvironment(
        registry=build_repair_registry(repository, python_executable=Path(sys.executable)),
        workspace=repository,
    )
    model = FailingRecordingModel(outputs=[], cost_per_call=0)
    agent = FastFixRepairAgent(
        model,
        environment,
        system_template="Use structured repair tools.",
        instance_template="{{ task }}",
        cost_limit=0,
    )
    with pytest.raises(RuntimeError, match="scripted provider failure"):
        agent.run("repair")
    FastFixRepairAgent._validate_tool_protocol(model.queries[0])
    assert [message["role"] for message in agent.messages] == ["system", "user", "exit"]
    assert agent.serialize()["messages"] == agent.messages
    assert agent.serialize()["info"]["context_projection"]["model_call_count"] == 1


def test_required_context_over_limit_is_retained_and_counted(tmp_path: Path) -> None:
    repository = workspace(tmp_path)
    model = RecordingToolcallModel(outputs=outputs([action("show_tree", {}, 1)]), cost_per_call=0)
    agent = FastFixRepairAgent(
        model,
        FastFixRepairEnvironment(
            registry=build_repair_registry(repository, python_executable=Path(sys.executable)),
            workspace=repository,
        ),
        system_template="Use structured repair tools.",
        instance_template="{{ task }}",
        cost_limit=0,
        context_max_chars=10_000,
    )
    agent.messages = [{"role": "system", "content": "s" * 12_000}, {"role": "user", "content": "repair"}]
    agent.query()
    projection = agent.serialize()["info"]["context_projection"]
    assert model.queries[0][0]["content"] == "s" * 12_000
    assert projection["max_projected_chars_per_call"] > projection["configured_projection_limit"]
    assert projection["calls_exceeding_projection_limit"] == 1


def test_context_projection_compacts_stale_outputs_and_preserves_required_context(tmp_path: Path) -> None:
    repository = workspace(tmp_path)
    (repository / "app" / "main.py").write_text(
        "value = 1\n" + "\n".join(f"line_{number} = '{'x' * 120}'" for number in range(250)) + "\n",
        encoding="utf-8",
    )
    git(repository, "add", "app/main.py")
    git(repository, "commit", "--amend", "--no-edit", "-q")
    actions = [
        action("read_file", {"path": "app/main.py", "max_lines": 300}, 1),
        action("search_code", {"query": "line_", "path": "app", "max_results": 100}, 2),
        action(
            "replace_text",
            {"path": "app/main.py", "old_text": "value = 1", "new_text": "value = 2"},
            3,
        ),
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_value"]},
            4,
        ),
        action("run_pytest", {"scope": "regression"}, 5),
        action("run_ruff", {}, 6),
        action("reopen_repair", {"reason": "A second scripted edit is required."}, 7),
        action(
            "replace_text",
            {"path": "app/main.py", "old_text": "value = 2", "new_text": "value = 3"},
            8,
        ),
        action("read_file", {"path": "app/main.py", "max_lines": 300}, 9),
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_value"]},
            10,
        ),
        action("run_pytest", {"scope": "regression"}, 11),
        action("run_ruff", {}, 12),
        action("show_git_diff", {}, 13),
        action("submit_repair", submission(), 14),
    ]
    agent, model = build_recording_agent(
        repository,
        actions,
        backend=RecordingValidationBackend(output_chars=8_000),
        context_recent_rounds=4,
        context_max_chars=80_000,
    )
    assert agent.run("repair")["exit_status"] == "Submitted"
    final_context = json.dumps(model.queries[-1], ensure_ascii=False)
    final_content = "\n".join(str(message.get("content", "")) for message in model.queries[-1])
    after_reopen_context = json.dumps(model.queries[7], ensure_ascii=False)
    trajectory_before = copy.deepcopy(agent.messages)
    agent.project_messages()
    assert agent.messages == trajectory_before
    assert "omitted old read_file output" in final_context
    assert "omitted old tool output; tool=search_code" in final_context
    assert "stale/omitted validation output" in final_context
    assert "stale/omitted validation output" in after_reopen_context
    assert "pytest validation" not in after_reopen_context
    assert "ruff validation" not in after_reopen_context
    assert '"revision":2' in final_content
    assert "value = 3" in final_context
    assert "diff --git a/app/main.py b/app/main.py" in final_context
    assert "pytest validation" in final_context and "ruff validation" in final_context
    projection = agent.serialize()["info"]["context_projection"]
    assert projection["compacted_message_count"] > 0
    assert projection["projected_chars"] < projection["raw_chars"]
    assert projection["reduction_ratio"] > 0
    assert projection["model_call_count"] == len(model.queries)
    assert projection["max_raw_chars_per_call"] == max(call["raw_chars"] for call in projection["calls"])
    assert projection["max_projected_chars_per_call"] == max(call["projected_chars"] for call in projection["calls"])
    assert projection["average_raw_chars_per_call"] == round(
        projection["raw_chars"] / projection["model_call_count"], 6
    )
    assert projection["average_projected_chars_per_call"] == round(
        projection["projected_chars"] / projection["model_call_count"], 6
    )
    assert projection["configured_projection_limit"] == 80_000
    assert projection["calls_exceeding_projection_limit"] == sum(
        call["projected_chars"] > 80_000 for call in projection["calls"]
    )


def test_validation_failure_remains_in_next_active_context(tmp_path: Path) -> None:
    repository = workspace(tmp_path)
    actions = [
        action("apply_patch", {"patch": patch()}, 1),
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_value"]},
            2,
        ),
        action("read_file", {"path": "app/main.py"}, 3),
    ]
    agent, model = build_recording_agent(
        repository,
        actions,
        backend=RecordingValidationBackend(passed=False, output_chars=5_000),
        step_limit=3,
    )
    assert agent.run("repair")["exit_status"] == "LimitsExceeded"
    next_context = json.dumps(model.queries[2], ensure_ascii=False)
    next_content = "\n".join(str(message.get("content", "")) for message in model.queries[2])
    assert "validation_failed" in next_context
    assert '"status":"current"' in next_content
    assert '"passed":false' in next_content
    assert all(FastFixRepairAgent._validate_tool_protocol(query) is None for query in model.queries)

import copy
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import typer

from fastfix.agents.repair import FastFixRepairAgent
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from fastfix.repair.state import RepairSessionState, valid_repair_actions
from fastfix.sandbox import ValidationExecution
from fastfix.tools.repair import build_repair_registry
from minisweagent.models.test_models import DeterministicToolcallModel

ROOT = Path(__file__).resolve().parents[2]
DESTINATION = ROOT / "benchmarks" / "results"


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    ).stdout


def repository(root: Path, *, large: bool = False) -> Path:
    root.mkdir(parents=True)
    (root / "app").mkdir()
    content = "value = 1\n"
    if large:
        content += "\n".join(f"line_{number} = '{'x' * 120}'" for number in range(250)) + "\n"
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "main.py").write_text(content, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text(
        "from app.main import value\n\ndef test_value():\n    assert value >= 2\n",
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "FastFix Scripted Evaluation"),
        ("config", "user.email", "fastfix@example.invalid"),
        ("config", "core.autocrlf", "false"),
        ("add", "."),
        ("commit", "-q", "-m", "baseline"),
    ):
        git(root, *arguments)
    return root


class RecordingValidationBackend:
    def __init__(self, *, passed: bool = True, output_chars: int = 0):
        self.passed = passed
        self.output_chars = output_chars
        self.calls = 0

    def run(
        self,
        *,
        tool: Literal["pytest", "ruff"],
        arguments: list[str],
        timeout_seconds: int,
    ) -> ValidationExecution:
        self.calls += 1
        outcome = "1 passed\n" if self.passed else "1 failed\n"
        return ValidationExecution(
            returncode=0 if self.passed else 1,
            output=(f"scripted_validation_call_{self.calls}_{tool}\n{'v' * self.output_chars}\n{outcome}"),
            duration_seconds=0.01,
            error_code=None if self.passed else "validation_failed",
            metadata={"image_id": "sha256:scripted", "network_mode": "none"},
        )


class RecordingToolcallModel(DeterministicToolcallModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queries: list[list[dict]] = []

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        self.queries.append(copy.deepcopy(messages))
        return super().query(messages, **kwargs)

    def format_observation_messages(
        self,
        message: dict,
        outputs: list[dict],
        template_vars: dict | None = None,
    ) -> list[dict]:
        messages = super().format_observation_messages(message, outputs, template_vars)
        for observation in messages:
            observation["extra"]["timestamp"] = 0.0
        return messages


def action(tool: str, arguments: dict, number: int) -> dict:
    return {"tool": tool, "arguments": arguments, "tool_call_id": str(number)}


def outputs(actions: list[dict]) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": item["tool_call_id"]}],
            "extra": {"actions": [item], "cost": 0.0},
        }
        for item in actions
    ]


def submission() -> dict:
    return {
        "summary": "Correct the scripted value.",
        "root_cause": "The source value was incorrect.",
        "changed_files": ["app/main.py"],
        "tests_run": ["targeted", "regression", "ruff"],
        "risk_notes": [],
        "confidence": 1.0,
    }


def edit(old: int, new: int, number: int) -> dict:
    return action(
        "replace_text",
        {
            "path": "app/main.py",
            "old_text": f"value = {old}",
            "new_text": f"value = {new}",
        },
        number,
    )


def validations(start: int) -> list[dict]:
    return [
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_value"]},
            start,
        ),
        action("run_pytest", {"scope": "regression"}, start + 1),
        action("run_ruff", {}, start + 2),
    ]


def run_agent(
    root: Path,
    actions: list[dict],
    *,
    backend: RecordingValidationBackend,
    large: bool = False,
    **config,
) -> tuple[FastFixRepairAgent, RecordingToolcallModel, dict]:
    workspace = repository(root, large=large)
    environment = FastFixRepairEnvironment(
        registry=build_repair_registry(workspace, validation_backend=backend),
        workspace=workspace,
    )
    model = RecordingToolcallModel(outputs=outputs(actions), cost_per_call=0)
    agent = FastFixRepairAgent(
        model,
        environment,
        system_template="Use structured repair tools.",
        instance_template="{{ task }}",
        step_limit=len(actions),
        cost_limit=0,
        **config,
    )
    return agent, model, agent.run("scripted post-freeze mechanism evaluation")


def tool_pairs_are_valid(messages: list[dict]) -> bool:
    try:
        FastFixRepairAgent._validate_tool_protocol(messages)
    except ValueError:
        return False
    return True


def state_cards(agent: FastFixRepairAgent) -> list[dict]:
    return [
        message["extra"]["state_card"]
        for message in agent.messages
        if message.get("role") == "tool" and "state_card" in message.get("extra", {})
    ]


def state_card_policy_matches(card: dict, tool_names: tuple[str, ...]) -> bool:
    return card["valid_actions"] == list(
        valid_repair_actions(
            RepairSessionState(phase=card["phase"]),
            tool_names,
        )
    )


def evaluate(root: Path) -> dict:
    lock_actions = [
        edit(1, 2, 1),
        *validations(2),
        edit(2, 3, 5),
        action("apply_patch", {"patch": "invalid patch"}, 6),
        action("show_git_diff", {}, 7),
        action("submit_repair", submission(), 8),
    ]
    lock_agent, _, lock_result = run_agent(
        root / "ready-lock",
        lock_actions,
        backend=RecordingValidationBackend(),
    )
    illegal_calls = [
        call for call in lock_agent.env.tool_call_history if call["tool_name"] in {"replace_text", "apply_patch"}
    ][1:]
    blocked_calls = [call for call in illegal_calls if call["error_code"] == "repair_ready_locked"]
    lock_scenario = {
        "id": "ff008_root_cause_scripted_reproduction",
        "outcome": "submitted_after_post_validation_edit_was_locked",
        "submitted": lock_result["exit_status"] == "Submitted",
        "revision": lock_agent.env.repair_state.revision,
        "candidate_content": (lock_agent.env.workspace / "app" / "main.py").read_text(encoding="utf-8"),
        "validation_current": all(
            item["status"] == "current" and item["passed"] for item in lock_agent.env.state_card["validation"].values()
        ),
        "blocked_error_codes": [call["error_code"] for call in blocked_calls],
        "candidate_diff_unchanged_after_blocks": (
            lock_agent.env.repair_state.revision == 1
            and (lock_agent.env.workspace / "app" / "main.py").read_text(encoding="utf-8") == "value = 2\n"
        ),
        "show_diff_succeeded": lock_agent.env.tool_call_history[-2]["tool_name"] == "show_git_diff"
        and lock_agent.env.tool_call_history[-2]["ok"],
    }

    reopen_actions = [
        edit(1, 2, 1),
        *validations(2),
        action("reopen_repair", {"reason": "Scripted review requires another edit."}, 5),
        edit(2, 3, 6),
        action("submit_repair", submission(), 7),
        *validations(8),
        action("show_git_diff", {}, 11),
        action("submit_repair", submission(), 12),
    ]
    reopen_agent, _, reopen_result = run_agent(
        root / "reopen",
        reopen_actions,
        backend=RecordingValidationBackend(),
    )
    reopen_scenario = {
        "id": "reopen_invalidates_validation",
        "outcome": "revalidated_and_submitted",
        "submitted": reopen_result["exit_status"] == "Submitted",
        "reopen_count": reopen_agent.env.repair_state.reopen_count,
        "revision": reopen_agent.env.repair_state.revision,
        "premature_submit_error": reopen_agent.env.tool_call_history[6]["error_code"],
        "candidate_content": (reopen_agent.env.workspace / "app" / "main.py").read_text(encoding="utf-8"),
    }

    failure_actions = [
        edit(1, 2, 1),
        action(
            "run_pytest",
            {"scope": "targeted", "targets": ["tests/test_main.py::test_value"]},
            2,
        ),
        action("read_file", {"path": "app/main.py"}, 3),
    ]
    failure_agent, failure_model, failure_result = run_agent(
        root / "validation-failure",
        failure_actions,
        backend=RecordingValidationBackend(passed=False, output_chars=5_000),
    )
    failure_context = json.dumps(failure_model.queries[-1], ensure_ascii=False)
    failure_scenario = {
        "id": "validation_failure_retention",
        "outcome": "failure_retained_until_script_limit",
        "exit_status": failure_result["exit_status"],
        "state_card_failed_current": (
            failure_agent.env.state_card["validation"]["targeted"]["status"] == "current"
            and not failure_agent.env.state_card["validation"]["targeted"]["passed"]
        ),
        "active_context_retained_failure": "validation_failed" in failure_context,
    }

    projection_actions = [
        action("read_file", {"path": "app/main.py", "max_lines": 300}, 1),
        action("search_code", {"query": "line_", "path": "app", "max_results": 100}, 2),
        edit(1, 2, 3),
        *validations(4),
        action("reopen_repair", {"reason": "Create a second revision for stale checks."}, 7),
        edit(2, 3, 8),
        action("read_file", {"path": "app/main.py", "max_lines": 300}, 9),
        *validations(10),
        action("show_git_diff", {}, 13),
        action("submit_repair", submission(), 14),
    ]
    projection_backend = RecordingValidationBackend(output_chars=8_000)
    projection_agent, projection_model, projection_result = run_agent(
        root / "projection",
        projection_actions,
        backend=projection_backend,
        large=True,
        context_recent_rounds=4,
        context_max_chars=80_000,
    )
    final_content = "\n".join(str(message.get("content", "")) for message in projection_model.queries[-1])
    trajectory_before = copy.deepcopy(projection_agent.messages)
    projection_agent.project_messages()
    current_validation_tokens = (
        "scripted_validation_call_4_pytest",
        "scripted_validation_call_5_pytest",
        "scripted_validation_call_6_ruff",
    )
    stale_validation_tokens = (
        "scripted_validation_call_1_pytest",
        "scripted_validation_call_2_pytest",
        "scripted_validation_call_3_ruff",
    )
    required_context_checks = [
        projection_model.queries[-1][0]["content"] == "Use structured repair tools.",
        projection_model.queries[-1][1]["content"] == "scripted post-freeze mechanism evaluation",
        "[FastFix Repair State]" in final_content,
        '"revision":2' in final_content,
        all(token in final_content for token in current_validation_tokens),
        "diff --git a/app/main.py b/app/main.py" in final_content,
        "value = 3" in final_content,
        "line_249" in final_content,
        "omitted old read_file output" in final_content,
        "omitted old tool output; tool=search_code" in final_content,
        "stale/omitted validation output" in final_content,
        projection_agent.messages == trajectory_before,
        all(tool_pairs_are_valid(query) for query in projection_model.queries),
    ]
    projection_summary = projection_agent.serialize()["info"]["context_projection"]
    projection_scenario = {
        "id": "deterministic_context_projection",
        "outcome": "submitted_with_protocol_valid_projected_context",
        "submitted": projection_result["exit_status"] == "Submitted",
        "compacted_message_count": projection_summary["compacted_message_count"],
        "trajectory_unchanged": projection_agent.messages == trajectory_before,
        "tool_pairs_valid": all(tool_pairs_are_valid(query) for query in projection_model.queries),
    }

    lock_cards = state_cards(lock_agent)
    ready_card = next(card for card in lock_cards if card["phase"] == "ready_to_submit")
    reopen_card = next(card for card in state_cards(reopen_agent) if card["reopen_count"] == 1)
    failure_card = state_cards(failure_agent)[-1]
    projection_card = state_cards(projection_agent)[-1]
    state_policy_checks = [
        state_card_policy_matches(lock_cards[0], lock_agent.env.tool_names),
        state_card_policy_matches(ready_card, lock_agent.env.tool_names),
        set(ready_card["valid_actions"]) == {"show_git_diff", "submit_repair", "rollback_changes", "reopen_repair"},
        all(call["tool_name"] not in ready_card["valid_actions"] for call in blocked_calls),
        state_card_policy_matches(reopen_card, reopen_agent.env.tool_names),
        state_card_policy_matches(failure_card, failure_agent.env.tool_names)
        and state_card_policy_matches(projection_card, projection_agent.env.tool_names),
    ]
    stale_validation_exposure_count = sum(token in final_content for token in stale_validation_tokens)
    illegal_attempts = len(illegal_calls)
    illegal_blocks = len(blocked_calls)
    state_policy_passes = sum(state_policy_checks)
    context_retention_passes = sum(required_context_checks)
    scenarios = [
        lock_scenario,
        reopen_scenario,
        failure_scenario,
        projection_scenario,
    ]
    return {
        "scenarios": scenarios,
        "metrics": {
            "scripted_scenarios": len(scenarios),
            "ready_state_illegal_action_attempts": illegal_attempts,
            "ready_state_illegal_action_blocks": illegal_blocks,
            "ready_state_illegal_action_block_rate": round(illegal_blocks / illegal_attempts, 6),
            "stale_validation_exposure_count": stale_validation_exposure_count,
            "state_card_policy_checks": len(state_policy_checks),
            "state_card_policy_consistency_rate": round(state_policy_passes / len(state_policy_checks), 6),
            "required_context_retention_checks": len(required_context_checks),
            "required_context_retention_rate": round(
                context_retention_passes / len(required_context_checks),
                6,
            ),
            "raw_context_chars": projection_summary["raw_chars"],
            "projected_context_chars": projection_summary["projected_chars"],
            "context_reduction_ratio": projection_summary["reduction_ratio"],
            "context_character_scope": projection_summary["scope"],
            "model_call_count": projection_summary["model_call_count"],
            "max_raw_chars_per_call": projection_summary["max_raw_chars_per_call"],
            "max_projected_chars_per_call": projection_summary["max_projected_chars_per_call"],
            "average_raw_chars_per_call": projection_summary["average_raw_chars_per_call"],
            "average_projected_chars_per_call": projection_summary["average_projected_chars_per_call"],
            "configured_projection_limit": projection_summary["configured_projection_limit"],
            "calls_exceeding_projection_limit": projection_summary["calls_exceeding_projection_limit"],
        },
    }


def build_assessment(root: Path = ROOT) -> dict:
    with tempfile.TemporaryDirectory(prefix="fastfix-post-freeze-") as temporary:
        evaluation = evaluate(Path(temporary))
    return {
        "schema_version": 1,
        "evaluation_role": "post_freeze_scripted_mechanism_evaluation",
        "formal_benchmark": False,
        "metric_eligible": False,
        "provider_calls": 0,
        "frozen_run_001_replayed": False,
        "historical_tasks_rerun": False,
        "original_development_validated_candidate_rate": "11/13",
        "aggregate_denominator_merged": False,
        "source_baseline_commit": "6b404165c8d8d7a8808af1ae3133a927717ac432",
        "generator": "benchmarks/scripts/build_post_freeze_mechanism_assessment.py",
        **evaluation,
    }


def render_markdown(assessment: dict) -> str:
    metrics = assessment["metrics"]
    scenarios = "\n".join(f"- `{scenario['id']}`: {scenario['outcome']}" for scenario in assessment["scenarios"])
    return f"""# Post-freeze scripted mechanism assessment

This is a deterministic, Provider-free mechanism evaluation. It is not a formal benchmark, does not replay
any frozen `run-001`, and is not merged into the original 11/13 development denominator.

## Scope

- `evaluation_role`: `{assessment["evaluation_role"]}`
- `formal_benchmark`: `{str(assessment["formal_benchmark"]).lower()}`
- `metric_eligible`: `{str(assessment["metric_eligible"]).lower()}`
- `provider_calls`: `{assessment["provider_calls"]}`
- `frozen_run_001_replayed`: `{str(assessment["frozen_run_001_replayed"]).lower()}`
- Original development validated Candidate rate: `{assessment["original_development_validated_candidate_rate"]}`

## Scripted scenarios

{scenarios}

The FF-008 scenario reproduces only the post-validation closure-failure mechanism. It does not rerun or
reclassify the frozen FF-008 attempt and does not claim that FF-008 was solved again.

## Computed metrics

| Metric | Value |
|---|---:|
| Scripted scenarios | {metrics["scripted_scenarios"]} |
| Ready-state illegal action attempts | {metrics["ready_state_illegal_action_attempts"]} |
| Ready-state illegal action blocks | {metrics["ready_state_illegal_action_blocks"]} |
| Ready-state illegal action block rate | {metrics["ready_state_illegal_action_block_rate"]:.6f} |
| Stale validation exposure count | {metrics["stale_validation_exposure_count"]} |
| State-card policy checks | {metrics["state_card_policy_checks"]} |
| State-card policy consistency rate | {metrics["state_card_policy_consistency_rate"]:.6f} |
| Required-context retention checks | {metrics["required_context_retention_checks"]} |
| Required-context retention rate | {metrics["required_context_retention_rate"]:.6f} |
| Model calls | {metrics["model_call_count"]} |
| Cumulative raw model-visible characters | {metrics["raw_context_chars"]} |
| Cumulative projected model-visible characters | {metrics["projected_context_chars"]} |
| Cumulative character reduction ratio | {metrics["context_reduction_ratio"]:.6f} |
| Maximum raw characters per call | {metrics["max_raw_chars_per_call"]} |
| Maximum projected characters per call | {metrics["max_projected_chars_per_call"]} |
| Average raw characters per call | {metrics["average_raw_chars_per_call"]:.6f} |
| Average projected characters per call | {metrics["average_projected_chars_per_call"]:.6f} |
| Configured projection limit per call | {metrics["configured_projection_limit"]} |
| Calls exceeding projection limit | {metrics["calls_exceeding_projection_limit"]} |

Character counts are deterministic JSON-serialized message sizes. Cumulative totals sum model-visible context
across all calls; they are not a single prompt, token counts, or evidence of Provider cost reduction. Required
evidence remains visible even when a projected call exceeds the configured limit, and such calls are counted.
"""


def write_outputs(destination: Path = DESTINATION, root: Path = ROOT) -> tuple[Path, Path]:
    assessment = build_assessment(root)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "post-freeze-mechanism-assessment.json"
    markdown_path = destination / "post-freeze-mechanism-assessment.md"
    json_path.write_text(
        json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown_path.write_text(render_markdown(assessment), encoding="utf-8", newline="\n")
    return json_path, markdown_path


app = typer.Typer(add_completion=False)


@app.command()
def main(
    destination: Path = typer.Option(DESTINATION),
) -> None:
    for path in write_outputs(destination):
        typer.echo(path)


if __name__ == "__main__":
    app()

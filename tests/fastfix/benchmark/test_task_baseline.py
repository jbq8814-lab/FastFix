import argparse
import json
import shutil
from pathlib import Path

import pytest

from benchmarks.scripts import run_task_baseline as generic
from benchmarks.scripts.run_fastfix_secure import PreflightResult, SecureRunnerError
from benchmarks.scripts.run_ff003_baseline import (
    PROTOCOL_PATH,
    BaselineSettings,
)
from benchmarks.scripts.run_ff003_baseline import (
    run_baseline as run_ff003,
)
from fastfix.agents.repair import FastFixRepairAgent
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output
from tests.fastfix.benchmark.test_ff003_baseline import (
    dependencies,
    result_text,
    scripted_agent,
)
from tests.fastfix.workflows.test_secure_repair import tool_action


@pytest.fixture(autouse=True)
def isolate_runner_flow_from_historical_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generic, "verify_snapshots", lambda protocol: {"task": (), "system": ()})


def settings(tmp_path: Path, name: str) -> BaselineSettings:
    return BaselineSettings(
        model_name="openai/scripted",
        image="fastfix-validation:ff001-v1",
        runtime_root=tmp_path / name / "runtime",
        results_root=tmp_path / name / "results",
    )


def test_generic_runner_matches_ff003_wrapper_and_redacts_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generic_settings = settings(tmp_path, "generic")
    wrapper_settings = settings(tmp_path, "wrapper")
    generic_calls: list[tuple[str, list[str]]] = []
    wrapper_calls: list[tuple[str, list[str]]] = []
    secret = "generic-baseline-secret-value"
    leak = f"{secret} {tmp_path}"
    monkeypatch.setenv("TASK_BASELINE_TEST_API_KEY", secret)

    generic_code, generic_summary = generic.run_baseline(
        generic_settings,
        protocol_path=PROTOCOL_PATH,
        dependencies=dependencies(
            generic_calls,
            lambda environment, model, cost: scripted_agent(environment, leak=leak),
        ),
    )
    wrapper_code, wrapper_summary = run_ff003(
        wrapper_settings,
        dependencies=dependencies(
            wrapper_calls,
            lambda environment, model, cost: scripted_agent(environment, leak=leak),
        ),
    )

    assert generic_code == wrapper_code == 0
    core = {
        "task_id",
        "run_id",
        "system_revision",
        "exit_status",
        "submitted",
        "approval_pending",
        "resolved_candidate",
        "tool_sequence",
        "unique_files_read",
        "targeted_tests_passed",
        "regression_tests_passed",
        "ruff_passed",
        "changed_files",
        "source_unchanged",
        "canonical_fixture_unchanged",
    }
    assert {key: generic_summary[key] for key in core} == {key: wrapper_summary[key] for key in core}
    assert generic_summary["resolved_candidate"]
    assert generic_summary["changed_files"] == ["app/schemas.py"]
    assert generic_calls == wrapper_calls
    text = result_text(generic_settings.results_root)
    assert secret not in text
    assert str(tmp_path) not in text


def test_generic_expected_file_and_unique_attempt_rules(tmp_path: Path) -> None:
    wrong_settings = settings(tmp_path, "wrong")
    wrong_calls: list[tuple[str, list[str]]] = []
    wrong_code, wrong = generic.run_baseline(
        wrong_settings,
        protocol_path=PROTOCOL_PATH,
        dependencies=dependencies(
            wrong_calls,
            lambda environment, model, cost: scripted_agent(environment, path="app/main.py"),
        ),
    )
    assert wrong_code == 0
    assert wrong["approval_pending"] and not wrong["resolved_candidate"]

    failed_settings = settings(tmp_path, "failed")
    failed_calls: list[tuple[str, list[str]]] = []

    def failed_agent(environment, model, cost):
        action = tool_action("read_file", {"path": "app/schemas.py"}, 1)
        return FastFixRepairAgent(
            DeterministicToolcallModel(
                outputs=[make_toolcall_output(None, [{"id": "1"}], [action])],
                cost_per_call=0,
            ),
            environment,
            system_template=generic.agent_config(generic.load_context(PROTOCOL_PATH))["agent"]["system_template"],
            instance_template="{{ task }} {{ issue }} {{ failing_test }} {{ allowed_source_paths }}",
            step_limit=1,
            cost_limit=0,
        )

    failed_dependencies = dependencies(failed_calls, failed_agent)
    failed_code, failed = generic.run_baseline(
        failed_settings,
        protocol_path=PROTOCOL_PATH,
        dependencies=failed_dependencies,
    )
    assert failed_code == 1 and not failed["approval_pending"]
    with pytest.raises(SecureRunnerError) as error:
        generic.run_baseline(
            failed_settings,
            protocol_path=PROTOCOL_PATH,
            dependencies=failed_dependencies,
        )
    assert error.value.code == "run_already_attempted"


def test_generic_preflight_failure_preserves_consumed_lease(tmp_path: Path) -> None:
    configured = settings(tmp_path, "preflight")
    deps = dependencies([], lambda environment, model, cost: scripted_agent(environment))

    def failed_preflight(model: str, image: str, output: Path) -> PreflightResult:
        raise SecureRunnerError("api_key_missing", "missing")

    deps.preflight = failed_preflight
    with pytest.raises(SecureRunnerError) as error:
        generic.run_baseline(configured, protocol_path=PROTOCOL_PATH, dependencies=deps)
    assert error.value.code == "api_key_missing"
    assert not (configured.results_root / "run-001").exists()
    assert not list(configured.runtime_root.glob("sessions/*/attempt.json"))
    assert next(configured.runtime_root.glob("attempt-leases/*/run-001/lease.json")).is_file()


def fake_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    shutil.copytree(
        generic.ROOT / "benchmarks" / "tasks" / "ff-003-response-model-field-mismatch",
        root / "benchmarks" / "tasks" / "ff-003-response-model-field-mismatch",
    )
    shutil.copytree(
        generic.ROOT / "benchmarks" / "fixture_repos" / "ff-003-response-model-field-mismatch",
        root / "benchmarks" / "fixture_repos" / "ff-003-response-model-field-mismatch",
    )
    protocol = root / "benchmarks" / "experiments" / "case" / "protocol.json"
    protocol.parent.mkdir(parents=True)
    protocol.write_bytes(PROTOCOL_PATH.read_bytes())
    return root, protocol


def test_protocol_and_path_validation_are_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SecureRunnerError) as outside:
        generic.load_context(tmp_path / "protocol.json")
    assert outside.value.code == "protocol_path_invalid"

    root, protocol_path = fake_project(tmp_path)
    monkeypatch.setattr(generic, "ROOT", root)
    value = json.loads(protocol_path.read_text(encoding="utf-8"))
    value.pop("validation_commands")
    protocol_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SecureRunnerError) as missing:
        generic.load_context(protocol_path)
    assert missing.value.code == "protocol_missing_field"
    assert generic.main(["--protocol", str(protocol_path), "preflight"]) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "protocol_missing_field"}

    value = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    value["validation_commands"]["ruff"] = ["ruff", "check", ".."]
    protocol_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SecureRunnerError) as command:
        generic.load_context(protocol_path)
    assert command.value.code == "validation_command_invalid"
    assert generic.main(["--protocol", str(protocol_path), "preflight"]) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "validation_command_invalid"}

    value = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol_path.write_text(json.dumps(value), encoding="utf-8")
    task_path = root / "benchmarks" / "tasks" / value["task_id"] / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["fixture_repo"] = "../outside"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(SecureRunnerError) as fixture:
        generic.load_context(protocol_path)
    assert fixture.value.code == "fixture_path_invalid"


def test_cli_and_nested_result_guards(tmp_path: Path) -> None:
    parser = generic.build_parser()
    command = parser.parse_args(["--protocol", str(PROTOCOL_PATH), "run"])
    assert command.protocol == PROTOCOL_PATH and command.command == "run"
    assert next(
        set(action.choices) for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ) == {"preflight", "run", "inspect"}
    with pytest.raises(SystemExit):
        parser.parse_args(["run"])

    context = generic.load_context(PROTOCOL_PATH)
    configured = BaselineSettings(
        model_name="openai/scripted",
        image="fastfix-validation:ff001-v1",
        runtime_root=tmp_path / "runtime",
        results_root=context.fixture / "nested-results",
    )
    with pytest.raises(SecureRunnerError) as nested:
        generic.preflight(
            configured,
            protocol_path=PROTOCOL_PATH,
            dependencies=dependencies([], lambda environment, model, cost: scripted_agent(environment)),
        )
    assert nested.value.code == "unsafe_path"

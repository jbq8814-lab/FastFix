import argparse
import hashlib
import shutil
from pathlib import Path

import pytest

from benchmarks.scripts.run_fastfix_secure import PreflightResult, SecureRunnerError, initialize_source
from benchmarks.scripts.run_ff003_baseline import (
    FIXTURE,
    PROTOCOL_PATH,
    BaselineDependencies,
    BaselineSettings,
    agent_config,
    baseline_registry,
    build_parser,
    inspect_baseline,
    load_protocol,
    run_baseline,
)
from fastfix.agents.repair import FastFixRepairAgent
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output
from tests.fastfix.workflows.test_secure_repair import RecordingValidationBackend, tool_action


@pytest.fixture(autouse=True)
def isolate_runner_flow_from_historical_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "benchmarks.scripts.run_task_baseline.verify_snapshots",
        lambda protocol: {"task": (), "system": ()},
    )


def hashes(repository: Path) -> dict[str, str]:
    return {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repository.rglob("*")
        if path.is_file()
    }


def submission(path: str) -> dict:
    return {
        "summary": "Correct the declared response contract.",
        "root_cause": "The response model does not match the service data.",
        "changed_files": [path],
        "tests_run": ["targeted", "regression", "ruff"],
        "risk_notes": [],
        "confidence": 1.0,
    }


def repair_actions(path: str = "app/schemas.py") -> list[dict]:
    old_text = "    user_name: str" if path == "app/schemas.py" else '    return {"status": "ok"}'
    new_text = "    name: str" if path == "app/schemas.py" else '    return dict(status="ok")'
    return [
        tool_action("inspect_fastapi_routes", {"path": "."}, 1),
        tool_action("read_file", {"path": path}, 2),
        tool_action("read_file", {"path": path}, 3),
        tool_action(
            "replace_text",
            {"path": path, "old_text": old_text, "new_text": new_text},
            4,
        ),
        tool_action(
            "run_pytest",
            {
                "scope": "targeted",
                "targets": ["tests/test_users.py::test_get_user_returns_user"],
            },
            5,
        ),
        tool_action("run_pytest", {"scope": "regression"}, 6),
        tool_action("run_ruff", {}, 7),
        tool_action("show_git_diff", {}, 8),
        tool_action("submit_repair", submission(path), 9),
    ]


def scripted_agent(
    environment: FastFixRepairEnvironment,
    *,
    path: str = "app/schemas.py",
    leak: str | None = None,
) -> FastFixRepairAgent:
    actions = repair_actions(path)
    return FastFixRepairAgent(
        DeterministicToolcallModel(
            outputs=[
                make_toolcall_output(leak if index == 0 else None, [{"id": item["tool_call_id"]}], [item])
                for index, item in enumerate(actions)
            ],
            cost_per_call=0,
        ),
        environment,
        system_template=agent_config()["agent"]["system_template"],
        instance_template="{{ task }} {{ issue }} {{ failing_test }} {{ allowed_source_paths }}",
        step_limit=len(actions),
        cost_limit=0,
    )


def settings(tmp_path: Path) -> BaselineSettings:
    return BaselineSettings(
        model_name="openai/scripted",
        image="fastfix-validation:ff001-v1",
        runtime_root=tmp_path / "runtime with spaces",
        results_root=tmp_path / "results with spaces",
    )


def dependencies(
    calls: list[tuple[str, list[str]]],
    factory,
) -> BaselineDependencies:
    return BaselineDependencies(
        preflight=lambda model, image, output: PreflightResult(
            provider="openai",
            resolved_model=model,
            docker_executable="docker",
            docker_server_version="test",
            image_id="sha256:scripted",
        ),
        validation_backend_factory=lambda candidate, image: RecordingValidationBackend(calls),
        agent_factory=factory,
    )


def result_text(root: Path) -> str:
    return "".join(path.read_text(encoding="utf-8", errors="replace") for path in root.rglob("*") if path.is_file())


def command_names(parser: argparse.ArgumentParser) -> set[str]:
    return set(next(action.choices for action in parser._actions if isinstance(action, argparse._SubParsersAction)))


def test_protocol_cli_and_tools_are_frozen(tmp_path: Path) -> None:
    protocol = load_protocol()
    parser = build_parser()
    workspace = tmp_path / "repository"
    shutil.copytree(FIXTURE, workspace)
    initialize_source(workspace)
    names = baseline_registry(workspace, RecordingValidationBackend([])).names()

    assert protocol["task_commit"] == "c74e3b48888b74f122e20ef604cfcaca2c890975"
    assert protocol["system_commit"] == "e2689579f0a4781475c9086f93cffc7429aff425"
    assert protocol["maximum_iterations"] == 20
    assert protocol["include_route_inspection"]
    assert not protocol["include_pydantic_inspection"]
    assert protocol["expected_changed_files"] == ["app/schemas.py"]
    assert protocol["result_labels"] == {
        "evaluation_role": "development_unseen_baseline",
        "metric_eligible": False,
        "task_external_exposure_before_run": False,
        "task_provenance": "synthetic",
    }
    assert set(protocol["metrics"]) >= {
        "status",
        "input_tokens",
        "output_tokens",
        "elapsed_seconds",
        "tool_sequence",
        "unique_files_read",
        "provider_retry_count",
        "targeted_tests_passed",
        "regression_tests_passed",
        "ruff_passed",
        "changed_files",
        "source_unchanged",
        "canonical_fixture_unchanged",
        "instance_cost",
    }
    protocol_text = PROTOCOL_PATH.read_text(encoding="utf-8")
    assert "API_KEY" not in protocol_text
    assert str(Path.home()) not in protocol_text
    assert command_names(parser) == {"preflight", "run", "inspect"}
    assert "inspect_fastapi_routes" in names
    assert not any("pydantic" in name.casefold() for name in names)
    assert not {"shell", "bash", "python", "run_python"}.intersection(names)


def test_correct_schema_repair_stops_at_pending_and_preserves_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(tmp_path)
    calls: list[tuple[str, list[str]]] = []
    fixture_before = hashes(FIXTURE)
    secret = "ff003-test-secret-value"
    leak = f"{secret} {tmp_path}"
    monkeypatch.setenv("FF003_TEST_API_KEY", secret)
    deps = dependencies(calls, lambda environment, model, cost: scripted_agent(environment, leak=leak))

    code, summary = run_baseline(configured, dependencies=deps)

    assert code == 0
    assert summary["approval_pending"]
    assert summary["resolved_candidate"]
    assert summary["status"] == "approval_pending"
    assert summary["changed_files"] == ["app/schemas.py"]
    assert summary["targeted_tests_passed"]
    assert summary["regression_tests_passed"]
    assert summary["ruff_passed"]
    assert summary["unique_files_read"] == ["app/schemas.py"]
    assert summary["inspect_fastapi_routes_calls"] == 1
    assert summary["source_unchanged"] and summary["canonical_fixture_unchanged"]
    assert calls == [
        ("pytest", ["-q", "tests/test_users.py::test_get_user_returns_user"]),
        ("pytest", ["-q", "tests"]),
        ("ruff", ["check", "app"]),
    ]
    result = configured.results_root / "run-001"
    assert {
        "summary.json",
        "trajectory.json",
        "tool-calls.json",
        "changed-files.txt",
        "validation-summary.json",
        "approval-request.json",
        "patch.diff",
    } <= {path.name for path in result.iterdir()}
    assert not any((result / name).exists() for name in ("decision.json", "application.json", "rollback.json"))
    assert hashes(FIXTURE) == fixture_before
    text = result_text(configured.results_root)
    assert secret not in text
    assert str(tmp_path) not in text
    assert inspect_baseline(configured)["run"]["resolved_candidate"]


def test_passing_validation_with_wrong_file_is_not_resolved(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    calls: list[tuple[str, list[str]]] = []
    deps = dependencies(
        calls,
        lambda environment, model, cost: scripted_agent(environment, path="app/main.py"),
    )

    code, summary = run_baseline(configured, dependencies=deps)

    assert code == 0
    assert summary["approval_pending"]
    assert summary["targeted_tests_passed"] and summary["regression_tests_passed"] and summary["ruff_passed"]
    assert summary["changed_files"] == ["app/main.py"]
    assert not summary["resolved_candidate"]


def test_failure_after_assistant_is_frozen_and_cannot_run_twice(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    calls: list[tuple[str, list[str]]] = []

    def failed_agent(environment, model, cost):
        action = tool_action("read_file", {"path": "app/schemas.py"}, 1)
        return FastFixRepairAgent(
            DeterministicToolcallModel(
                outputs=[make_toolcall_output(None, [{"id": "1"}], [action])],
                cost_per_call=0,
            ),
            environment,
            system_template=agent_config()["agent"]["system_template"],
            instance_template="{{ task }} {{ issue }} {{ failing_test }} {{ allowed_source_paths }}",
            step_limit=1,
            cost_limit=0,
        )

    deps = dependencies(calls, failed_agent)
    code, summary = run_baseline(configured, dependencies=deps)

    assert code == 1
    assert not summary["approval_pending"]
    result = configured.results_root / "run-001"
    assert {
        "summary.json",
        "trajectory.json",
        "tool-calls.json",
        "changed-files.txt",
        "validation-summary.json",
        "failure.json",
    } <= {path.name for path in result.iterdir()}
    assert next(configured.runtime_root.glob("sessions/*/attempt.json")).is_file()
    with pytest.raises(SecureRunnerError) as error:
        run_baseline(configured, dependencies=deps)
    assert error.value.code == "run_already_attempted"


def test_preflight_failure_consumes_lease_without_creating_session_or_result(tmp_path: Path) -> None:
    configured = settings(tmp_path)

    def failed_preflight(model: str, image: str, output: Path) -> PreflightResult:
        raise SecureRunnerError("api_key_missing", "missing")

    deps = dependencies([], lambda environment, model, cost: scripted_agent(environment))
    deps.preflight = failed_preflight

    with pytest.raises(SecureRunnerError) as error:
        run_baseline(configured, dependencies=deps)

    assert error.value.code == "api_key_missing"
    assert not (configured.results_root / "run-001").exists()
    assert not list(configured.runtime_root.glob("sessions/*/attempt.json"))
    assert next(configured.runtime_root.glob("attempt-leases/*/run-001/lease.json")).is_file()
    with pytest.raises(SecureRunnerError) as repeated:
        run_baseline(configured, dependencies=deps)
    assert repeated.value.code == "run_already_attempted"

    unsafe = BaselineSettings(
        model_name=configured.model_name,
        image=configured.image,
        runtime_root=tmp_path,
        results_root=tmp_path / "results",
    )
    with pytest.raises(SecureRunnerError) as path_error:
        run_baseline(unsafe, dependencies=deps)
    assert path_error.value.code == "unsafe_path"


def test_summary_preserves_patch_failure_after_rollback(tmp_path: Path) -> None:
    configured = settings(tmp_path)

    def failed_then_rollback_agent(environment, model, cost):
        actions = [
            tool_action("apply_patch", {"patch": "invalid patch"}, 1),
            tool_action("rollback_changes", {"reason": "recover"}, 2),
        ]
        return FastFixRepairAgent(
            DeterministicToolcallModel(
                outputs=[
                    make_toolcall_output(None, [{"id": str(index)}], [item])
                    for index, item in enumerate(actions, start=1)
                ],
                cost_per_call=0,
            ),
            environment,
            system_template=agent_config()["agent"]["system_template"],
            instance_template="{{ task }} {{ issue }} {{ failing_test }} {{ allowed_source_paths }}",
            step_limit=len(actions),
            cost_limit=0,
        )

    code, summary = run_baseline(
        configured,
        dependencies=dependencies([], failed_then_rollback_agent),
    )
    assert code == 1
    assert summary["patch_failures"] == 1

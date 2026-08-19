import hashlib
import json
import shutil
from pathlib import Path

import pytest

from benchmarks.scripts.run_fastfix_secure import PreflightResult, initialize_source
from benchmarks.scripts.run_ff002_route_ablation import (
    ARM_ORDER,
    FIXTURE,
    PROTOCOL_PATH,
    ROUTE_GUIDANCE,
    AblationDependencies,
    AblationSettings,
    SecureRunnerError,
    agent_config,
    arm_registry,
    build_parser,
    compare,
    inspect_experiment,
    load_protocol,
    provider_failure_summary,
    run_arm,
    successful_read_files,
)
from fastfix.agents.repair import FastFixRepairAgent
from fastfix.environments.repair_environment import FastFixRepairEnvironment
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output
from tests.fastfix.workflows.test_secure_repair import RecordingValidationBackend, tool_action


def hashes(repository: Path) -> dict[str, str]:
    return {
        path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repository.rglob("*")
        if path.is_file()
    }


def submission() -> dict:
    return {
        "summary": "Correct the route path parameter binding.",
        "root_cause": "The route path placeholder and handler parameter names differ.",
        "changed_files": ["app/main.py"],
        "tests_run": ["targeted", "regression", "ruff"],
        "risk_notes": [],
        "confidence": 1.0,
    }


def actions(arm: str) -> list[dict]:
    values = []
    number = 1
    if arm == "route":
        values.append(tool_action("inspect_fastapi_routes", {"path": "."}, number))
        number += 1
    for path in ("app/main.py", "app/service.py") if arm == "route" else ("app/main.py", "tests/test_users.py"):
        values.append(tool_action("read_file", {"path": path}, number))
        number += 1
    values.extend(
        [
            tool_action(
                "replace_text",
                {
                    "path": "app/main.py",
                    "old_text": '@app.get("/users/{id}", response_model=UserResponse)',
                    "new_text": '@app.get("/users/{user_id}", response_model=UserResponse)',
                },
                number,
            ),
            tool_action(
                "run_pytest",
                {
                    "scope": "targeted",
                    "targets": ["tests/test_users.py::test_get_user_returns_user"],
                },
                number + 1,
            ),
            tool_action("run_pytest", {"scope": "regression"}, number + 2),
            tool_action("run_ruff", {}, number + 3),
            tool_action("show_git_diff", {}, number + 4),
            tool_action("submit_repair", submission(), number + 5),
        ]
    )
    return values


def scripted_agent(environment: FastFixRepairEnvironment, arm: str) -> FastFixRepairAgent:
    scripted = actions(arm)
    return FastFixRepairAgent(
        DeterministicToolcallModel(
            outputs=[make_toolcall_output(None, [{"id": item["tool_call_id"]}], [item]) for item in scripted],
            cost_per_call=0,
        ),
        environment,
        system_template=agent_config(arm)["agent"]["system_template"],
        instance_template="{{ task }} {{ issue }} {{ failing_test }} {{ allowed_source_paths }}",
        step_limit=len(scripted),
        cost_limit=0,
    )


def dependencies(
    calls: dict[str, list[tuple[str, list[str]]]],
    agents: dict[str, FastFixRepairAgent],
) -> AblationDependencies:
    return AblationDependencies(
        preflight=lambda model, image, output: PreflightResult(
            provider="openai",
            resolved_model=model,
            docker_executable="docker",
            docker_server_version="test",
            image_id="sha256:scripted",
        ),
        validation_backend_factory=lambda candidate, image: RecordingValidationBackend(
            calls.setdefault(candidate.parent.parent.name, [])
        ),
        agent_factory=lambda environment, arm, model, cost: agents.setdefault(
            arm,
            scripted_agent(environment, arm),
        ),
    )


def settings(tmp_path: Path) -> AblationSettings:
    return AblationSettings(
        model_name="openai/scripted",
        image="fastfix-validation:ff001-v1",
        runtime_root=tmp_path / "runtime with spaces",
        results_root=tmp_path / "results with spaces",
    )


def result_text(root: Path) -> str:
    return "".join(path.read_text(encoding="utf-8", errors="replace") for path in root.rglob("*") if path.is_file())


def test_frozen_protocol_and_arm_prompt_registry_are_isolated(tmp_path: Path) -> None:
    protocol = load_protocol()
    route = agent_config("route")
    baseline = agent_config("baseline")
    backend = RecordingValidationBackend([])
    workspace = tmp_path / "repository"
    shutil.copytree(FIXTURE, workspace)
    initialize_source(workspace)
    baseline_names = arm_registry(workspace, backend, "baseline").names()
    route_names = arm_registry(workspace, backend, "route").names()

    assert protocol["arm_order"] == ["baseline", "route"]
    assert protocol["task_commit"] == "a1f459b2a8c24fff4ea4cb79461ad43bbba600da"
    assert protocol["system_commit"] == "c12b32929226979d31083174d75c410c9e284983"
    assert protocol["result_labels"] == {
        "evaluation_role": "development_targeted_ablation",
        "metric_eligible": False,
        "task_external_exposure_before_run": False,
        "task_provenance": "synthetic",
    }
    assert "inspect_fastapi_routes" not in baseline_names
    assert "inspect_fastapi_routes" in route_names
    assert "inspect_fastapi_routes" not in baseline["agent"]["system_template"]
    assert ROUTE_GUIDANCE in route["agent"]["system_template"]
    assert route["agent"]["system_template"].replace(ROUTE_GUIDANCE, "") == baseline["agent"]["system_template"]
    assert route["agent"] | {"system_template": ""} == baseline["agent"] | {"system_template": ""}
    assert route["model"] == baseline["model"]
    assert baseline_names == (
        "show_tree",
        "read_file",
        "search_code",
        "replace_text",
        "apply_patch",
        "run_pytest",
        "run_ruff",
        "show_git_diff",
        "rollback_changes",
    )
    assert route_names[:4] == ("show_tree", "read_file", "search_code", "inspect_fastapi_routes")
    assert (
        arm_registry(workspace, backend, "baseline").execute("inspect_fastapi_routes", {}).error_code == "unknown_tool"
    )
    assert arm_registry(workspace, backend, "route").execute("inspect_fastapi_routes", {}).ok


def test_cli_has_fixed_order_and_no_approval_actions() -> None:
    parser = build_parser()

    assert ARM_ORDER == ("baseline", "route")
    assert parser.parse_args(["run", "--arm", "baseline"]).arm == "baseline"
    assert parser.parse_args(["run", "--arm", "route"]).arm == "route"
    for command in ("approve", "reject", "rollback"):
        with pytest.raises(SystemExit):
            parser.parse_args([command])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--arm", "other"])
    protocol_text = PROTOCOL_PATH.read_text(encoding="utf-8")
    assert "API_KEY" not in protocol_text
    assert str(Path.home()) not in protocol_text


def test_scripted_arms_use_independent_workspaces_and_create_pending_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(tmp_path)
    calls: dict[str, list[tuple[str, list[str]]]] = {}
    agents: dict[str, FastFixRepairAgent] = {}
    canonical_before = hashes(FIXTURE)
    monkeypatch.setenv("FF002_TEST_API_KEY", "must-not-appear-in-results")

    baseline_code, baseline = run_arm(configured, "baseline", dependencies=dependencies(calls, agents))
    route_code, route = run_arm(configured, "route", dependencies=dependencies(calls, agents))

    assert baseline_code == route_code == 0
    assert baseline["approval_pending"] and route["approval_pending"]
    assert baseline["resolved_candidate"] and route["resolved_candidate"]
    assert baseline["session_id"] != route["session_id"]
    assert baseline["approval_request_id"] != route["approval_request_id"]
    assert baseline["tool_sequence"] == [
        "read_file",
        "read_file",
        "replace_text",
        "run_pytest",
        "run_pytest",
        "run_ruff",
        "show_git_diff",
        "submit_repair",
    ]
    assert route["tool_sequence"] == ["inspect_fastapi_routes", *baseline["tool_sequence"]]
    assert baseline["unique_files_read"] == ["app/main.py", "tests/test_users.py"]
    assert route["unique_files_read"] == ["app/main.py", "app/service.py"]
    assert baseline["last_provider_error_type"] is None
    assert baseline["last_provider_status_code"] is None
    assert not baseline["provider_failure_exhausted"]
    assert baseline["inspect_fastapi_routes_calls"] == 0
    assert route["inspect_fastapi_routes_calls"] == 1
    assert baseline["source_unchanged"] and route["source_unchanged"]
    assert baseline["canonical_fixture_unchanged"] and route["canonical_fixture_unchanged"]
    assert hashes(FIXTURE) == canonical_before
    assert len(calls) == 2
    assert all(
        value
        == [
            ("pytest", ["-q", "tests/test_users.py::test_get_user_returns_user"]),
            ("pytest", ["-q", "tests"]),
            ("ruff", ["check", "app"]),
        ]
        for value in calls.values()
    )
    assert agents["baseline"] is not agents["route"]
    assert agents["baseline"].messages is not agents["route"].messages
    for arm in ARM_ORDER:
        directory = configured.results_root / arm / "run-001"
        assert (directory / "approval-request.json").is_file()
        assert (directory / "patch.diff").is_file()
        assert not any(
            (directory / name).exists()
            for name in ("decision.json", "application.json", "rollback.json", "reverse.patch")
        )
    text = result_text(configured.results_root)
    assert str(tmp_path) not in text
    assert "must-not-appear-in-results" not in text
    assert inspect_experiment(configured)["arms"]["baseline"]["arm"] == "baseline"

    with pytest.raises(SecureRunnerError) as error:
        run_arm(configured, "baseline", dependencies=dependencies(calls, agents))
    assert error.value.code == "arm_already_attempted"


def test_read_file_and_provider_failure_metrics_use_structured_evidence() -> None:
    trajectory = {
        "info": {
            "fastfix_model": {"provider_attempt_count": 12},
            "model_stats": {"api_calls": 2},
        },
        "messages": [
            {"extra": {"actions": [{"tool": "read_file", "arguments": {"path": "app/main.py"}}]}},
            {"extra": {"actions": [{"tool": "read_file", "arguments": {"path": "tests/test_users.py"}}]}},
            {"extra": {"actions": [{"tool": "read_file", "arguments": {"path": "app/main.py"}}]}},
            {"extra": {"actions": [{"tool": "read_file", "arguments": {"path": "app/service.py"}}]}},
            {
                "role": "exit",
                "extra": {
                    "exit_status": "BadGatewayError",
                    "exception_str": "litellm.BadGatewayError: OpenAIException - Error code: 502",
                },
            },
        ],
    }
    tool_calls = [
        {"tool_name": "read_file", "ok": True},
        {"tool_name": "read_file", "ok": True},
        {"tool_name": "read_file", "ok": True},
        {"tool_name": "read_file", "ok": False},
    ]

    assert successful_read_files(trajectory, tool_calls) == ["app/main.py", "tests/test_users.py"]
    assert provider_failure_summary(trajectory) == {
        "provider_retry_count": 10,
        "last_provider_error_type": "BadGatewayError",
        "last_provider_status_code": 502,
        "provider_failure_exhausted": True,
    }


def test_recorded_assessments_preserve_evidence_boundaries() -> None:
    root = PROTOCOL_PATH.parents[2] / "results" / "ff-002-route-ablation"
    baseline = json.loads((root / "baseline" / "run-001" / "assessment.json").read_text(encoding="utf-8"))
    route = json.loads((root / "route" / "run-001" / "assessment.json").read_text(encoding="utf-8"))
    experiment = json.loads((root / "assessment.json").read_text(encoding="utf-8"))

    assert baseline["outcome"] == "resolved_candidate"
    assert baseline["patch_semantically_valid"] and not baseline["gold_patch_exact_match"]
    assert baseline["approval_action"] == "rejected_after_experiment"
    assert route["outcome"] == "provider_failure"
    assert route["failure_stage"] == "model_interaction"
    assert route["failure_category"] == "provider_bad_gateway_exhausted"
    assert route["candidate_patch_available"] and route["candidate_tests_passed"]
    assert not route["validation_complete"]
    assert not experiment["comparison_valid"] and not experiment["route_tool_invoked"]
    assert experiment["provider_failure_confounded"]
    assert experiment["performance_conclusion"] is None
    assert set(experiment["prohibited_performance_conclusions"]) == {
        "token_usage_improvement",
        "tool_call_reduction",
        "elapsed_time_improvement",
        "repair_success_rate_improvement",
    }


def test_order_preflight_and_existing_result_guards_create_no_attempt(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    calls: dict[str, list[tuple[str, list[str]]]] = {}
    agents: dict[str, FastFixRepairAgent] = {}

    with pytest.raises(SecureRunnerError) as order_error:
        run_arm(configured, "route", dependencies=dependencies(calls, agents))
    assert order_error.value.code == "arm_order_invalid"
    assert not configured.runtime_root.exists()

    def failed_preflight(model: str, image: str, output: Path) -> PreflightResult:
        raise SecureRunnerError("api_key_missing", "missing")

    failed = dependencies(calls, agents)
    failed.preflight = failed_preflight
    with pytest.raises(SecureRunnerError) as preflight_error:
        run_arm(configured, "baseline", dependencies=failed)
    assert preflight_error.value.code == "api_key_missing"
    assert not configured.runtime_root.exists()
    assert not (configured.results_root / "baseline" / "run-001").exists()

    existing = configured.results_root / "baseline" / "run-001"
    existing.mkdir(parents=True)
    with pytest.raises(SecureRunnerError) as result_error:
        run_arm(configured, "baseline", dependencies=dependencies(calls, agents))
    assert result_error.value.code == "arm_already_attempted"


def test_failed_baseline_is_frozen_but_does_not_skip_route(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    calls: dict[str, list[tuple[str, list[str]]]] = {}
    agents: dict[str, FastFixRepairAgent] = {}

    def factory(environment, arm, model, cost):
        if arm == "baseline":
            scripted = [tool_action("read_file", {"path": "app/main.py"}, 1)]
            return FastFixRepairAgent(
                DeterministicToolcallModel(
                    outputs=[make_toolcall_output(None, [{"id": "1"}], scripted)],
                    cost_per_call=0,
                ),
                environment,
                system_template=agent_config(arm)["agent"]["system_template"],
                instance_template="{{ task }} {{ issue }} {{ failing_test }} {{ allowed_source_paths }}",
                step_limit=1,
                cost_limit=0,
            )
        return scripted_agent(environment, arm)

    deps = dependencies(calls, agents)
    deps.agent_factory = factory

    baseline_code, baseline = run_arm(configured, "baseline", dependencies=deps)
    route_code, route = run_arm(configured, "route", dependencies=deps)

    assert baseline_code == 1 and not baseline["approval_pending"]
    assert (configured.results_root / "baseline" / "run-001" / "failure.json").is_file()
    assert route_code == 0 and route["approval_pending"]
    with pytest.raises(SecureRunnerError, match="unique attempt"):
        run_arm(configured, "baseline", dependencies=deps)


def test_comparison_waits_for_both_arms_and_never_mutates_results(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    calls: dict[str, list[tuple[str, list[str]]]] = {}
    agents: dict[str, FastFixRepairAgent] = {}

    run_arm(configured, "baseline", dependencies=dependencies(calls, agents))
    with pytest.raises(SecureRunnerError) as incomplete:
        compare(configured)
    assert incomplete.value.code == "comparison_incomplete"
    run_arm(configured, "route", dependencies=dependencies(calls, agents))
    arm_hashes_before = {arm: hashes(configured.results_root / arm / "run-001") for arm in ARM_ORDER}

    value = compare(configured)

    assert value["interpretation"] is None
    assert value["differences_route_minus_baseline"]["inspect_fastapi_routes_calls"] == 1
    assert "percentage" not in json.dumps(value).casefold()
    assert {arm: hashes(configured.results_root / arm / "run-001") for arm in ARM_ORDER} == arm_hashes_before
    with pytest.raises(SecureRunnerError) as existing:
        compare(configured)
    assert existing.value.code == "comparison_exists"

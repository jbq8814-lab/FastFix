import json

import pytest

from benchmarks.scripts.run_mini_baseline import (
    calculate_resolved,
    changed_files_allowed,
    classify_run_eligibility,
    extract_trajectory_metrics,
    filter_changed_files,
    load_interactive_agent_class,
    model_cost_available,
    normalize_instance_cost,
    validate_model_name,
    validate_run_preflight,
    write_artifacts,
)


def test_extract_trajectory_metrics() -> None:
    assert extract_trajectory_metrics(
        {
            "info": {
                "model_stats": {"instance_cost": 0.125, "api_calls": 3},
                "exit_status": "Submitted",
            }
        }
    ) == {
        "exit_status": "Submitted",
        "api_calls": 3,
        "execution_steps": 3,
        "instance_cost": 0.125,
    }


def test_load_interactive_agent_class_without_console() -> None:
    assert load_interactive_agent_class().__name__ == "InteractiveAgent"


def test_custom_base_url_rejects_unqualified_model() -> None:
    with pytest.raises(ValueError, match="openai/MiniMax-M2.7-bf16"):
        validate_model_name("MiniMax-M2.7-bf16", custom_base_url=True)


def test_qualified_openai_model_passes_preflight(tmp_path) -> None:
    assert validate_run_preflight(
        "openai/MiniMax-M2.7-bf16",
        custom_base_url=True,
        output_dir=tmp_path / "run-002",
    ) == ("MiniMax-M2.7-bf16", "openai")


def test_invalid_model_does_not_create_output_directory(tmp_path) -> None:
    output_dir = tmp_path / "run-002"
    with pytest.raises(ValueError):
        validate_run_preflight("MiniMax-M2.7-bf16", custom_base_url=True, output_dir=output_dir)
    assert not output_dir.exists()


def test_run_without_assistant_response_is_not_eligible() -> None:
    assert classify_run_eligibility({"messages": []}, "BadRequestError: provider missing") == {
        "benchmark_eligible": False,
        "benchmark_attempt": None,
        "assistant_response_count": 0,
        "failure_stage": "model_provider_resolution",
        "failure_category": "configuration_error",
    }


def test_run_with_assistant_response_is_eligible() -> None:
    assert classify_run_eligibility({"messages": [{"role": "assistant"}]}, "") == {
        "benchmark_eligible": True,
        "benchmark_attempt": 1,
        "assistant_response_count": 1,
        "failure_stage": None,
        "failure_category": None,
    }


def test_generated_caches_are_excluded_from_changed_files() -> None:
    assert filter_changed_files(
        [
            "app/__pycache__/main.cpython-312.pyc",
            "app/main.py",
            ".pytest_cache/README.md",
            ".ruff_cache/content",
        ]
    ) == ["app/main.py"]


def test_real_test_change_is_preserved() -> None:
    assert filter_changed_files(["tests/__pycache__/test.pyc", "tests/test_users.py"]) == ["tests/test_users.py"]


def test_unknown_model_cost_is_unavailable() -> None:
    assert not model_cost_available("openai/MiniMax-M2.7-bf16")
    assert normalize_instance_cost(0.0, cost_available=False) is None


def test_changed_files_allowed() -> None:
    assert changed_files_allowed(["app/main.py", "app/services/users.py"])
    assert not changed_files_allowed([])
    assert not changed_files_allowed(["app/main.py", "tests/test_users.py"])
    assert not changed_files_allowed(["README.md"])


def test_resolved_requires_diff_tests_ruff_and_app_only_changes() -> None:
    assert calculate_resolved(
        "diff --git a/app/main.py b/app/main.py",
        ["app/main.py"],
        pytest_passed=True,
        ruff_passed=True,
    )
    assert not calculate_resolved("", ["app/main.py"], pytest_passed=True, ruff_passed=True)
    assert not calculate_resolved(
        "diff",
        ["app/main.py"],
        pytest_passed=False,
        ruff_passed=True,
    )
    assert not calculate_resolved(
        "diff",
        ["app/main.py"],
        pytest_passed=True,
        ruff_passed=False,
    )
    assert not calculate_resolved(
        "diff",
        ["tests/test_users.py"],
        pytest_passed=True,
        ruff_passed=True,
    )


def test_write_artifacts_creates_complete_output(tmp_path) -> None:
    summary = {
        "task_id": "FF-001",
        "run_id": "run-001",
        "baseline": "mini-swe-agent-v2.4.6",
        "model": "test/model",
        "exit_status": "Submitted",
        "resolved": True,
        "api_calls": 2,
        "instance_cost": 0.1,
        "elapsed_seconds": 3.5,
        "changed_files": ["app/main.py"],
        "pytest_passed": True,
        "ruff_passed": True,
    }
    write_artifacts(
        tmp_path,
        summary=summary,
        trajectory={"messages": []},
        patch="diff",
        pytest_log="2 passed",
        ruff_log="All checks passed!",
        changed_files=["app/main.py"],
    )

    assert {path.name for path in tmp_path.iterdir()} == {
        "summary.json",
        "trajectory.json",
        "patch.diff",
        "pytest.log",
        "ruff.log",
        "changed-files.txt",
    }
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8")) == summary

import json
import re
import subprocess
from pathlib import Path

from benchmarks.scripts import run_task_baseline

ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS = ROOT / "benchmarks" / "experiments"
SYSTEM_COMMIT = "ef4452d7215aee8937e2e287c60c3de86354fb55"
PROTOCOLS = {
    "ff-004-current-baseline": (
        "ff-004-unhandled-service-exception",
        "2fa1e4d35dc00b1be8a33b0601396b8948de5316",
        "FF-004 current FastFix unseen-task baseline",
    ),
    "ff-005-current-baseline": (
        "ff-005-missing-depends",
        "81ca7c1b673b6dba23e09c572a63e3eebf007307",
        "FF-005 current FastFix unseen-task baseline",
    ),
    "ff-006-current-baseline": (
        "ff-006-wrong-created-status",
        "191b0aa82eb0d6366a784a94f5e741f0f7571446",
        "FF-006 current FastFix unseen-task baseline",
    ),
    "ff-007-current-baseline": (
        "ff-007-missing-service-return",
        "8bf610458b85bc1c15e877dcecc2b591776f15c2",
        "FF-007 current FastFix unseen-task baseline",
    ),
    "ff-008-current-baseline": (
        "ff-008-uncommitted-user",
        "0cafdbd502bed49ddc7088adb8a610ceedb0765c",
        "FF-008 current FastFix unseen-task baseline",
    ),
}
METRICS = [
    "status",
    "exit_status",
    "submitted",
    "approval_pending",
    "resolved_candidate",
    "assistant_response_count",
    "api_calls",
    "input_tokens",
    "output_tokens",
    "elapsed_seconds",
    "tool_call_count",
    "tool_sequence",
    "read_file_calls",
    "unique_files_read",
    "search_code_calls",
    "inspect_fastapi_routes_calls",
    "replace_text_calls",
    "apply_patch_calls",
    "patch_failures",
    "provider_retry_count",
    "last_provider_error_type",
    "last_provider_status_code",
    "provider_failure_exhausted",
    "targeted_tests_passed",
    "regression_tests_passed",
    "ruff_passed",
    "changed_files",
    "source_unchanged",
    "canonical_fixture_unchanged",
    "instance_cost",
]


def protocol_path(experiment: str) -> Path:
    return EXPERIMENTS / experiment / "protocol.json"


def load_protocol(experiment: str) -> dict[str, object]:
    return json.loads(protocol_path(experiment).read_text(encoding="utf-8"))


def test_ff004_ff008_protocols_load_and_match_tasks() -> None:
    assert {path.name for path in EXPERIMENTS.glob("ff-00[4-8]-current-baseline") if path.is_dir()} == set(PROTOCOLS)
    for experiment, (task_id, task_commit, experiment_name) in PROTOCOLS.items():
        path = protocol_path(experiment)
        context = run_task_baseline.load_context(path)
        protocol = context.protocol
        task = json.loads((ROOT / "benchmarks" / "tasks" / task_id / "task.json").read_text())
        assert context.task_id == protocol["task_id"] == task_id
        assert protocol["experiment_name"] == experiment_name
        assert protocol["protocol_version"] == "1.0"
        assert protocol["task_commit"] == task_commit
        assert protocol["system_commit"] == SYSTEM_COMMIT
        assert protocol["secure_workflow_version"] == "secure-workflow-v1"
        assert protocol["model_env_name"] == "MSWEA_MODEL_NAME"
        assert protocol["docker_image_reference"] == "fastfix-validation:benchmark-v1"
        assert protocol["maximum_iterations"] == 20
        assert protocol["include_route_inspection"] is True
        assert protocol["include_pydantic_inspection"] is False
        assert protocol["timeouts"] == {
            "agent_wall_seconds": 600,
            "pytest_seconds": 60,
            "ruff_seconds": 60,
        }
        assert protocol["provider_retry"] == {
            "stop_after_attempt_env_name": "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT",
            "stop_after_attempt": 10,
        }
        assert protocol["validation_commands"] == {
            "targeted": ["pytest", "-q", *task["failing_tests"]],
            "regression": ["pytest", "-q", "tests"],
            "ruff": ["ruff", "check", *task["allowed_paths"]],
        }
        assert protocol["expected_changed_files"] == task["buggy_files"]
        assert protocol["metrics"] == METRICS
        assert protocol["result_labels"] == {
            "evaluation_role": "development_unseen_baseline",
            "metric_eligible": False,
            "task_provenance": task["provenance"],
            "task_external_exposure_before_run": False,
        }


def test_ff004_ff008_commits_are_frozen() -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", SYSTEM_COMMIT)
    for task_id, task_commit, _ in PROTOCOLS.values():
        assert re.fullmatch(r"[0-9a-f]{40}", task_commit)
        task_path = Path("benchmarks") / "tasks" / task_id / "task.json"
        result = subprocess.run(
            [
                "git",
                "log",
                "--diff-filter=A",
                "--format=%H",
                "--reverse",
                "--",
                task_path.as_posix(),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        assert result.returncode == 0
        assert result.stdout.splitlines() == [task_commit]

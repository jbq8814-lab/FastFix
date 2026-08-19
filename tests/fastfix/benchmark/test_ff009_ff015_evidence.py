import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS = ROOT / "benchmarks" / "experiments"
RESULTS = ROOT / "benchmarks" / "results"
CASES = [
    ("ff-009-current-baseline", "ff-009-session-lifecycle", "app/database.py"),
    ("ff-010-current-baseline", "ff-010-orm-attribute-mismatch", "app/models.py"),
    ("ff-011-current-baseline", "ff-011-static-route-shadowed", "app/main.py"),
    ("ff-012-current-baseline", "ff-012-response-field-type-mismatch", "app/schemas.py"),
    ("ff-013-current-baseline", "ff-013-dependency-exception-not-raised", "app/dependencies.py"),
    ("ff-014-current-baseline", "ff-014-awaiting-sync-service", "app/main.py"),
    ("ff-015-current-baseline", "ff-015-environment-variable-mapping", "app/config.py"),
]
TASK_COMMITS = {
    "ff-009-current-baseline": "94352bf3435e0e41b103ff7f1e678896f9960134",
    "ff-010-current-baseline": "0b6b130946001a1a8f8be4baf9a534974fcea501",
    "ff-011-current-baseline": "fb563a2c4973efab0c2de54ff2567fd5c23504fd",
    "ff-012-current-baseline": "423a59a4c9fe4237fbeed0fda5f22fd8da6742cc",
    "ff-013-current-baseline": "7f9053120de8764c78d97622d910e0112ab63099",
    "ff-014-current-baseline": "dd825b028bbf4c30edec9e0352606b624b131a0e",
    "ff-015-current-baseline": "3cfdabfa6b1e17984bdf56441c747e344c98b7d2",
}
SESSIONS = {
    "ff-009-current-baseline": "71fc0eee-9abf-4636-8304-f92b01fb7410",
    "ff-010-current-baseline": "8cce7bff-16e4-4df8-af9f-04be4ae76e65",
    "ff-011-current-baseline": "49ac4a4b-20f9-4a3d-b595-710a7f4d7fef",
    "ff-012-current-baseline": "6da71e90-4a58-4561-9db9-ccd0ae5ac4d9",
    "ff-013-current-baseline": "1c6655ed-a8d4-4c5b-b385-8e1e9daa9375",
    "ff-014-current-baseline": "2941b498-4d52-425e-ab0a-a0628c7b20ad",
    "ff-015-current-baseline": "3178acc7-72e9-435a-a2eb-e47aa60f667a",
}
PATCH_HASHES = {
    "ff-009-current-baseline": "4bee50abf5eddac81332ae5ec1f7120d64f02926a2f43b4d834b8b38d37ea0ae",
    "ff-010-current-baseline": "eedcfafb1a47c5c2e3cfa0961ae30df7c734d93581612029327ba0b360afd71f",
    "ff-011-current-baseline": "b197080923d94450252be2e8740ffd60f52b7f68d22a495c82f283d025dfbc5b",
    "ff-012-current-baseline": "8984f7c9464bfee4cd3a0aad3514a71f1d692119201981c59f6f35a093015001",
    "ff-013-current-baseline": "6f796548b8eb9fc6ac1e95c75ee023fe35216e4aa8c216063d840c78d2aee6a5",
    "ff-014-current-baseline": "cd00c5572938af493b4406944f9c7a2c4227387472f333f891e6dc140591dd1c",
    "ff-015-current-baseline": "f82ee8fae0d6dec4e4d9fc4b2e6c0bd3d8b8e86b8730219bbfa9d3c93e8d0381",
}
RUN_FILES = {
    "approval-request.json",
    "changed-files.txt",
    "patch.diff",
    "summary.json",
    "tool-calls.json",
    "trajectory.json",
    "validation-summary.json",
}


def payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_commit(commit: str) -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
            shell=False,
        ).returncode
        == 0
    )


@pytest.mark.parametrize(("experiment", "task_id", "changed_file"), CASES)
def test_ff009_ff015_protocols_and_attempt_identity_are_frozen(
    experiment: str,
    task_id: str,
    changed_file: str,
) -> None:
    source = EXPERIMENTS / experiment / "protocol.json"
    published = RESULTS / experiment / "protocol.json"
    protocol = payload(source)
    assessment = payload(RESULTS / experiment / "run-001" / "assessment.json")
    assert source.exists() and published.exists()
    assert source.read_bytes() == published.read_bytes()
    assert protocol["task_id"] == task_id
    assert experiment[:6] == task_id[:6]
    assert protocol["protocol_version"] == "1.0"
    assert protocol["secure_workflow_version"] == "secure-workflow-v1"
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
    assert protocol["validation_commands"]["regression"] == ["pytest", "-q", "tests"]
    assert protocol["validation_commands"]["ruff"] == ["ruff", "check", "app"]
    assert protocol["expected_changed_files"] == [changed_file]
    assert protocol["result_labels"] == {
        "evaluation_role": "development_unseen_baseline",
        "metric_eligible": False,
        "task_provenance": "synthetic",
        "task_external_exposure_before_run": False,
    }
    assert protocol["task_commit"] == protocol["system_commit"] == TASK_COMMITS[experiment]
    assert_commit(protocol["task_commit"])
    assert_commit(protocol["system_commit"])
    assert assessment["performance_conclusion"] is None
    assert [path.name for path in (RESULTS / experiment).glob("run-*") if path.is_dir()] == ["run-001"]
    expected_source_runs = (
        ["run-001"]
        if experiment
        in {
            "ff-009-current-baseline",
            "ff-010-current-baseline",
        }
        else []
    )
    assert [path.name for path in (EXPERIMENTS / experiment).glob("run-*") if path.is_dir()] == expected_source_runs


@pytest.mark.parametrize(("experiment", "task_id", "changed_file"), CASES)
def test_ff009_ff015_successful_candidate_bundles_are_frozen(
    experiment: str,
    task_id: str,
    changed_file: str,
) -> None:
    run = RESULTS / experiment / "run-001"
    assert RUN_FILES | {"assessment.json"} <= {path.name for path in run.iterdir() if path.is_file()}
    summary = payload(run / "summary.json")
    validation = payload(run / "validation-summary.json")
    assessment = payload(run / "assessment.json")
    request = payload(run / "approval-request.json")
    assert summary["task_id"] == assessment["task_id"] == request["task_id"] == task_id
    assert summary["run_id"] == assessment["run_id"] == "run-001"
    assert summary["session_id"] == assessment["session_id"] == SESSIONS[experiment]
    assert summary["evaluation_role"] == assessment["evaluation_role"] == "development_unseen_baseline"
    assert summary["metric_eligible"] is assessment["metric_eligible"] is False
    assert summary["status"] == "approval_pending"
    assert summary["approval_pending"] is True
    assert summary["submitted"] is summary["resolved_candidate"] is True
    assert assessment["outcome"] == "resolved_candidate"
    assert assessment["decision"] == "reject"
    assert assessment["candidate_cleaned"] is True
    assert assessment["performance_conclusion"] is None
    assert summary["changed_files"] == request["changed_files"] == [changed_file]
    assert (run / "changed-files.txt").read_text(encoding="utf-8").splitlines() == [changed_file]
    assert all(validation[name]["passed"] is True for name in ("targeted", "regression", "ruff"))
    assert all(validation[name]["returncode"] == 0 for name in ("targeted", "regression", "ruff"))
    assert all(validation[name]["timed_out"] is False for name in ("targeted", "regression", "ruff"))
    assert summary["targeted_tests_passed"] is summary["regression_tests_passed"] is summary["ruff_passed"] is True
    assert request["targeted_tests_passed"] is request["regression_tests_passed"] is request["ruff_passed"] is True
    assert request["status"] == "pending"
    assert request["request_id"] == summary["approval_request_id"] == assessment["approval_request_id"]
    assert (
        sha256(run / "patch.diff") == request["patch_sha256"] == assessment["patch_sha256"] == PATCH_HASHES[experiment]
    )
    assert isinstance(payload(run / "tool-calls.json"), list)
    trajectory = payload(run / "trajectory.json")
    assert trajectory["trajectory_format"] == "mini-swe-agent-1.1"
    assert isinstance(trajectory["messages"], list)
    assert isinstance(trajectory["info"], dict)


def test_ff009_original_pending_state_and_release_reject_are_distinct() -> None:
    run = RESULTS / "ff-009-current-baseline" / "run-001"
    summary = payload(run / "summary.json")
    assessment = payload(run / "assessment.json")
    audit = payload(run / "reject-audit.json")
    decision = payload(run / "reject-decision.json")
    manifest = payload(run / "approval-package-manifest.json")
    assert summary["status"] == audit["runtime_status_before_closeout"] == "approval_pending"
    assert audit["runtime_status_after_closeout"] == "rejected"
    assert assessment["approval_action"] == "rejected_during_release_audit_closeout"
    assert decision["decision"] == assessment["decision"] == "reject"
    assert decision["application_sha256"] is None
    assert sha256(run / "patch.diff") == audit["candidate_patch"]["sha256"]
    assert sha256(run / "reject-decision.json") == audit["decision_record"]["sha256"]
    assert sha256(run / "approval-package-manifest.json") == audit["approval_package_manifest"]["sha256"]
    for entry in manifest["files"]:
        assert sha256(run / entry["path"]) == entry["sha256"]


@pytest.mark.parametrize("experiment", ["ff-009-current-baseline", "ff-010-current-baseline"])
def test_ff009_ff010_experiment_source_and_published_copy_are_byte_identical(experiment: str) -> None:
    source = EXPERIMENTS / experiment
    published = RESULTS / experiment
    assert (source / "protocol.json").read_bytes() == (published / "protocol.json").read_bytes()
    assert {path.name for path in (source / "run-001").iterdir() if path.is_file()} == RUN_FILES
    for name in RUN_FILES:
        assert (source / "run-001" / name).read_bytes() == (published / "run-001" / name).read_bytes()
    assert (
        payload(source / "run-001" / "summary.json")["session_id"]
        == payload(published / "run-001" / "summary.json")["session_id"]
    )

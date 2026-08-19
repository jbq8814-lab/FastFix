"""AgentGuard 恢复控制接口：会话解析、幂等台账与真实验证链路。"""

import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from fastfix.control.models import ControlResult, DiagnosisContext
from fastfix.control.service import ControlInterfaceError, ControlInterfaceService

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-008-uncommitted-user"
GOLD_PATCH = ROOT / "benchmarks" / "tasks" / "ff-008-uncommitted-user" / "gold.patch"
REAL_SESSION = (
    "ff-008-current-baseline/sessions/9bfe8234-6b55-46c6-ad9d-002eebae7936"
)
SESSION_ID = "11111111-2222-3333-4444-555555555555"


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )


def make_session(root: Path) -> str:
    experiments = root / "experiments"
    session_dir = experiments / "exp-recovery" / "sessions" / SESSION_ID
    source = session_dir / "source"
    shutil.copytree(FIXTURE, source, ignore=shutil.ignore_patterns(".git*"))
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "Control Test"),
        ("config", "user.email", "control@example.invalid"),
        ("config", "core.autocrlf", "false"),
        ("add", "."),
        ("commit", "-q", "-m", "buggy baseline"),
    ):
        assert git(source, *arguments).returncode == 0, arguments
    session_dir.joinpath("session.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "session_id": SESSION_ID,
                "task_id": "ff-008-uncommitted-user",
                "run_id": "run-001",
                "status": "failed",
                "source_path": str(source),
            }
        ),
        encoding="utf-8",
    )
    return f"exp-recovery/sessions/{SESSION_ID}"


@pytest.fixture
def service(tmp_path: Path) -> ControlInterfaceService:
    make_session(tmp_path)
    return ControlInterfaceService(ROOT, experiments_root=tmp_path / "experiments")


def context() -> DiagnosisContext:
    return DiagnosisContext(
        diagnosis_id="diagnosis_case_a",
        failure_category="validation_closure_failure",
        root_cause_summary="repair finished without completing the validation closure",
        critical_failure_event_id="evt-submit-1",
        evidence_event_ids=["evt-submit-1", "evt-pytest-2"],
        cited_case_ids=[],
        recovery_hint="rerun the full validation chain on the repaired workspace",
    )


def test_status_reports_session_and_workspace(service: ControlInterfaceService) -> None:
    result = service.status(f"exp-recovery/sessions/{SESSION_ID}")
    assert result.status == "executed"
    assert result.details["task_id"] == "ff-008-uncommitted-user"
    assert result.details["candidate_active"] is False
    assert result.workspace is not None
    assert result.workspace.endswith("source")


def test_rerun_validation_runs_real_pytest_and_is_idempotent(service: ControlInterfaceService) -> None:
    first = service.rerun_validation(f"exp-recovery/sessions/{SESSION_ID}", key="key-rerun-0001")
    assert first.status == "executed"
    assert first.validation is not None
    # fixture 基线：1 个失败测试（真实 pytest 输出解析）
    assert (first.validation.pytest_passed, first.validation.pytest_failed) == (2, 1)
    assert first.validation.passed is False
    duplicate = service.rerun_validation(f"exp-recovery/sessions/{SESSION_ID}", key="key-rerun-0001")
    assert duplicate.status == "duplicate"
    assert duplicate.validation == first.validation


def test_rerun_validation_follows_active_candidate_workspace(service: ControlInterfaceService, tmp_path: Path) -> None:
    """candidate 生效后验证必须落在 candidate 上：应用 gold patch 后验证通过。"""
    from fastfix.workspace import CandidateWorkspaceManager

    session_dir = tmp_path / "experiments" / "exp-recovery" / "sessions" / SESSION_ID
    (session_dir / "candidates").mkdir(exist_ok=True)
    manager = CandidateWorkspaceManager(session_dir / "candidates")
    candidate = manager.create(session_dir / "source", target=session_dir / "candidates" / "recovery-0001")
    patch = subprocess.run(
        ["git", "apply", "-"],
        cwd=candidate.path,
        input=GOLD_PATCH.read_bytes(),
        capture_output=True,
        shell=False,
    )
    assert patch.returncode == 0, patch.stderr
    session_dir.joinpath("control").mkdir(exist_ok=True)
    session_dir.joinpath("control", "state.json").write_text(
        json.dumps({"candidate_path": str(candidate.path)}), encoding="utf-8"
    )
    result = service.rerun_validation(f"exp-recovery/sessions/{SESSION_ID}", key="key-rerun-0002")
    assert result.workspace == str(candidate.path)
    assert result.validation is not None and result.validation.passed
    assert result.validation.pytest_failed == 0


def test_rollback_restores_candidate_to_baseline(service: ControlInterfaceService, tmp_path: Path) -> None:
    from fastfix.workspace import CandidateWorkspaceManager

    session_dir = tmp_path / "experiments" / "exp-recovery" / "sessions" / SESSION_ID
    (session_dir / "candidates").mkdir(exist_ok=True)
    manager = CandidateWorkspaceManager(session_dir / "candidates")
    candidate = manager.create(session_dir / "source", target=session_dir / "candidates" / "recovery-0002")
    patched = subprocess.run(
        ["git", "apply", "-"],
        cwd=candidate.path,
        input=GOLD_PATCH.read_bytes(),
        capture_output=True,
        shell=False,
    )
    assert patched.returncode == 0
    session_dir.joinpath("control").mkdir(exist_ok=True)
    session_dir.joinpath("control", "state.json").write_text(
        json.dumps({"candidate_path": str(candidate.path)}), encoding="utf-8"
    )
    result = service.rollback(f"exp-recovery/sessions/{SESSION_ID}", key="key-rollb-0001")
    assert result.status == "executed"
    assert git(candidate.path, "status", "--porcelain").stdout.strip() == ""
    duplicate = service.rollback(f"exp-recovery/sessions/{SESSION_ID}", key="key-rollb-0001")
    assert duplicate.status == "duplicate"


def test_rollback_reports_failure_when_untracked_files_remain(
    service: ControlInterfaceService, tmp_path: Path
) -> None:
    from fastfix.workspace import CandidateWorkspaceManager

    session_dir = tmp_path / "experiments" / "exp-recovery" / "sessions" / SESSION_ID
    (session_dir / "candidates").mkdir(exist_ok=True)
    manager = CandidateWorkspaceManager(session_dir / "candidates")
    candidate = manager.create(
        session_dir / "source", target=session_dir / "candidates" / "recovery-untracked"
    )
    created = candidate.path / "app" / "agent-created.py"
    created.write_text("unsafe residue\n", encoding="utf-8")
    session_dir.joinpath("control").mkdir(exist_ok=True)
    session_dir.joinpath("control", "state.json").write_text(
        json.dumps({"candidate_path": str(candidate.path)}), encoding="utf-8"
    )

    result = service.rollback(f"exp-recovery/sessions/{SESSION_ID}", key="key-rollb-0004")

    assert result.status == "failed"
    assert created.exists()
    assert "app/agent-created.py" in git(candidate.path, "status", "--porcelain").stdout


def test_rollback_without_candidate_is_a_safe_noop(service: ControlInterfaceService) -> None:
    result = service.rollback(f"exp-recovery/sessions/{SESSION_ID}", key="key-rollb-0002")
    assert result.status == "executed"
    assert "No candidate workspace" in result.message
    # source 工作区保持原始 bug 状态
    assert result.workspace is not None
    assert git(Path(result.workspace), "status", "--porcelain").stdout.strip() == ""


def test_invalid_session_references_are_rejected(service: ControlInterfaceService) -> None:
    for bad in (
        "../experiments/exp/sessions/" + SESSION_ID,
        "exp-recovery/sessions/../../" + SESSION_ID,
        str(service.experiments_root / "exp-recovery" / "sessions" / SESSION_ID),
        "exp-recovery/not-sessions/" + SESSION_ID,
        "missing-experiment/sessions/" + SESSION_ID,
    ):
        with pytest.raises((ControlInterfaceError, ValueError)):
            service.status(bad)


def test_diagnosis_context_schema_is_enforced() -> None:
    with pytest.raises(ValidationError):
        DiagnosisContext(
            diagnosis_id="d1",
            failure_category="validation_closure_failure",
            root_cause_summary="x" * 1501,
        )
    with pytest.raises(ValidationError):
        DiagnosisContext.model_validate(
            {
                "diagnosis_id": "d1",
                "failure_category": "validation_closure_failure",
                "root_cause_summary": "ok",
                "shell": "rm -rf /",
            }
        )
    with pytest.raises(ValidationError):
        DiagnosisContext(
            diagnosis_id="d1",
            failure_category="Validation Closure!",  # pattern 只允许小写下划线
            root_cause_summary="ok",
        )


def test_mutating_commands_require_idempotency_key(service: ControlInterfaceService) -> None:
    reference = f"exp-recovery/sessions/{SESSION_ID}"
    with pytest.raises(ControlInterfaceError):
        service.rollback(reference, key=None)  # type: ignore[arg-type]
    with pytest.raises(ControlInterfaceError):
        service.reopen_repair(reference, context(), key=None, model_name="x")  # type: ignore[arg-type]


def test_interrupted_mutating_ledger_refuses_replay(service: ControlInterfaceService, tmp_path: Path) -> None:
    session_dir = tmp_path / "experiments" / "exp-recovery" / "sessions" / SESSION_ID
    ledger = session_dir / "control" / "actions" / "key-rollb-0003.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"command": "rollback", "status": "started", "started_at": "t"}), encoding="utf-8"
    )
    result = service.rollback(f"exp-recovery/sessions/{SESSION_ID}", key="key-rollb-0003")
    assert result.status == "interrupted"
    # 台账未被覆盖成 completed，重复调用保持 interrupted
    assert service.rollback(f"exp-recovery/sessions/{SESSION_ID}", key="key-rollb-0003").status == "interrupted"


def test_idempotency_ledger_claim_is_atomic_across_concurrent_callers(
    service: ControlInterfaceService,
) -> None:
    session = service._resolve_session(f"exp-recovery/sessions/{SESSION_ID}")
    barrier = threading.Barrier(2)

    def claim() -> ControlResult | None:
        barrier.wait()
        return service._begin(session, "key-atomic-0001", "rollback")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(claim), pool.submit(claim))]

    assert sum(result is None for result in results) == 1
    rejected = next(result for result in results if result is not None)
    assert rejected.status == "interrupted"
    assert rejected.details["reason"] == "active_invocation"


def test_active_candidate_must_stay_inside_current_session(
    service: ControlInterfaceService, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    session_dir = tmp_path / "experiments" / "exp-recovery" / "sessions" / SESSION_ID
    control_dir = session_dir / "control"
    control_dir.mkdir(exist_ok=True)
    (control_dir / "state.json").write_text(
        json.dumps({"candidate_path": str(outside)}), encoding="utf-8"
    )

    with pytest.raises(ControlInterfaceError, match="candidate") as error:
        service.status(f"exp-recovery/sessions/{SESSION_ID}")
    assert error.value.code == "candidate_outside_session"


def test_session_task_id_cannot_escape_task_registry(
    service: ControlInterfaceService, tmp_path: Path
) -> None:
    session_file = (
        tmp_path
        / "experiments"
        / "exp-recovery"
        / "sessions"
        / SESSION_ID
        / "session.json"
    )
    payload = json.loads(session_file.read_text(encoding="utf-8"))
    payload["task_id"] = "../../outside"
    session_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ControlInterfaceError) as error:
        service.status(f"exp-recovery/sessions/{SESSION_ID}")
    assert error.value.code == "task_id_invalid"


def test_session_payload_identity_must_match_reference(
    service: ControlInterfaceService, tmp_path: Path
) -> None:
    session_file = (
        tmp_path
        / "experiments"
        / "exp-recovery"
        / "sessions"
        / SESSION_ID
        / "session.json"
    )
    payload = json.loads(session_file.read_text(encoding="utf-8"))
    payload["session_id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    session_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ControlInterfaceError) as error:
        service.status(f"exp-recovery/sessions/{SESSION_ID}")
    assert error.value.code == "session_identity_mismatch"


def test_idempotency_key_conflicts_across_commands(service: ControlInterfaceService) -> None:
    reference = f"exp-recovery/sessions/{SESSION_ID}"
    assert service.rerun_validation(reference, key="key-mix-0001").status == "executed"
    with pytest.raises(ControlInterfaceError):
        service.rollback(reference, key="key-mix-0001")


def test_cli_outputs_json_and_rejects_bad_context(service: ControlInterfaceService, tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "fastfix.control",
            "reopen-repair",
            "--session",
            f"exp-recovery/sessions/{SESSION_ID}",
            "--idempotency-key",
            "key-cli--0001",
            "--diagnosis-context",
            json.dumps({"diagnosis_id": "d1", "failure_category": "bad category!", "root_cause_summary": "x"}),
            "--repo-root",
            str(ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=60,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "rejected"
    assert payload["error_code"] in {"invalid_context"}


def test_real_ff008_session_status_if_available() -> None:
    real_root = ROOT / ".fastfix-runtime" / "experiments"
    if not (real_root / REAL_SESSION).is_dir():
        pytest.skip("Real ff-008 session is unavailable.")
    result = ControlInterfaceService(ROOT).status(REAL_SESSION)
    assert ControlResult.model_validate(result.model_dump()) == result

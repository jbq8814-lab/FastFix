from pathlib import Path

import pytest

from benchmarks.scripts.run_fastfix_secure import (
    PreflightResult,
    PrepareDependencies,
    PrepareSettings,
    SecureRunnerError,
    build_parser,
    decide_session,
    inspect_session,
    load_runtime_session,
    prepare,
    rollback_session,
)
from fastfix.workflows import SecureRepairStage
from tests.fastfix.workflows.test_secure_repair import (
    RecordingValidationBackend,
    agent_factory,
    git,
    tool_action,
)


def dependencies(
    calls: list[tuple[str, list[str]]],
    *,
    actions: list[dict] | None = None,
    passed: bool = True,
) -> PrepareDependencies:
    return PrepareDependencies(
        preflight=lambda model, image, output: PreflightResult(
            provider="openai",
            resolved_model=model,
            docker_executable="docker",
            docker_server_version="test",
            image_id="sha256:scripted",
        ),
        validation_backend_factory=lambda candidate, image: RecordingValidationBackend(calls, passed=passed),
        agent_factory=lambda environment, model, cost: agent_factory(actions)(environment),
    )


def settings(tmp_path: Path, run_id: str = "run-001") -> PrepareSettings:
    return PrepareSettings(
        model_name="openai/scripted",
        image="fastfix-validation:test",
        runtime_root=tmp_path / ".fastfix-runtime",
        output_dir=tmp_path / "results" / run_id,
    )


def test_prepare_persists_pending_session_and_approve_rollback_are_recoverable(tmp_path: Path) -> None:
    calls: list[tuple[str, list[str]]] = []
    configured = settings(tmp_path)

    code, summary = prepare(configured, dependencies=dependencies(calls))

    assert code == 0
    assert summary["approval_pending"] and summary["resolved_candidate"]
    assert summary["source_unchanged"] and summary["canonical_fixture_unchanged"]
    assert summary["instance_cost"] is None and not summary["metric_eligible"]
    assert calls == [
        ("pytest", ["-q", "tests/test_users.py::test_get_user_returns_user"]),
        ("pytest", ["-q", "tests"]),
        ("ruff", ["check", "app"]),
    ]
    assert {path.name for path in configured.output_dir.iterdir()} == {
        "approval-request.json",
        "changed-files.txt",
        "patch.diff",
        "summary.json",
        "tool-calls.json",
        "trajectory.json",
        "validation-summary.json",
    }
    result_text = "".join(path.read_text(encoding="utf-8") for path in configured.output_dir.iterdir())
    assert str(tmp_path) not in result_text and "API_KEY" not in result_text
    session_id = summary["session_id"]
    _, runtime = load_runtime_session(configured.runtime_root, session_id)
    assert runtime.status == SecureRepairStage.APPROVAL_PENDING.value
    assert runtime.candidate_path.is_dir()
    assert git(runtime.source_path, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    inspected = inspect_session(configured.runtime_root, session_id)
    assert inspected["status"] == SecureRepairStage.APPROVAL_PENDING.value

    approved = decide_session(
        configured.runtime_root,
        session_id,
        decision="approve",
        actor="reviewer",
        expected_patch_sha256=inspected["patch_sha256"],
    )

    assert approved["status"] == SecureRepairStage.APPLIED.value
    assert "return await fetch_user(user_id)" in (runtime.source_path / "app" / "main.py").read_text(encoding="utf-8")
    assert git(runtime.source_path, "diff", "--cached", "--quiet").returncode == 0
    assert rollback_session(configured.runtime_root, session_id, actor="reviewer")["status"] == "rolled_back"
    assert git(runtime.source_path, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_reject_is_recoverable_and_never_changes_source(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    code, summary = prepare(configured, dependencies=dependencies([]))
    _, runtime = load_runtime_session(configured.runtime_root, summary["session_id"])

    result = decide_session(
        configured.runtime_root,
        summary["session_id"],
        decision="reject",
        actor="reviewer",
    )

    assert code == 0 and result["status"] == "rejected"
    assert git(runtime.source_path, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    assert not runtime.candidate_path.exists()


def test_preflight_and_no_assistant_failures_create_no_attempt_result(tmp_path: Path) -> None:
    configured = settings(tmp_path)

    def failed_preflight(model: str, image: str, output: Path) -> PreflightResult:
        raise SecureRunnerError("api_key_missing", "missing")

    with pytest.raises(SecureRunnerError) as error:
        prepare(
            configured,
            dependencies=PrepareDependencies(
                preflight=failed_preflight,
                validation_backend_factory=lambda candidate, image: RecordingValidationBackend([]),
                agent_factory=lambda environment, model, cost: agent_factory()(environment),
            ),
        )
    assert error.value.code == "api_key_missing"
    assert not configured.runtime_root.exists() and not configured.output_dir.exists()

    class NoResponseAgent:
        def __init__(self):
            self.extra_template_vars = {}

        def run(self, task: str) -> dict:
            raise RuntimeError("request failed")

        def serialize(self) -> dict:
            return {"info": {}, "messages": []}

    code, summary = prepare(
        configured,
        dependencies=PrepareDependencies(
            preflight=dependencies([]).preflight,
            validation_backend_factory=lambda candidate, image: RecordingValidationBackend([]),
            agent_factory=lambda environment, model, cost: NoResponseAgent(),
        ),
    )
    assert code == 2 and summary is None
    assert not configured.output_dir.exists()
    assert not list((configured.runtime_root / "sessions").iterdir())


def test_failure_after_assistant_is_recorded_without_retry(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    read_only = [tool_action("read_file", {"path": "app/main.py"}, 1)]

    code, summary = prepare(
        configured,
        dependencies=dependencies([], actions=read_only),
    )

    assert code == 1
    assert summary["assistant_response_count"] == 1
    assert not summary["approval_pending"] and not summary["resolved_candidate"]
    assert (configured.output_dir / "failure.json").is_file()
    assert not (configured.output_dir / "patch.diff").exists()
    assert len(list((configured.runtime_root / "sessions").iterdir())) == 1


def test_cli_exposes_prepare_and_recovery_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["prepare", "--model", "openai/test"]).command == "prepare"
    for command in ("inspect", "approve", "reject", "rollback"):
        arguments = [command, "--session-id", "00000000-0000-4000-8000-000000000000"]
        if command != "inspect":
            arguments += ["--actor", "reviewer"]
        if command == "approve":
            arguments += ["--expected-patch-sha256", "0" * 64]
        assert parser.parse_args(arguments).command == command

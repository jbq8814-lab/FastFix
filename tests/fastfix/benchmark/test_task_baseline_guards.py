import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.scripts import run_task_baseline as runner
from benchmarks.scripts.run_fastfix_secure import PreflightResult, SecureRunnerError
from fastfix.security.result_publication import publication_state_path


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


@pytest.fixture
def frozen_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, dict[str, object], str]:
    root = tmp_path / "project"
    task_id = "ff-test-snapshot"
    task_root = root / "benchmarks" / "tasks" / task_id
    fixture = root / "benchmarks" / "fixture_repos" / task_id
    task = {
        "task_id": "FF-TEST",
        "fixture_repo": f"benchmarks/fixture_repos/{task_id}",
        "issue_file": f"benchmarks/tasks/{task_id}/issue.md",
        "gold_patch": f"benchmarks/tasks/{task_id}/gold.patch",
        "failing_tests": ["tests/test_app.py::test_value"],
        "allowed_paths": ["app"],
        "buggy_files": ["app/main.py"],
    }
    write(task_root / "task.json", json.dumps(task))
    write(task_root / "issue.md", "Fix the returned value.\n")
    write(task_root / "gold.patch", "gold\n")
    write(task_root / "freeze.txt", "extra frozen task input\n")
    write(fixture / "app" / "main.py", "VALUE = 'buggy'\n")
    write(fixture / "tests" / "test_app.py", "def test_value():\n    assert True\n")
    write(root / "benchmarks" / "task_support" / "shared.txt", "shared frozen input\n")
    write(root / "system" / "runner.py", "VERSION = 1\n")
    git(root, "init", "-q")
    git(root, "config", "user.email", "fastfix@example.test")
    git(root, "config", "user.name", "FastFix Test")
    git(root, "add", ".")
    git(root, "commit", "-qm", "frozen inputs")
    commit = git(root, "rev-parse", "HEAD")
    protocol = json.loads(
        (runner.ROOT / "benchmarks" / "experiments" / "ff-015-current-baseline" / "protocol.json").read_text(
            encoding="utf-8"
        )
    )
    protocol.update(
        {
            "task_id": task_id,
            "task_commit": commit,
            "system_commit": commit,
            "expected_changed_files": ["app/main.py"],
            "include_route_inspection": False,
            "validation_commands": {
                "targeted": ["pytest", "-q", "tests/test_app.py::test_value"],
                "regression": ["pytest", "-q", "tests"],
                "ruff": ["ruff", "check", "app"],
            },
        }
    )
    protocol_path = root / "benchmarks" / "experiments" / "snapshot-case" / "protocol.json"
    write(protocol_path, json.dumps(protocol))
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "SYSTEM_SNAPSHOT_PATHS", ("system",))
    return root, protocol_path, protocol, commit


def configured(tmp_path: Path, name: str = "case") -> runner.BaselineSettings:
    return runner.BaselineSettings(
        model_name="openai/fake",
        image="fake-image",
        runtime_root=tmp_path / name / "runtime",
        results_root=tmp_path / name / "results",
    )


def test_matching_snapshots_pass_and_unrelated_changes_are_ignored(
    frozen_project: tuple[Path, Path, dict[str, object], str],
) -> None:
    root, _, protocol, _ = frozen_project
    assert runner.verify_snapshots(protocol) == {
        "task": (
            "benchmarks/tasks/ff-test-snapshot",
            "benchmarks/fixture_repos/ff-test-snapshot",
        ),
        "system": ("system",),
    }
    write(root / "docs" / "notes.md", "unrelated\n")
    write(root / "benchmarks" / "results" / "scratch" / "summary.json", "{}")
    write(root / "system" / "__pycache__" / "runner.pyc", "cache")
    write(root / "benchmarks" / "fixture_repos" / "ff-test-snapshot" / ".ruff_cache" / "CACHEDIR.TAG", "cache")
    assert runner.verify_snapshots(protocol)["system"] == ("system",)


def test_protocol_additional_task_snapshot_path_is_frozen(
    frozen_project: tuple[Path, Path, dict[str, object], str],
) -> None:
    root, _, protocol, _ = frozen_project
    protocol["task_snapshot_paths"] = ["benchmarks/task_support/shared.txt"]
    assert runner.verify_snapshots(protocol)["task"][-1] == "benchmarks/task_support/shared.txt"
    write(root / "benchmarks" / "task_support" / "shared.txt", "changed\n")
    with pytest.raises(SecureRunnerError) as error:
        runner.verify_snapshots(protocol)
    assert error.value.code == "task_snapshot_mismatch"


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("task_commit", "task_commit_not_found"),
        ("system_commit", "system_commit_not_found"),
    ],
)
def test_missing_commit_is_rejected(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    field: str,
    code: str,
) -> None:
    _, _, protocol, _ = frozen_project
    protocol[field] = "f" * 40
    with pytest.raises(SecureRunnerError) as error:
        runner.verify_snapshots(protocol)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("relative_path", "replacement"),
    [
        ("benchmarks/fixture_repos/ff-test-snapshot/app/main.py", "VALUE = 'changed'\n"),
        ("benchmarks/tasks/ff-test-snapshot/task.json", None),
        ("benchmarks/tasks/ff-test-snapshot/issue.md", "changed issue\n"),
        ("benchmarks/tasks/ff-test-snapshot/gold.patch", "changed gold\n"),
        ("benchmarks/tasks/ff-test-snapshot/freeze.txt", "changed extra input\n"),
    ],
)
def test_task_snapshot_modification_is_rejected(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    relative_path: str,
    replacement: str | None,
) -> None:
    root, _, protocol, _ = frozen_project
    path = root / relative_path
    if replacement is None:
        task = json.loads(path.read_text(encoding="utf-8"))
        task["title"] = "changed"
        replacement = json.dumps(task)
    write(path, replacement)
    with pytest.raises(SecureRunnerError) as error:
        runner.verify_snapshots(protocol)
    assert error.value.code == "task_snapshot_mismatch"


@pytest.mark.parametrize("change", ["modified", "deleted", "untracked"])
def test_system_snapshot_change_is_rejected(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    change: str,
) -> None:
    root, _, protocol, _ = frozen_project
    path = root / "system" / "runner.py"
    if change == "modified":
        write(path, "VERSION = 2\n")
    elif change == "deleted":
        path.unlink()
    else:
        write(root / "system" / "untracked.py", "UNTRACKED = True\n")
    with pytest.raises(SecureRunnerError) as error:
        runner.verify_snapshots(protocol)
    assert error.value.code == "system_snapshot_mismatch"


def test_snapshot_failure_precedes_all_run_side_effects(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    tmp_path: Path,
) -> None:
    root, protocol_path, _, _ = frozen_project
    write(root / "system" / "runner.py", "VERSION = 2\n")
    calls: list[str] = []
    dependencies = runner.BaselineDependencies(
        preflight=lambda model, image, output: calls.append("provider"),
        validation_backend_factory=lambda candidate, image: calls.append("candidate"),
        agent_factory=lambda environment, model, cost: calls.append("session"),
    )
    settings = configured(tmp_path)
    with pytest.raises(SecureRunnerError) as error:
        runner.run_baseline(settings, protocol_path=protocol_path, dependencies=dependencies)
    assert error.value.code == "system_snapshot_mismatch"
    assert calls == []
    assert not settings.runtime_root.exists()
    assert not settings.results_root.exists()


def lease_protocol(task_id: str = "ff-lease") -> dict[str, object]:
    return {
        "task_id": task_id,
        "task_commit": "1" * 40,
        "system_commit": "2" * 40,
    }


def test_first_lease_wins_and_metadata_is_complete(tmp_path: Path) -> None:
    protocol_path = tmp_path / "protocol.json"
    protocol = lease_protocol()
    write(protocol_path, json.dumps(protocol))
    lease = runner.acquire_attempt_lease(tmp_path / "runtime", protocol_path, protocol)
    value = json.loads((lease / "lease.json").read_text(encoding="utf-8"))
    assert {
        "schema_version",
        "runner_version",
        "task_id",
        "attempt_id",
        "protocol_path",
        "protocol_sha256",
        "task_commit",
        "system_commit",
        "created_at",
        "process_id",
        "hostname",
    } <= value.keys()
    with pytest.raises(SecureRunnerError) as error:
        runner.acquire_attempt_lease(tmp_path / "runtime", protocol_path, protocol)
    assert error.value.code == "run_already_attempted"


def test_two_processes_compete_atomically_for_one_lease(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    protocol_path = tmp_path / "protocol.json"
    write(protocol_path, json.dumps(lease_protocol()))
    code = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "from benchmarks.scripts.run_task_baseline import acquire_attempt_lease,SecureRunnerError\n"
        "try:\n"
        " acquire_attempt_lease(Path(sys.argv[1]),Path(sys.argv[2]),json.loads(Path(sys.argv[2]).read_text()))\n"
        "except SecureRunnerError:\n"
        " raise SystemExit(2)\n"
    )
    processes = [subprocess.Popen([sys.executable, "-c", code, str(runtime), str(protocol_path)]) for _ in range(2)]
    assert sorted(process.wait(timeout=30) for process in processes) == [0, 2]
    assert len(list(runtime.glob("attempt-leases/ff-lease/run-001/lease.json"))) == 1


def test_lease_isolated_by_task_and_attempt(tmp_path: Path) -> None:
    protocol_path = tmp_path / "protocol.json"
    first = lease_protocol("ff-first")
    second = lease_protocol("ff-second")
    write(protocol_path, json.dumps(first))
    runner.acquire_attempt_lease(tmp_path, protocol_path, first)
    runner.acquire_attempt_lease(tmp_path, protocol_path, second)
    runner.acquire_attempt_lease(tmp_path, protocol_path, first, "run-002")
    assert len(list(tmp_path.glob("attempt-leases/*/*/lease.json"))) == 3


def test_corrupt_lease_is_never_overwritten(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    tmp_path: Path,
) -> None:
    _, protocol_path, protocol, _ = frozen_project
    settings = configured(tmp_path)
    lease = runner._lease_dir(settings.runtime_root, protocol["task_id"])
    write(lease / "lease.json", "{broken")
    context = runner.load_context(protocol_path)
    assert runner.attempt_status(settings, context) == {
        "state": "lease_invalid",
        "can_run": False,
        "error": "attempt_lease_invalid",
    }
    with pytest.raises(SecureRunnerError) as error:
        runner.acquire_attempt_lease(settings.runtime_root, protocol_path, protocol)
    assert error.value.code == "attempt_lease_invalid"
    assert (lease / "lease.json").read_text(encoding="utf-8") == "{broken"


def test_attempt_status_distinguishes_lifecycle_states(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    tmp_path: Path,
) -> None:
    _, protocol_path, protocol, _ = frozen_project
    settings = configured(tmp_path)
    context = runner.load_context(protocol_path)
    assert runner.attempt_status(settings, context) == {"state": "not_started", "can_run": True}
    assert runner.inspect_baseline(settings, protocol_path=protocol_path)["attempt"]["state"] == "not_started"
    runner.acquire_attempt_lease(settings.runtime_root, protocol_path, protocol)
    assert runner.attempt_status(settings, context)["state"] == "lease_acquired"
    assert runner.inspect_baseline(settings, protocol_path=protocol_path)["attempt"]["state"] == "lease_acquired"
    write(
        settings.runtime_root / "sessions" / "session" / "session.json",
        json.dumps({"task_id": protocol["task_id"], "run_id": "run-001"}),
    )
    assert runner.attempt_status(settings, context)["state"] == "completed"
    assert runner.inspect_baseline(settings, protocol_path=protocol_path)["attempt"]["state"] == "completed"
    write(settings.results_root / "run-001" / "summary.json", "{}")
    assert runner.attempt_status(settings, context)["state"] == "published"
    assert runner.inspect_baseline(settings, protocol_path=protocol_path)["attempt"]["state"] == "published"


def test_inspect_treats_failed_publication_as_incomplete_without_mutating_it(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    tmp_path: Path,
) -> None:
    _, protocol_path, _, _ = frozen_project
    settings = configured(tmp_path)
    context = runner.load_context(protocol_path)
    result = settings.results_root / "run-001"
    write(result / "summary.json", '{"status": "approval_pending"}')
    state_path = publication_state_path(result)
    write(
        state_path,
        json.dumps(
            {
                "schema_version": 1,
                "state": "failed",
                "error": "windows_acl_repair_failed",
                "destination": "run-001",
            }
        ),
    )
    before = state_path.read_bytes()

    assert runner.attempt_status(settings, context) == {
        "state": "result_incomplete",
        "can_run": False,
        "publication": {
            "schema_version": 1,
            "state": "failed",
            "error": "windows_acl_repair_failed",
            "destination": "run-001",
        },
    }
    inspected = runner.inspect_baseline(settings, protocol_path=protocol_path)
    assert inspected["attempt"]["state"] == "result_incomplete"
    assert inspected["run"] == {"status": "publication_incomplete"}
    calls: list[str] = []
    assert (
        runner.preflight(
            settings,
            protocol_path=protocol_path,
            dependencies=runner.BaselineDependencies(
                preflight=lambda model, image, output: calls.append("provider"),
                validation_backend_factory=lambda candidate, image: calls.append("candidate"),
                agent_factory=lambda environment, model, cost: calls.append("session"),
            ),
        )["can_run"]
        is False
    )
    assert calls == []
    assert state_path.read_bytes() == before


def test_preflight_reports_consumed_lease_without_provider_call(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    tmp_path: Path,
) -> None:
    _, protocol_path, protocol, _ = frozen_project
    settings = configured(tmp_path)
    runner.acquire_attempt_lease(settings.runtime_root, protocol_path, protocol)
    calls: list[str] = []
    dependencies = runner.BaselineDependencies(
        preflight=lambda model, image, output: calls.append("provider"),
        validation_backend_factory=lambda candidate, image: calls.append("candidate"),
        agent_factory=lambda environment, model, cost: calls.append("session"),
    )
    assert runner.preflight(settings, protocol_path=protocol_path, dependencies=dependencies) == {
        "attempt": {
            "state": "lease_acquired",
            "can_run": False,
            "disposition": "running_or_interrupted",
            "lease": runner._read_lease(
                runner._lease_dir(settings.runtime_root, protocol["task_id"]),
                protocol,
            ),
        },
        "can_run": False,
    }
    assert calls == []


def test_failed_run_preflight_keeps_lease_and_blocks_retry_before_provider(
    frozen_project: tuple[Path, Path, dict[str, object], str],
    tmp_path: Path,
) -> None:
    _, protocol_path, protocol, _ = frozen_project
    settings = configured(tmp_path)
    calls: list[str] = []

    def fail_preflight(model: str, image: str, output: Path) -> PreflightResult:
        calls.append("provider")
        raise SecureRunnerError("api_key_missing", "missing")

    dependencies = runner.BaselineDependencies(
        preflight=fail_preflight,
        validation_backend_factory=lambda candidate, image: calls.append("candidate"),
        agent_factory=lambda environment, model, cost: calls.append("session"),
    )
    with pytest.raises(SecureRunnerError) as first:
        runner.run_baseline(settings, protocol_path=protocol_path, dependencies=dependencies)
    assert first.value.code == "api_key_missing"
    with pytest.raises(SecureRunnerError) as second:
        runner.run_baseline(settings, protocol_path=protocol_path, dependencies=dependencies)
    assert second.value.code == "run_already_attempted"
    assert calls == ["provider"]
    assert (
        runner._read_lease(
            runner._lease_dir(settings.runtime_root, protocol["task_id"]),
            protocol,
        )["task_id"]
        == protocol["task_id"]
    )

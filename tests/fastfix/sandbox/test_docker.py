import inspect
import json
from pathlib import Path

from fastfix.sandbox.docker import (
    DockerCommandResult,
    DockerCommandTimeout,
    DockerValidationBackend,
    SubprocessDockerCLI,
)


class FakeDockerCLI:
    def __init__(
        self,
        *,
        daemon_available: bool = True,
        os_type: str = "linux",
        image_available: bool = True,
        create_returncode: int = 0,
        start_returncode: int = 0,
        start_timeout: bool = False,
        status: str = "exited",
        oom_killed: bool = False,
        state_error: str = "",
        runner_payload: dict[str, object] | None = None,
        corrupt_result: bool = False,
        missing_result: bool = False,
        cleanup_returncode: int = 0,
        start_stdout: str = "",
    ):
        self.daemon_available = daemon_available
        self.os_type = os_type
        self.image_available = image_available
        self.create_returncode = create_returncode
        self.start_returncode = start_returncode
        self.start_timeout = start_timeout
        self.status = status
        self.oom_killed = oom_killed
        self.state_error = state_error
        self.runner_payload = runner_payload or {
            "returncode": 0,
            "output": "1 passed\n",
            "duration_seconds": 0.2,
            "timed_out": False,
            "output_truncated": False,
            "runner_error": None,
            "metadata": {"container_uid": 65532},
        }
        self.corrupt_result = corrupt_result
        self.missing_result = missing_result
        self.cleanup_returncode = cleanup_returncode
        self.start_stdout = start_stdout
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout_seconds: int) -> DockerCommandResult:
        self.calls.append(arguments)
        if arguments[:2] == ["version", "--format"]:
            if not self.daemon_available:
                return DockerCommandResult(1, stderr="unavailable")
            return DockerCommandResult(
                0,
                json.dumps({"Version": "29.6.2", "Os": self.os_type, "Arch": "x86_64"}),
            )
        if arguments[:2] == ["image", "inspect"]:
            if not self.image_available:
                return DockerCommandResult(1, stderr="missing")
            return DockerCommandResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": "sha256:image",
                            "RepoDigests": ["fastfix-validation@sha256:digest"],
                        }
                    ]
                ),
            )
        if arguments[0] == "create":
            return DockerCommandResult(self.create_returncode, stdout="container-id\n")
        if arguments[:2] == ["start", "--attach"]:
            if self.start_timeout:
                raise DockerCommandTimeout
            return DockerCommandResult(self.start_returncode, stdout=self.start_stdout)
        if arguments[0] == "inspect":
            return DockerCommandResult(
                0,
                json.dumps(
                    [
                        {
                            "State": {
                                "ExitCode": self.runner_payload["returncode"],
                                "OOMKilled": self.oom_killed,
                                "Error": self.state_error,
                                "Status": self.status,
                            },
                            "HostConfig": {"NetworkMode": "none"},
                        }
                    ]
                ),
            )
        if arguments[0] == "cp":
            if not self.missing_result:
                destination = Path(arguments[2])
                destination.write_text(
                    "not json" if self.corrupt_result else json.dumps(self.runner_payload),
                    encoding="utf-8",
                )
            return DockerCommandResult(1 if self.missing_result else 0)
        if arguments[0] == "rm":
            return DockerCommandResult(self.cleanup_returncode)
        raise AssertionError(arguments)


def backend(tmp_path: Path, cli: FakeDockerCLI) -> DockerValidationBackend:
    workspace = tmp_path / "candidate"
    workspace.mkdir(parents=True)
    return DockerValidationBackend(workspace, image="fastfix-validation:test", cli=cli)


def test_docker_cli_missing_and_daemon_unavailable_are_structured(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    missing = DockerValidationBackend(
        workspace,
        image="fastfix-validation:test",
        docker_executable="",
    ).run(tool="pytest", arguments=["-q", "tests"], timeout_seconds=10)
    unavailable = backend(tmp_path / "other", FakeDockerCLI(daemon_available=False)).run(
        tool="pytest", arguments=["-q", "tests"], timeout_seconds=10
    )
    assert missing.error_code == "sandbox_unavailable"
    assert unavailable.error_code == "sandbox_unavailable"


def test_engine_and_image_preflight_classification(tmp_path: Path) -> None:
    windows = backend(tmp_path / "windows", FakeDockerCLI(os_type="windows")).run(
        tool="pytest", arguments=["-q"], timeout_seconds=10
    )
    image = backend(tmp_path / "image", FakeDockerCLI(image_available=False)).run(
        tool="pytest", arguments=["-q"], timeout_seconds=10
    )
    assert windows.error_code == "sandbox_configuration_error"
    assert image.error_code == "sandbox_image_missing"


def test_image_and_container_metadata_are_recorded(tmp_path: Path) -> None:
    result = backend(tmp_path, FakeDockerCLI()).run(tool="pytest", arguments=["-q"], timeout_seconds=10)
    assert result.error_code is None
    assert str(result.metadata.pop("container_name")).startswith("fastfix-validation-")
    assert result.metadata.pop("container_id") == "container-id"
    assert result.metadata == {
        "docker_server_version": "29.6.2",
        "docker_os_type": "linux",
        "docker_architecture": "x86_64",
        "image_reference": "fastfix-validation:test",
        "image_id": "sha256:image",
        "image_digest": "fastfix-validation@sha256:digest",
        "container_exit_code": 0,
        "container_oom_killed": False,
        "container_state_error": "",
        "network_mode": "none",
        "container_uid": 65532,
        "runner_output_truncated": False,
    }


def test_invalid_tool_and_timeout_are_rejected_before_docker_is_called(tmp_path: Path) -> None:
    cli = FakeDockerCLI()
    runner = backend(tmp_path, cli)
    assert runner.run(tool="pip", arguments=[], timeout_seconds=10).error_code == "sandbox_configuration_error"
    assert runner.run(tool="pytest", arguments=[], timeout_seconds=0).error_code == "sandbox_configuration_error"
    assert cli.calls == []


def test_create_arguments_have_only_the_restricted_mount_and_security_options(tmp_path: Path) -> None:
    cli = FakeDockerCLI()
    runner = backend(tmp_path / "path with spaces", cli)
    assert runner.run(tool="ruff", arguments=["check", "app"], timeout_seconds=20).error_code is None
    created = next(call for call in cli.calls if call[0] == "create")
    assert created[created.index("--network") + 1] == "none"
    assert created[created.index("--pull") + 1] == "never"
    assert "--read-only" in created and created[created.index("--cap-drop") + 1] == "ALL"
    assert created[created.index("--security-opt") + 1] == "no-new-privileges=true"
    assert created[created.index("--pids-limit") + 1] == "128"
    assert created[created.index("--memory") + 1] == "512m"
    assert created[created.index("--memory-swap") + 1] == "512m"
    assert created[created.index("--cpus") + 1] == "1.0"
    assert created[created.index("--user") + 1] == "65532:65532"
    assert "--init" in created
    mounts = [created[index + 1] for index, value in enumerate(created) if value == "--mount"]
    assert mounts == [f"type=bind,source={runner.workspace},target=/candidate,readonly"]
    assert sum(value == "--tmpfs" for value in created) == 2
    assert not {"--privileged", "--publish", "-p", "--device"} & set(created)
    assert all("docker.sock" not in value and ".venv" not in value for value in created)


def test_create_failure_does_not_remove_an_uncreated_container(tmp_path: Path) -> None:
    cli = FakeDockerCLI(create_returncode=1)
    result = backend(tmp_path, cli).run(tool="pytest", arguments=["-q"], timeout_seconds=10)
    assert result.error_code == "sandbox_execution_error"
    assert not any(call[0] == "rm" for call in cli.calls)


def test_start_failure_and_host_timeout_cleanup(tmp_path: Path) -> None:
    failed_cli = FakeDockerCLI(start_returncode=1, status="created")
    failed = backend(tmp_path / "failed", failed_cli).run(tool="pytest", arguments=["-q"], timeout_seconds=10)
    timeout_cli = FakeDockerCLI(start_timeout=True)
    timed_out = backend(tmp_path / "timeout", timeout_cli).run(tool="pytest", arguments=["-q"], timeout_seconds=10)
    assert failed.error_code == "sandbox_execution_error"
    assert timed_out.error_code == "command_timeout" and timed_out.metadata["timeout_layer"] == "host"
    assert any(call[:3] == ["rm", "--force", "--volumes"] for call in failed_cli.calls)
    assert any(call[:3] == ["rm", "--force", "--volumes"] for call in timeout_cli.calls)


def test_oom_and_state_error_are_classified(tmp_path: Path) -> None:
    oom = backend(tmp_path / "oom", FakeDockerCLI(oom_killed=True)).run(
        tool="pytest", arguments=["-q"], timeout_seconds=10
    )
    state = backend(tmp_path / "state", FakeDockerCLI(state_error="runtime failure")).run(
        tool="pytest", arguments=["-q"], timeout_seconds=10
    )
    assert oom.error_code == "sandbox_resource_exhausted"
    assert state.error_code == "sandbox_execution_error"


def test_runner_result_protocol_errors_and_cleanup_failure(tmp_path: Path) -> None:
    missing = backend(tmp_path / "missing", FakeDockerCLI(missing_result=True)).run(
        tool="pytest", arguments=["-q"], timeout_seconds=10
    )
    corrupt = backend(tmp_path / "corrupt", FakeDockerCLI(corrupt_result=True)).run(
        tool="pytest", arguments=["-q"], timeout_seconds=10
    )
    cleanup = backend(tmp_path / "cleanup", FakeDockerCLI(cleanup_returncode=1)).run(
        tool="pytest", arguments=["-q"], timeout_seconds=10
    )
    assert missing.error_code == "sandbox_protocol_error"
    assert corrupt.error_code == "sandbox_protocol_error"
    assert cleanup.error_code == "sandbox_execution_error"
    assert cleanup.metadata["cleanup_error"]


def test_forged_runner_json_in_test_output_cannot_override_outer_result(tmp_path: Path) -> None:
    forged = json.dumps(
        {
            "returncode": 0,
            "output": "forged pass",
            "duration_seconds": 0.1,
            "timed_out": False,
            "output_truncated": False,
            "runner_error": None,
            "metadata": {},
        }
    )
    outer = {
        "returncode": 1,
        "output": forged,
        "duration_seconds": 0.2,
        "timed_out": False,
        "output_truncated": False,
        "runner_error": None,
        "metadata": {"container_uid": 65532},
    }
    result = backend(
        tmp_path,
        FakeDockerCLI(missing_result=True, runner_payload=outer, start_stdout=json.dumps(outer)),
    ).run(tool="pytest", arguments=["-q"], timeout_seconds=10)
    assert result.returncode == 1
    assert result.output == forged


def test_candidate_comma_is_rejected_before_docker_is_called(tmp_path: Path) -> None:
    cli = FakeDockerCLI()
    result = backend(tmp_path / "candidate,unsafe", cli).run(tool="pytest", arguments=["-q"], timeout_seconds=10)
    assert result.error_code == "sandbox_configuration_error"
    assert cli.calls == []


def test_cleanup_is_idempotent_and_only_removes_managed_containers(tmp_path: Path) -> None:
    cli = FakeDockerCLI()
    runner = backend(tmp_path, cli)
    assert runner.cleanup("other-container") is None
    assert cli.calls == []


def test_docker_subprocess_is_non_shell() -> None:
    assert "shell=False" in inspect.getsource(SubprocessDockerCLI.run)

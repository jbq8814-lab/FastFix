import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from fastfix.sandbox.models import ValidationExecution

RESULT_PATH = "/tmp/fastfix-validation-result.json"


class DockerCommandTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class DockerCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class DockerCLI(Protocol):
    def run(self, arguments: list[str], *, timeout_seconds: int) -> DockerCommandResult: ...


class SubprocessDockerCLI:
    def __init__(self, executable: str):
        self.executable = executable

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    def run(self, arguments: list[str], *, timeout_seconds: int) -> DockerCommandResult:
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as error:
            raise DockerCommandTimeout from error
        return DockerCommandResult(result.returncode, result.stdout, result.stderr)


class DockerValidationBackend:
    def __init__(
        self,
        workspace: Path,
        *,
        image: str,
        cli: DockerCLI | None = None,
        docker_executable: str | None = None,
        host_timeout_grace_seconds: int = 30,
    ):
        self.workspace = workspace.resolve()
        self.image = image
        if host_timeout_grace_seconds < 0:
            raise ValueError("host_timeout_grace_seconds must not be negative")
        self.host_timeout_grace_seconds = host_timeout_grace_seconds
        executable = (
            (docker_executable if docker_executable is not None else shutil.which("docker")) if cli is None else None
        )
        self.cli = cli or (SubprocessDockerCLI(executable) if executable else None)
        self._managed_containers: set[str] = set()

    @staticmethod
    def _failure(
        error_code: str,
        *,
        output: str = "",
        metadata: dict[str, object] | None = None,
        timed_out: bool = False,
        duration_seconds: float = 0.0,
    ) -> ValidationExecution:
        return ValidationExecution(
            returncode=124 if timed_out else 125,
            output=output,
            duration_seconds=duration_seconds,
            timed_out=timed_out,
            error_code=error_code,
            metadata=metadata or {},
        )

    def _command(
        self,
        arguments: list[str],
        *,
        timeout_seconds: int = 30,
    ) -> DockerCommandResult:
        if self.cli is None:
            raise FileNotFoundError
        return self.cli.run(arguments, timeout_seconds=timeout_seconds)

    def _preflight(self) -> tuple[dict[str, object], ValidationExecution | None]:
        if self.cli is None:
            return {}, self._failure("sandbox_unavailable", output="Docker CLI was not found.")
        try:
            version = self._command(["version", "--format", "{{json .Server}}"])
        except (DockerCommandTimeout, OSError):
            return {}, self._failure("sandbox_unavailable", output="Docker daemon is unavailable.")
        if version.returncode:
            return {}, self._failure("sandbox_unavailable", output="Docker daemon is unavailable.")
        try:
            server = json.loads(version.stdout)
        except json.JSONDecodeError:
            return {}, self._failure("sandbox_unavailable", output="Docker daemon returned invalid metadata.")
        if not isinstance(server, dict):
            return {}, self._failure("sandbox_unavailable", output="Docker daemon returned invalid metadata.")
        metadata: dict[str, object] = {
            "docker_server_version": server.get("Version"),
            "docker_os_type": server.get("Os"),
            "docker_architecture": server.get("Arch"),
            "image_reference": self.image,
        }
        if server.get("Os") != "linux":
            return metadata, self._failure(
                "sandbox_configuration_error",
                output="Docker Engine must use Linux containers.",
                metadata=metadata,
            )
        try:
            inspected = self._command(["image", "inspect", self.image])
        except (DockerCommandTimeout, OSError):
            return metadata, self._failure("sandbox_unavailable", output="Unable to inspect Docker image.")
        if inspected.returncode:
            return metadata, self._failure(
                "sandbox_image_missing",
                output="Configured validation image is not available locally.",
                metadata=metadata,
            )
        try:
            image = json.loads(inspected.stdout)[0]
        except (json.JSONDecodeError, IndexError, TypeError):
            return metadata, self._failure(
                "sandbox_protocol_error",
                output="Docker image inspect returned invalid metadata.",
                metadata=metadata,
            )
        if not isinstance(image, dict):
            return metadata, self._failure(
                "sandbox_protocol_error",
                output="Docker image inspect returned invalid metadata.",
                metadata=metadata,
            )
        metadata |= {
            "image_id": image.get("Id"),
            "image_digest": (image.get("RepoDigests") or [None])[0],
        }
        return metadata, None

    def _create_arguments(
        self,
        *,
        container_name: str,
        tool: Literal["pytest", "ruff"],
        arguments: list[str],
        timeout_seconds: int,
    ) -> list[str]:
        mount = f"type=bind,source={self.workspace},target=/candidate,readonly"
        return [
            "create",
            "--name",
            container_name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "1.0",
            "--user",
            "65532:65532",
            "--init",
            "--mount",
            mount,
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            self.image,
            "--timeout-seconds",
            str(timeout_seconds),
            tool,
            "--",
            *arguments,
        ]

    def cleanup(self, container_name: str) -> str | None:
        if container_name not in self._managed_containers:
            return None
        try:
            result = self._command(["rm", "--force", "--volumes", container_name])
        except (DockerCommandTimeout, OSError):
            return "Unable to remove validation container."
        if result.returncode:
            return "Unable to remove validation container."
        self._managed_containers.discard(container_name)
        return None

    @staticmethod
    def _state(inspect_output: str) -> dict[str, object] | None:
        try:
            return json.loads(inspect_output)[0]
        except (json.JSONDecodeError, IndexError, TypeError):
            return None

    def _read_runner_result(
        self,
        *,
        container_name: str,
        destination: Path,
        attached_output: str,
        metadata: dict[str, object],
    ) -> ValidationExecution:
        copied = self._command(["cp", f"{container_name}:{RESULT_PATH}", str(destination)])
        if copied.returncode or not destination.is_file():
            raw_payload = attached_output.strip()
        else:
            try:
                raw_payload = destination.read_text(encoding="utf-8")
            except OSError:
                raw_payload = ""
        if not raw_payload:
            return self._failure(
                "sandbox_protocol_error",
                output="Validation runner result is missing.",
                metadata=metadata,
            )
        try:
            payload = json.loads(raw_payload)
            returncode = int(payload["returncode"])
            output = str(payload["output"])
            duration_seconds = float(payload["duration_seconds"])
            timed_out = bool(payload["timed_out"])
            output_truncated = bool(payload["output_truncated"])
            runner_error = payload["runner_error"]
            runner_metadata = dict(payload.get("metadata", {}))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return self._failure(
                "sandbox_protocol_error",
                output="Validation runner result is invalid.",
                metadata=metadata,
            )
        metadata |= runner_metadata | {"runner_output_truncated": output_truncated}
        if runner_error is not None:
            return self._failure(
                "sandbox_execution_error",
                output="Validation runner failed.",
                metadata=metadata,
                duration_seconds=duration_seconds,
            )
        return ValidationExecution(
            returncode=returncode,
            output=output,
            duration_seconds=duration_seconds,
            timed_out=timed_out,
            error_code="command_timeout" if timed_out else None,
            metadata=metadata,
        )

    def run(
        self,
        *,
        tool: Literal["pytest", "ruff"],
        arguments: list[str],
        timeout_seconds: int,
    ) -> ValidationExecution:
        start = time.monotonic()
        if tool not in {"pytest", "ruff"} or timeout_seconds <= 0:
            return self._failure(
                "sandbox_configuration_error",
                output="Docker validation requires pytest or ruff and a positive timeout.",
            )
        if not self.workspace.is_dir():
            return self._failure("sandbox_configuration_error", output="Candidate workspace does not exist.")
        if "," in str(self.workspace):
            return self._failure(
                "sandbox_configuration_error",
                output="Candidate paths containing commas cannot be mounted safely.",
            )
        metadata, failure = self._preflight()
        if failure is not None:
            return failure
        container_name = f"fastfix-validation-{uuid4().hex}"
        execution: ValidationExecution | None = None
        cleanup_error: str | None = None
        try:
            created = self._command(
                self._create_arguments(
                    container_name=container_name,
                    tool=tool,
                    arguments=arguments,
                    timeout_seconds=timeout_seconds,
                )
            )
            if created.returncode:
                return self._failure(
                    "sandbox_execution_error",
                    output="Unable to create validation container.",
                    metadata=metadata,
                    duration_seconds=time.monotonic() - start,
                )
            self._managed_containers.add(container_name)
            metadata |= {"container_name": container_name, "container_id": created.stdout.strip()}
            try:
                started = self._command(
                    ["start", "--attach", container_name],
                    timeout_seconds=timeout_seconds + self.host_timeout_grace_seconds,
                )
            except DockerCommandTimeout:
                execution = self._failure(
                    "command_timeout",
                    output="Host timed out waiting for validation container.",
                    metadata=metadata | {"timeout_layer": "host"},
                    timed_out=True,
                    duration_seconds=time.monotonic() - start,
                )
            if execution is None:
                inspected = self._command(["inspect", container_name])
                container = self._state(inspected.stdout) if not inspected.returncode else None
                if container is None:
                    execution = self._failure(
                        "sandbox_execution_error",
                        output="Unable to inspect validation container state.",
                        metadata=metadata,
                        duration_seconds=time.monotonic() - start,
                    )
                else:
                    state_value = container.get("State")
                    host_config = container.get("HostConfig")
                    if not isinstance(state_value, dict) or not isinstance(host_config, dict):
                        execution = self._failure(
                            "sandbox_protocol_error",
                            output="Docker container inspect returned invalid metadata.",
                            metadata=metadata,
                            duration_seconds=time.monotonic() - start,
                        )
                        state_value = {}
                        host_config = {}
                    state = dict(state_value)
                    metadata |= {
                        "container_exit_code": state.get("ExitCode"),
                        "container_oom_killed": state.get("OOMKilled"),
                        "container_state_error": state.get("Error"),
                        "network_mode": host_config.get("NetworkMode"),
                    }
                    if execution is None:
                        if state.get("OOMKilled"):
                            execution = self._failure(
                                "sandbox_resource_exhausted",
                                output="Validation container exceeded its memory limit.",
                                metadata=metadata,
                                duration_seconds=time.monotonic() - start,
                            )
                        elif state.get("Error"):
                            execution = self._failure(
                                "sandbox_execution_error",
                                output="Validation container reported a runtime error.",
                                metadata=metadata,
                                duration_seconds=time.monotonic() - start,
                            )
                        elif started.returncode and state.get("Status") != "exited":
                            execution = self._failure(
                                "sandbox_execution_error",
                                output="Unable to start validation container.",
                                metadata=metadata,
                                duration_seconds=time.monotonic() - start,
                            )
                        else:
                            with tempfile.TemporaryDirectory(prefix="fastfix-validation-result-") as temporary:
                                execution = self._read_runner_result(
                                    container_name=container_name,
                                    destination=Path(temporary) / "result.json",
                                    attached_output=started.stdout,
                                    metadata=metadata,
                                )
        except (DockerCommandTimeout, OSError):
            execution = self._failure(
                "sandbox_execution_error",
                output="Docker validation execution failed.",
                metadata=metadata,
                duration_seconds=time.monotonic() - start,
            )
        finally:
            cleanup_error = self.cleanup(container_name)
        if execution is None:
            execution = self._failure(
                "sandbox_execution_error",
                output="Docker validation did not produce a result.",
                metadata=metadata,
                duration_seconds=time.monotonic() - start,
            )
        if cleanup_error:
            execution.metadata["cleanup_error"] = cleanup_error
            if execution.error_code is None:
                execution.returncode = 125
                execution.error_code = "sandbox_execution_error"
        return execution

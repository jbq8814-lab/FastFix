import os
import subprocess
import time
from pathlib import Path
from typing import Literal

from fastfix.sandbox.models import ValidationExecution


class LocalValidationBackend:
    def __init__(self, workspace: Path, *, python_executable: Path):
        if not workspace.is_dir():
            raise ValueError("Workspace must be an existing directory.")
        if not python_executable.is_file():
            raise ValueError("python_executable must be an existing file.")
        self.workspace = workspace.resolve()
        self.python_executable = python_executable.resolve()

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        return environment | {"NO_COLOR": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    def run(
        self,
        *,
        tool: Literal["pytest", "ruff"],
        arguments: list[str],
        timeout_seconds: int,
    ) -> ValidationExecution:
        start = time.monotonic()
        try:
            result = subprocess.run(
                [str(self.python_executable), "-m", tool, *arguments],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                env=self._environment(),
            )
            return ValidationExecution(
                returncode=result.returncode,
                output=result.stdout + result.stderr,
                duration_seconds=time.monotonic() - start,
            )
        except subprocess.TimeoutExpired as error:
            return ValidationExecution(
                returncode=124,
                output=(error.stdout or "") + (error.stderr or ""),
                duration_seconds=time.monotonic() - start,
                timed_out=True,
                error_code="command_timeout",
            )

import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fastfix.sandbox.local import LocalValidationBackend
from fastfix.sandbox.models import ValidationBackend
from fastfix.security.paths import PathPolicyError, WorkspacePathPolicy
from fastfix.tools.models import ToolResult

MAX_OUTPUT_CHARS = 20_000
SAFE_TARGET = re.compile(r"^[A-Za-z0-9_./:\[\]-]+$")


class ValidationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunPytestArgs(ValidationArgs):
    scope: Literal["targeted", "regression"]
    targets: list[str] = Field(default_factory=list, max_length=20)
    timeout_seconds: int = Field(default=60, ge=5, le=180)

    @model_validator(mode="after")
    def validate_targeted_targets(self) -> "RunPytestArgs":
        if self.scope == "targeted" and not self.targets:
            raise ValueError("Targeted pytest requires at least one target.")
        return self


class RunRuffArgs(ValidationArgs):
    paths: list[str] = Field(default_factory=list, max_length=10)
    timeout_seconds: int = Field(default=60, ge=5, le=120)


class WorkspaceValidationTools:
    def __init__(
        self,
        workspace: Path,
        *,
        python_executable: Path | None = None,
        backend: ValidationBackend | None = None,
        source_paths: tuple[str, ...] = ("app",),
        test_paths: tuple[str, ...] = ("tests",),
    ):
        if not workspace.is_dir():
            raise ValueError("Workspace must be an existing directory.")
        if (python_executable is None) == (backend is None):
            raise ValueError("Provide exactly one of python_executable or backend.")
        self.workspace = workspace.resolve()
        self.python_executable = python_executable.resolve() if python_executable is not None else None
        self.backend = (
            backend if backend is not None else LocalValidationBackend(workspace, python_executable=python_executable)
        )
        self.source_policy = WorkspacePathPolicy(workspace, allowed_paths=source_paths)
        self.test_policy = WorkspacePathPolicy(workspace, allowed_paths=test_paths)
        self.source_paths = tuple(self._validate_source_path(path) for path in source_paths)
        self.test_paths = tuple(self._validate_test_target(path) for path in test_paths)

    @staticmethod
    def _bounded_output(output: str) -> tuple[str, bool]:
        if len(output) <= MAX_OUTPUT_CHARS:
            return output, False
        half = MAX_OUTPUT_CHARS // 2
        return f"{output[:half]}\n... output truncated ...\n{output[-half:]}", True

    @staticmethod
    def _count(output: str, label: str) -> int | None:
        matches = re.findall(rf"(\d+)\s+{label}\b", output)
        return int(matches[-1]) if matches else None

    def _validate_test_target(self, target: str) -> str:
        if target.startswith("-") or "\x00" in target or not SAFE_TARGET.fullmatch(target):
            raise PathPolicyError("invalid_arguments", "Unsafe pytest target.")
        path, separator, node = target.partition("::")
        if ".." in PurePosixPath(path).parts:
            raise PathPolicyError("invalid_arguments", "Unsafe pytest target.")
        relative = self.test_policy.to_relative(self.test_policy.resolve(path, must_exist=True))
        return f"{relative}::{node}" if separator else relative

    def run_pytest(self, arguments: BaseModel) -> ToolResult:
        args = RunPytestArgs.model_validate(arguments)
        targets = [self._validate_test_target(target) for target in args.targets]
        if args.scope == "regression":
            if targets and tuple(targets) != self.test_paths:
                raise PathPolicyError(
                    "validation_scope_incomplete",
                    "Regression pytest must use the configured complete targets.",
                )
            targets = list(self.test_paths)
        execution = self.backend.run(
            tool="pytest",
            arguments=["-q", *targets],
            timeout_seconds=args.timeout_seconds,
        )
        output, truncated = self._bounded_output(execution.output.replace(str(self.workspace), "."))
        return ToolResult(
            tool_name="run_pytest",
            ok=execution.returncode == 0 and execution.error_code is None,
            output=output,
            error_code=execution.error_code or (None if execution.returncode == 0 else "validation_failed"),
            metadata={
                **execution.metadata,
                "scope": args.scope,
                "targets": targets,
                **(
                    {
                        "required_targets": list(self.test_paths),
                        "scope_complete": tuple(targets) == self.test_paths,
                    }
                    if args.scope == "regression"
                    else {}
                ),
                "returncode": execution.returncode,
                "passed": self._count(execution.output, "passed"),
                "failed": self._count(execution.output, "failed"),
                "skipped": self._count(execution.output, "skipped"),
                "duration_seconds": round(execution.duration_seconds, 3),
                "timed_out": execution.timed_out,
                "output_truncated": truncated or bool(execution.metadata.get("runner_output_truncated")),
            },
        )

    def _validate_source_path(self, path: str) -> str:
        if path.startswith("-") or "\x00" in path or not SAFE_TARGET.fullmatch(path):
            raise PathPolicyError("invalid_arguments", "Unsafe Ruff path.")
        if ".." in PurePosixPath(path).parts:
            raise PathPolicyError("invalid_arguments", "Unsafe Ruff path.")
        return self.source_policy.to_relative(self.source_policy.resolve(path, must_exist=True))

    def run_ruff(self, arguments: BaseModel) -> ToolResult:
        args = RunRuffArgs.model_validate(arguments)
        paths = [self._validate_source_path(path) for path in args.paths]
        if paths and tuple(paths) != self.source_paths:
            raise PathPolicyError(
                "validation_scope_incomplete",
                "Ruff must use the configured complete source paths.",
            )
        paths = list(self.source_paths)
        execution = self.backend.run(
            tool="ruff",
            arguments=["check", *paths],
            timeout_seconds=args.timeout_seconds,
        )
        output, truncated = self._bounded_output(execution.output.replace(str(self.workspace), "."))
        return ToolResult(
            tool_name="run_ruff",
            ok=execution.returncode == 0 and execution.error_code is None,
            output=output,
            error_code=execution.error_code or (None if execution.returncode == 0 else "validation_failed"),
            metadata={
                **execution.metadata,
                "paths": paths,
                "required_paths": list(self.source_paths),
                "scope_complete": tuple(paths) == self.source_paths,
                "returncode": execution.returncode,
                "passed": execution.returncode == 0 and execution.error_code is None,
                "duration_seconds": round(execution.duration_seconds, 3),
                "timed_out": execution.timed_out,
                "output_truncated": truncated or bool(execution.metadata.get("runner_output_truncated")),
            },
        )

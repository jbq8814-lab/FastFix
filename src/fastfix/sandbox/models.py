from typing import Literal, Protocol

from pydantic import BaseModel, Field


class ValidationExecution(BaseModel):
    returncode: int
    output: str = ""
    duration_seconds: float
    timed_out: bool = False
    error_code: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class ValidationBackend(Protocol):
    def run(
        self,
        *,
        tool: Literal["pytest", "ruff"],
        arguments: list[str],
        timeout_seconds: int,
    ) -> ValidationExecution: ...

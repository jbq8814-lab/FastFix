"""AgentGuard 恢复控制接口的严格输入输出模型。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CommandName = Literal["status", "rerun-validation", "reopen-repair", "rollback"]


class ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosisContext(ControlModel):
    """AgentGuard 交付的受限诊断上下文：字段、长度与数量全部收紧。"""

    diagnosis_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    failure_category: str = Field(min_length=1, max_length=80, pattern=r"^[a-z_]+$")
    root_cause_summary: str = Field(min_length=1, max_length=1500)
    critical_failure_event_id: str | None = Field(default=None, max_length=80)
    evidence_event_ids: list[str] = Field(default_factory=list, max_length=8)
    cited_case_ids: list[str] = Field(default_factory=list, max_length=3)
    recovery_hint: str | None = Field(default=None, max_length=300)

    @field_validator("evidence_event_ids", "cited_case_ids")
    @classmethod
    def validate_ids(cls, ids: list[str]) -> list[str]:
        for item in ids:
            if not item or len(item) > 80:
                raise ValueError("context ids must be 1..80 chars")
        return ids


class ValidationSummary(ControlModel):
    pytest_returncode: int | None = None
    pytest_passed: int | None = None
    pytest_failed: int | None = None
    ruff_returncode: int | None = None
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.pytest_returncode == 0
            and self.ruff_returncode == 0
            and not self.timed_out
        )


class ControlResult(ControlModel):
    command: CommandName
    status: Literal["executed", "duplicate", "interrupted", "rejected", "failed"]
    idempotency_key: str | None = None
    session: str
    workspace: str | None = None
    message: str = Field(default="", max_length=500)
    validation: ValidationSummary | None = None
    changed_files: list[str] = Field(default_factory=list, max_length=32)
    trajectory_path: str | None = None
    diagnosis_context_path: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


SESSION_ID_PATTERN = r"^[A-Za-z0-9_.\-]+/sessions/[0-9a-fA-F-]{8,64}$"


def validate_session_reference(reference: str) -> str:
    """会话引用必须是 <experiment>/sessions/<uuid> 形式的相对路径。"""
    import re

    if not re.fullmatch(SESSION_ID_PATTERN, reference):
        raise ValueError(f"Invalid session reference: {reference!r}")
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Session reference must stay relative")
    return reference

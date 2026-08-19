from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class SubmitRepairArgs(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    root_cause: str = Field(min_length=1, max_length=2000)
    changed_files: list[str] = Field(min_length=1, max_length=10)
    tests_run: list[str] = Field(min_length=1, max_length=20)
    risk_notes: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("changed_files")
    @classmethod
    def validate_changed_files(cls, paths: list[str]) -> list[str]:
        unique = []
        for user_path in paths:
            path = Path(user_path)
            if not user_path or path.is_absolute() or ".." in path.parts:
                raise ValueError("changed_files must contain safe relative paths")
            normalized = path.as_posix()
            if normalized not in unique:
                unique.append(normalized)
        return unique


class ReopenRepairArgs(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, reason: str) -> str:
        if not reason.strip():
            raise ValueError("reason must not be blank")
        return reason.strip()


def get_submit_repair_tool() -> dict[str, object]:
    parameters = SubmitRepairArgs.model_json_schema()
    parameters.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": "submit_repair",
            "description": "Submit a repair only after all validation gates pass.",
            "parameters": parameters,
        },
    }


def get_reopen_repair_tool() -> dict[str, object]:
    parameters = ReopenRepairArgs.model_json_schema()
    parameters.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": "reopen_repair",
            "description": "Reopen a ready repair for further edits and invalidate its current validation.",
            "parameters": parameters,
        },
    }

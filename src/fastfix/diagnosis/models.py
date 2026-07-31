from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator


class EvidenceItem(BaseModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_line_range(self) -> "EvidenceItem":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class SubmitDiagnosisArgs(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    root_cause: str = Field(min_length=1, max_length=2000)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=10)
    suspected_files: list[str] = Field(min_length=1, max_length=10)
    recommended_fix: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("suspected_files")
    @classmethod
    def validate_suspected_files(cls, paths: list[str]) -> list[str]:
        unique = []
        for user_path in paths:
            path = Path(user_path)
            if not user_path or path.is_absolute() or ".." in path.parts:
                raise ValueError("suspected_files must contain safe relative paths")
            normalized = path.as_posix()
            if normalized not in unique:
                unique.append(normalized)
        return unique


class DiagnosisResult(SubmitDiagnosisArgs):
    tool_call_count: int
    tool_names: list[str]


def get_submit_diagnosis_tool() -> dict[str, object]:
    parameters = SubmitDiagnosisArgs.model_json_schema()
    parameters.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": "submit_diagnosis",
            "description": "Submit the final diagnosis after collecting sufficient repository evidence.",
            "parameters": parameters,
        },
    }

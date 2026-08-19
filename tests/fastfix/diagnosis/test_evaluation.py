import pytest
from pydantic import ValidationError

from fastfix.diagnosis.evaluation import evaluate_ff001_diagnosis
from fastfix.diagnosis.models import EvidenceItem, SubmitDiagnosisArgs, get_submit_diagnosis_tool


def diagnosis(**overrides) -> SubmitDiagnosisArgs:
    values = {
        "summary": "The route mishandles an asynchronous service result.",
        "root_cause": "The route returns the service coroutine.",
        "evidence": [
            {"path": "app/main.py", "start_line": 14, "end_line": 16, "reason": "Caller."},
            {"path": "app/service.py", "start_line": 4, "end_line": 6, "reason": "Async definition."},
        ],
        "suspected_files": ["app/main.py", "app/main.py"],
        "recommended_fix": "Await fetch_user before returning its value.",
        "confidence": 0.95,
    }
    values.update(overrides)
    return SubmitDiagnosisArgs.model_validate(values)


def test_models_validate_ranges_paths_and_schema() -> None:
    with pytest.raises(ValidationError):
        EvidenceItem(path="app/main.py", start_line=2, end_line=1, reason="bad")
    with pytest.raises(ValidationError):
        diagnosis(suspected_files=["../main.py"])
    assert diagnosis().suspected_files == ["app/main.py"]
    tool = get_submit_diagnosis_tool()
    assert tool["function"]["name"] == "submit_diagnosis"
    assert tool["function"]["parameters"]["type"] == "object"


def test_correct_diagnosis_passes_case_insensitively() -> None:
    result = evaluate_ff001_diagnosis(diagnosis(recommended_fix="AWAIT FETCH_USER before return."))
    assert result.diagnosis_correct
    assert result.reasons == []


@pytest.mark.parametrize(
    ("overrides", "failed_field"),
    [
        ({"suspected_files": ["app/service.py"]}, "suspected_file_correct"),
        (
            {"evidence": [{"path": "app/main.py", "start_line": 14, "end_line": 16, "reason": "Caller."}]},
            "evidence_file_coverage",
        ),
        ({"recommended_fix": "Call fetch_user differently."}, "recommended_fix_correct"),
    ],
)
def test_incomplete_diagnosis_fails(overrides: dict, failed_field: str) -> None:
    result = evaluate_ff001_diagnosis(diagnosis(**overrides))
    assert not result.diagnosis_correct
    assert failed_field in result.reasons


def test_missing_diagnosis_fails() -> None:
    result = evaluate_ff001_diagnosis(None)
    assert not result.submitted
    assert not result.diagnosis_correct

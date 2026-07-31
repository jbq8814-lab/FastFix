from pydantic import BaseModel

from fastfix.diagnosis.models import SubmitDiagnosisArgs


class DiagnosisEvaluation(BaseModel):
    submitted: bool
    suspected_file_correct: bool
    evidence_file_coverage: bool
    recommended_fix_correct: bool
    diagnosis_correct: bool
    reasons: list[str]


def evaluate_ff001_diagnosis(diagnosis: SubmitDiagnosisArgs | None) -> DiagnosisEvaluation:
    if diagnosis is None:
        return DiagnosisEvaluation(
            submitted=False,
            suspected_file_correct=False,
            evidence_file_coverage=False,
            recommended_fix_correct=False,
            diagnosis_correct=False,
            reasons=["No diagnosis was submitted."],
        )

    suspected_file_correct = "app/main.py" in diagnosis.suspected_files
    evidence_paths = {item.path for item in diagnosis.evidence}
    evidence_file_coverage = {"app/main.py", "app/service.py"}.issubset(evidence_paths)
    recommendation = diagnosis.recommended_fix.casefold()
    recommended_fix_correct = "await" in recommendation and "fetch_user" in recommendation
    checks = {
        "suspected_file_correct": suspected_file_correct,
        "evidence_file_coverage": evidence_file_coverage,
        "recommended_fix_correct": recommended_fix_correct,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return DiagnosisEvaluation(
        submitted=True,
        **checks,
        diagnosis_correct=all(checks.values()),
        reasons=reasons,
    )

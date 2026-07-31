from pydantic import BaseModel

from fastfix.repair.models import SubmitRepairArgs


class RepairEvaluation(BaseModel):
    submitted: bool
    diff_non_empty: bool
    changed_files_allowed: bool
    tests_unchanged: bool
    targeted_tests_passed: bool
    regression_tests_passed: bool
    ruff_passed: bool
    expected_change_present: bool
    resolved: bool
    reasons: list[str]


def evaluate_ff001_repair(
    *,
    submission: SubmitRepairArgs | None,
    patch: str,
    changed_files: list[str],
    targeted_passed: bool,
    regression_passed: bool,
    ruff_passed: bool,
) -> RepairEvaluation:
    checks = {
        "submitted": submission is not None,
        "diff_non_empty": bool(patch.strip()),
        "changed_files_allowed": bool(changed_files) and all(path.startswith("app/") for path in changed_files),
        "tests_unchanged": all(not path.startswith("tests/") for path in changed_files),
        "targeted_tests_passed": targeted_passed,
        "regression_tests_passed": regression_passed,
        "ruff_passed": ruff_passed,
        "expected_change_present": "await fetch_user" in " ".join(patch.casefold().split()),
    }
    return RepairEvaluation(
        **checks,
        resolved=all(checks.values()),
        reasons=[name for name, passed in checks.items() if not passed],
    )

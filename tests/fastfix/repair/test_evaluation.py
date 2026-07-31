from fastfix.repair.evaluation import evaluate_ff001_repair
from fastfix.repair.models import SubmitRepairArgs


def submission() -> SubmitRepairArgs:
    return SubmitRepairArgs(
        summary="Resolve route result.",
        root_cause="The route returned an unresolved asynchronous result.",
        changed_files=["app/main.py"],
        tests_run=["targeted", "regression", "ruff"],
        confidence=1.0,
    )


def test_complete_ff001_repair_is_resolved() -> None:
    result = evaluate_ff001_repair(
        submission=submission(),
        patch="+    return await fetch_user(user_id)\n",
        changed_files=["app/main.py"],
        targeted_passed=True,
        regression_passed=True,
        ruff_passed=True,
    )
    assert result.resolved and result.reasons == []


def test_each_required_repair_condition_is_enforced() -> None:
    result = evaluate_ff001_repair(
        submission=None,
        patch="",
        changed_files=["tests/test_users.py"],
        targeted_passed=False,
        regression_passed=False,
        ruff_passed=False,
    )
    assert not result.resolved
    assert set(result.reasons) == {
        "submitted",
        "diff_non_empty",
        "changed_files_allowed",
        "tests_unchanged",
        "targeted_tests_passed",
        "regression_tests_passed",
        "ruff_passed",
        "expected_change_present",
    }


def test_expected_change_is_case_and_whitespace_normalized() -> None:
    result = evaluate_ff001_repair(
        submission=submission(),
        patch="+ RETURN   AWAIT   FETCH_USER(user_id)\n",
        changed_files=["app/main.py"],
        targeted_passed=True,
        regression_passed=True,
        ruff_passed=True,
    )
    assert result.expected_change_present and result.resolved

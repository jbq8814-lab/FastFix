import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FF008 = ROOT / "benchmarks" / "results" / "ff-008-current-baseline" / "run-001"
FF014 = ROOT / "benchmarks" / "results" / "ff-014-current-baseline" / "run-001"


def payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_evidence(entry: dict) -> None:
    path = ROOT / entry["path"]
    assert path.is_file()
    assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
    assert sha256(path) == entry["sha256"]


def test_ff008_metric_erratum_preserves_and_reconciles_original_evidence() -> None:
    erratum = payload(FF008 / "metrics-erratum.json")
    assert {
        "schema_version",
        "record_type",
        "task_id",
        "attempt",
        "created_at",
        "erratum_type",
        "original_evidence",
        "confirmed_facts",
        "inferences_or_uncertainties",
        "fix_commit",
    } <= set(erratum)
    assert erratum["schema_version"] == "1.0"
    assert erratum["record_type"] == "historical_metric_erratum"
    assert erratum["task_id"] == "ff-008-uncommitted-user"
    assert erratum["attempt"] == "run-001"
    assert erratum["erratum_type"] == "patch_failure_counter_underreporting"
    for entry in erratum["original_evidence"]:
        assert_evidence(entry)
    summary = payload(FF008 / "summary.json")
    tool_calls = payload(FF008 / "tool-calls.json")
    failures = [call for call in tool_calls if call["error_code"] == "patch_apply_failed"]
    assert summary["patch_failures"] == erratum["confirmed_facts"]["summary_patch_failures"] == 0
    assert len(failures) >= erratum["confirmed_facts"]["minimum_observed_patch_failures"] == 1
    assert erratum["confirmed_facts"]["original_summary_preserved"] is True
    assert erratum["inferences_or_uncertainties"]
    assert re.fullmatch(r"[0-9a-f]{40}", erratum["fix_commit"])
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{erratum['fix_commit']}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
            shell=False,
        ).returncode
        == 0
    )


def test_ff014_amendment_limits_the_claim_to_fixture_behavior() -> None:
    amendment = payload(FF014 / "assessment-amendment.json")
    assert {
        "schema_version",
        "record_type",
        "task_id",
        "attempt",
        "created_at",
        "amendment_type",
        "original_assessment",
        "patch_evidence",
        "confirmed_facts",
        "semantic_scope",
        "inferences_or_uncertainties",
    } <= set(amendment)
    assert amendment["schema_version"] == "1.0"
    assert amendment["record_type"] == "assessment_amendment"
    assert amendment["task_id"] == "ff-014-awaiting-sync-service"
    assert amendment["attempt"] == "run-001"
    assert amendment["amendment_type"] == "semantic_scope_correction"
    assert_evidence(amendment["original_assessment"])
    for entry in amendment["patch_evidence"]:
        assert_evidence(entry)
    candidate = (FF014 / "patch.diff").read_text(encoding="utf-8")
    gold = (ROOT / "benchmarks" / "tasks" / "ff-014-awaiting-sync-service" / "gold.patch").read_text(encoding="utf-8")
    assert "-async def get_user" in candidate and "+def get_user" in candidate
    assert "-    return await user_service.get_user(user_id)" in candidate
    assert "+    return user_service.get_user(user_id)" in candidate
    assert " async def get_user" in gold
    assert "-async def get_user" not in gold and "+def get_user" not in gold
    facts = amendment["confirmed_facts"]
    assert facts["fixture_behavior_match"] is True
    assert facts["full_semantic_equivalence"] is None
    fixture_test = (ROOT / "tests" / "fastfix" / "test_ff014_fixture.py").read_text(encoding="utf-8")
    assert "tests/test_users.py::test_user_detail" in fixture_test
    assert '"3 passed"' in fixture_test
    assert all(term not in fixture_test for term in ("threadpool", "thread context", "execution scheduling"))
    assert amendment["inferences_or_uncertainties"]

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from benchmarks.scripts.build_aggregate_assessment import (
    EVALUATION_ROLE,
    EvidenceError,
    build_assessment,
    render_markdown,
    write_outputs,
)

ROOT = Path(__file__).resolve().parents[3]
JSON_PATH = ROOT / "benchmarks" / "results" / "aggregate-assessment.json"
MARKDOWN_PATH = JSON_PATH.with_suffix(".md")
EXPECTED_TASKS = [f"FF-{number:03d}" for number in range(3, 16)]
EXPECTED_VALIDATED = [
    "FF-003",
    "FF-004",
    "FF-005",
    "FF-006",
    "FF-009",
    "FF-010",
    "FF-011",
    "FF-012",
    "FF-013",
    "FF-014",
    "FF-015",
]


def payload() -> dict:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task(assessment: dict, task_id: str) -> dict:
    return next(item for item in assessment["task_results"] if item["task_id"] == task_id)


def copy_sources(assessment: dict, destination: Path) -> None:
    for entry in assessment["source_manifest"]:
        source = ROOT / entry["path"]
        target = destination / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def test_primary_universe_and_metric_are_fixed_and_evidence_computed() -> None:
    assessment = payload()
    metric = assessment["primary_metric"]
    assert assessment["assessment_kind"] == "development_frozen_aggregate"
    assert assessment["formal_benchmark"] is assessment["metric_eligible"] is False
    assert assessment["evaluation_role"] == EVALUATION_ROLE
    assert assessment["performance_conclusion"] is None
    assert assessment["task_universe"]["included_task_ids"] == EXPECTED_TASKS
    assert assessment["task_universe"]["task_count"] == 13
    assert {item["task_id"] for item in assessment["task_universe"]["excluded_tasks"]} == {"FF-001", "FF-002"}
    assert metric["name"] == "development_validated_candidate_rate"
    assert metric["numerator"] == 11
    assert metric["denominator"] == 13
    assert metric["exact_fraction"] == "11/13"
    assert metric["exact_decimal_notation"] == "0.(846153)"
    assert metric["display_percentage"] == "84.6%"
    assert metric["validated_candidate_task_ids"] == EXPECTED_VALIDATED
    assert [item["task_id"] for item in assessment["task_results"]] == EXPECTED_TASKS
    assert [item["task_id"] for item in assessment["task_results"] if item["validated_candidate"]] == EXPECTED_VALIDATED
    assert all(
        item["evaluation_role"] == EVALUATION_ROLE
        and item["metric_eligible"] is False
        and item["performance_conclusion"] is None
        and item["candidate_applied"] is False
        and item["single_run_no_rerun"] is True
        for item in assessment["task_results"]
    )


def test_failed_tasks_have_evidence_bounded_classifications() -> None:
    assessment = payload()
    ff007 = task(assessment, "FF-007")
    assert ff007["classification"] == "provider_confounded_incomplete"
    assert ff007["validated_candidate"] is False
    assert ff007["validation"] == {
        "revision": 1,
        "targeted": "passed",
        "regression": "incomplete",
        "ruff": "incomplete",
    }
    assert ff007["provider_failure"] == {
        "error_type": "BadGatewayError",
        "http_status": 502,
        "retries_exhausted": True,
        "retry_count": 25,
        "semantic_repair_failure_established": False,
    }
    assert ff007["approval_request_path"] is None
    ff008 = task(assessment, "FF-008")
    assert ff008["classification"] == "agent_closure_failure"
    assert ff008["validated_candidate"] is False
    assert ff008["core_repair_found"] is True
    assert ff008["validation"]["targeted"] == "passed_before_rollback"
    assert ff008["validation"]["regression"] == "passed_before_rollback"
    assert ff008["validation"]["ruff"] == "passed_before_rollback"
    assert ff008["pre_rollback_changed_files"] == ["app/database.py", "app/service.py"]
    assert ff008["changed_files"] == []
    assert ff008["rollback_cleaned_final_diff"] is True
    assert ff008["patch_failure_metric"] == {
        "historical_summary_value": 0,
        "minimum_confirmed_value": 1,
        "replacement_cumulative_value": None,
        "root_cause": "The historical rollback state transition reset total_patch_failures to zero.",
        "aggregation_rule": (
            "Do not interpret the historical patch_failures value of zero as the true cumulative failure count."
        ),
    }
    assert any(
        item["task_id"] == "FF-008"
        and item["type"] == "metrics_erratum"
        and item["path"].endswith("metrics-erratum.json")
        for item in assessment["errata_applied"]
    )


def test_ff009_run_state_and_evidence_limit_are_separate_and_explicit() -> None:
    assessment = payload()
    ff009 = task(assessment, "FF-009")
    assert ff009["run_final_state"] == "approval_pending"
    assert ff009["post_run_disposition"]["status"] == "rejected"
    assert ff009["post_run_disposition"]["timing"] == "after_run"
    assert ff009["post_run_disposition"]["context"] == "release_preparation_audit_closeout"
    assert ff009["post_run_disposition"]["decided_at"] == "2026-07-30T12:32:59.331073Z"
    limitation = next(item for item in assessment["limitations"] if item["id"] == "ff009_evidence_quality")
    assert limitation["canonical_gold_patch_directly_applicable"] is False
    assert limitation["metadata_counts"] == {
        "buggy": {"passed": 3, "failed": 1},
        "gold": {"passed": 4, "failed": 0},
    }
    assert limitation["frozen_execution_counts"] == {
        "buggy": {"passed": 4, "failed": 1},
        "gold": {"passed": 5, "failed": 0},
    }
    assert limitation["canonical_files_rewritten"] is False
    assert limitation["candidate_validation_impact"] == "none"


def test_ff014_amendment_replaces_the_broad_semantic_claim() -> None:
    assessment = payload()
    ff014 = task(assessment, "FF-014")
    assert ff014["semantic_scope"] == {
        "fixture_behavior_match": True,
        "full_semantic_equivalence": None,
        "amendment_path": ("benchmarks/results/ff-014-current-baseline/run-001/assessment-amendment.json"),
    }
    assert any(
        item["task_id"] == "FF-014"
        and item["type"] == "assessment_amendment"
        and item["path"] == ff014["amendment_path"]
        for item in assessment["errata_applied"]
    )
    limitation = next(item for item in assessment["limitations"] if item["id"] == "ff014_semantic_boundary")
    assert limitation["fixture_behavior_match"] is True
    assert limitation["full_semantic_equivalence"] is None


def test_sensitivity_analysis_is_secondary_post_hoc_and_not_the_resume_metric() -> None:
    sensitivity = payload()["sensitivity_analysis"]
    assert sensitivity == {
        "name": "exclude_provider_confounded_ff007",
        "level": "secondary",
        "analysis_type": "sensitivity analysis",
        "view": "post-hoc",
        "primary_result": False,
        "resume_primary_metric_allowed": False,
        "excluded_task_ids": ["FF-007"],
        "numerator": 11,
        "denominator": 12,
        "exact_fraction": "11/12",
        "display_percentage": "91.7%",
    }


def test_prohibited_claims_are_labels_only_not_aggregate_claims() -> None:
    assessment = payload()
    claims = assessment["prohibited_claims"]
    assert claims == [
        "benchmark pass rate",
        "resolved@1",
        "model success rate",
        "general repair rate",
        "production success rate",
    ]
    asserted_content = json.dumps(
        {
            "assessment_kind": assessment["assessment_kind"],
            "primary_metric": assessment["primary_metric"],
            "task_results": assessment["task_results"],
            "limitations": assessment["limitations"],
        },
        ensure_ascii=False,
    ).lower()
    assert all(claim not in asserted_content for claim in claims)
    markdown = MARKDOWN_PATH.read_text(encoding="utf-8")
    conclusion = markdown.split("##", maxsplit=1)[0].lower()
    assert all(claim not in conclusion for claim in claims)
    assert "## 禁止替换为的夸大表述" in markdown


def test_source_manifest_hashes_and_required_roles_are_complete() -> None:
    assessment = payload()
    manifest = {entry["path"]: entry for entry in assessment["source_manifest"]}
    assert list(manifest) == sorted(manifest)
    for path, entry in manifest.items():
        assert (ROOT / path).is_file()
        assert sha256(ROOT / path) == entry["sha256"]
        assert entry["task_ids"]
    for task_id in EXPECTED_TASKS:
        result = task(assessment, task_id)
        assert result["source_evidence"]
        assert all(sha256(ROOT / entry["path"]) == entry["sha256"] for entry in result["source_evidence"])
        experiment = f"ff-{int(task_id[-3:]):03d}-current-baseline"
        prefix = f"benchmarks/results/{experiment}"
        required = {
            f"{prefix}/protocol.json",
            f"{prefix}/run-001/summary.json",
            f"{prefix}/run-001/validation-summary.json",
            f"{prefix}/run-001/assessment.json",
            f"{prefix}/run-001/changed-files.txt",
        }
        assert required <= set(manifest)
    for task_id in EXPECTED_VALIDATED:
        experiment = f"ff-{int(task_id[-3:]):03d}-current-baseline"
        prefix = f"benchmarks/results/{experiment}/run-001"
        assert {f"{prefix}/patch.diff", f"{prefix}/approval-request.json"} <= set(manifest)
    assert any(path.endswith("ff-009-current-baseline/run-001/reject-audit.json") for path in manifest)
    assert any(path.endswith("ff-008-current-baseline/run-001/metrics-erratum.json") for path in manifest)
    assert any(path.endswith("ff-014-current-baseline/run-001/assessment-amendment.json") for path in manifest)


def test_tracked_outputs_match_the_builder_and_markdown_data_model() -> None:
    assessment = payload()
    assert assessment["generated_from_commit"] == "ae9a7ece3e0dac43b93054cdc62a11fa94e244fc"
    assert build_assessment() == assessment
    assert render_markdown(assessment) == MARKDOWN_PATH.read_text(encoding="utf-8")
    markdown = MARKDOWN_PATH.read_text(encoding="utf-8")
    assert "共 13 个" in markdown
    assert "11 个产生了" in markdown
    assert "`development_validated_candidate_rate=11/13`（84.6%）" in markdown
    assert markdown.count("| FF-") == 13


def test_two_builds_are_byte_identical_and_leave_sources_unchanged(tmp_path: Path) -> None:
    assessment = payload()
    before = {entry["path"]: sha256(ROOT / entry["path"]) for entry in assessment["source_manifest"]}
    first = write_outputs(output_dir=tmp_path / "first")
    second = write_outputs(output_dir=tmp_path / "second")
    assert first[0].read_bytes() == second[0].read_bytes() == JSON_PATH.read_bytes()
    assert first[1].read_bytes() == second[1].read_bytes() == MARKDOWN_PATH.read_bytes()
    assert sha256(first[0]) == sha256(second[0])
    assert sha256(first[1]) == sha256(second[1])
    assert {path: sha256(ROOT / path) for path in before} == before


def test_conflicting_source_evidence_fails_conservatively(tmp_path: Path) -> None:
    assessment = payload()
    copy_sources(assessment, tmp_path)
    summary_path = tmp_path / "benchmarks" / "results" / "ff-003-current-baseline" / "run-001" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["session_id"] = "00000000-0000-4000-8000-000000000000"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(EvidenceError, match="assessment identity mismatch"):
        build_assessment(tmp_path, assessment["generated_from_commit"])


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        (
            "benchmarks/results/ff-008-current-baseline/run-001/metrics-erratum.json",
            "Missing required evidence",
        ),
        (
            "benchmarks/results/ff-014-current-baseline/run-001/assessment-amendment.json",
            "Missing required evidence",
        ),
    ],
)
def test_required_erratum_or_amendment_cannot_be_omitted(
    tmp_path: Path,
    relative_path: str,
    message: str,
) -> None:
    assessment = payload()
    copy_sources(assessment, tmp_path)
    (tmp_path / relative_path).unlink()
    with pytest.raises(EvidenceError, match=message):
        build_assessment(tmp_path, assessment["generated_from_commit"])

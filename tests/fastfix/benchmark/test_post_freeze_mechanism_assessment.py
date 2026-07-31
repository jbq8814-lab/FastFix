import json
from pathlib import Path

from benchmarks.scripts.build_post_freeze_mechanism_assessment import (
    build_assessment,
    render_markdown,
    write_outputs,
)

ROOT = Path(__file__).resolve().parents[3]
JSON_PATH = ROOT / "benchmarks" / "results" / "post-freeze-mechanism-assessment.json"
MARKDOWN_PATH = ROOT / "benchmarks" / "results" / "post-freeze-mechanism-assessment.md"


def payload() -> dict:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def test_post_freeze_labels_keep_formal_benchmark_denominator_separate() -> None:
    assessment = payload()
    assert assessment["evaluation_role"] == "post_freeze_scripted_mechanism_evaluation"
    assert not assessment["formal_benchmark"] and not assessment["metric_eligible"]
    assert assessment["provider_calls"] == 0
    assert not assessment["frozen_run_001_replayed"] and not assessment["historical_tasks_rerun"]
    assert assessment["original_development_validated_candidate_rate"] == "11/13"
    assert not assessment["aggregate_denominator_merged"]


def test_scripted_scenarios_cover_lock_reopen_failure_and_projection() -> None:
    assessment = payload()
    scenarios = {scenario["id"]: scenario for scenario in assessment["scenarios"]}
    assert set(scenarios) == {
        "ff008_root_cause_scripted_reproduction",
        "reopen_invalidates_validation",
        "validation_failure_retention",
        "deterministic_context_projection",
    }
    assert scenarios["ff008_root_cause_scripted_reproduction"] == {
        "id": "ff008_root_cause_scripted_reproduction",
        "outcome": "submitted_after_post_validation_edit_was_locked",
        "submitted": True,
        "revision": 1,
        "candidate_content": "value = 2\n",
        "validation_current": True,
        "blocked_error_codes": ["repair_ready_locked", "repair_ready_locked"],
        "candidate_diff_unchanged_after_blocks": True,
        "show_diff_succeeded": True,
    }
    assert scenarios["reopen_invalidates_validation"]["premature_submit_error"] == "validation_incomplete"
    assert scenarios["reopen_invalidates_validation"]["reopen_count"] == 1
    assert scenarios["reopen_invalidates_validation"]["revision"] == 2
    assert scenarios["validation_failure_retention"]["state_card_failed_current"]
    assert scenarios["validation_failure_retention"]["active_context_retained_failure"]
    assert scenarios["deterministic_context_projection"]["trajectory_unchanged"]
    assert scenarios["deterministic_context_projection"]["tool_pairs_valid"]


def test_metrics_are_computed_from_scenario_counts() -> None:
    metrics = payload()["metrics"]
    assert metrics["scripted_scenarios"] == 4
    assert metrics["ready_state_illegal_action_attempts"] == 2
    assert metrics["ready_state_illegal_action_blocks"] == 2
    assert metrics["ready_state_illegal_action_block_rate"] == (
        metrics["ready_state_illegal_action_blocks"] / metrics["ready_state_illegal_action_attempts"]
    )
    assert metrics["stale_validation_exposure_count"] == 0
    assert metrics["state_card_policy_checks"] == 6
    assert metrics["state_card_policy_consistency_rate"] == 1.0
    assert metrics["required_context_retention_checks"] == 13
    assert metrics["required_context_retention_rate"] == 1.0
    assert metrics["projected_context_chars"] < metrics["raw_context_chars"]
    assert metrics["context_reduction_ratio"] == round(
        1 - metrics["projected_context_chars"] / metrics["raw_context_chars"],
        6,
    )
    assert metrics["context_character_scope"] == "cumulative_model_visible_characters_across_calls"
    assert metrics["model_call_count"] > 0
    assert metrics["max_raw_chars_per_call"] >= metrics["average_raw_chars_per_call"]
    assert metrics["max_projected_chars_per_call"] >= metrics["average_projected_chars_per_call"]
    assert metrics["configured_projection_limit"] == 80_000
    assert 0 <= metrics["calls_exceeding_projection_limit"] <= metrics["model_call_count"]


def test_tracked_outputs_match_builder() -> None:
    assessment = payload()
    assert build_assessment() == assessment
    assert render_markdown(assessment) == MARKDOWN_PATH.read_text(encoding="utf-8")


def test_two_builds_are_byte_identical(tmp_path: Path) -> None:
    first = write_outputs(tmp_path / "first")
    second = write_outputs(tmp_path / "second")
    assert [path.read_bytes() for path in first] == [path.read_bytes() for path in second]

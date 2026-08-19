import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import typer

ROOT = Path(__file__).resolve().parents[2]
TASK_NUMBERS = tuple(range(3, 16))
RUN_ID = "run-001"
EVALUATION_ROLE = "development_unseen_baseline"
PRIMARY_METRIC_NAME = "development_validated_candidate_rate"
PROHIBITED_CLAIMS = [
    "benchmark pass rate",
    "resolved@1",
    "model success rate",
    "general repair rate",
    "production success rate",
]


class EvidenceError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"Expected a JSON object: {path}")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact_decimal_notation(value: Fraction) -> str:
    integer, remainder = divmod(value.numerator, value.denominator)
    digits: list[str] = []
    seen: dict[int, int] = {}
    while remainder and remainder not in seen:
        seen[remainder] = len(digits)
        digit, remainder = divmod(remainder * 10, value.denominator)
        digits.append(str(digit))
    if not remainder:
        return f"{integer}.{''.join(digits)}"
    repeat_at = seen[remainder]
    return f"{integer}.{''.join(digits[:repeat_at])}({''.join(digits[repeat_at:])})"


def decimal_expansion(value: Fraction, places: int = 36) -> str:
    integer, remainder = divmod(value.numerator, value.denominator)
    digits: list[str] = []
    for _ in range(places):
        digit, remainder = divmod(remainder * 10, value.denominator)
        digits.append(str(digit))
        if not remainder:
            break
    return f"{integer}.{''.join(digits)}{'...' if remainder else ''}"


def display_percentage(value: Fraction) -> str:
    return f"{100 * value.numerator / value.denominator:.1f}%"


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def add_source(sources: dict[str, set[str]], root: Path, task_id: str, path: Path) -> None:
    require(path.is_file(), f"Missing required evidence: {relative(path, root)}")
    sources.setdefault(relative(path, root), set()).add(task_id)


def verify_reference(root: Path, reference: dict[str, Any]) -> None:
    path = root / reference["path"]
    require(path.is_file(), f"Missing referenced evidence: {reference['path']}")
    require(sha256(path) == reference["sha256"], f"Referenced evidence hash mismatch: {reference['path']}")


def validation_statuses(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": validation["revision"],
        "targeted": "passed" if validation["targeted"]["passed"] else "failed",
        "regression": "passed" if validation["regression"]["passed"] else "failed",
        "ruff": "passed" if validation["ruff"]["passed"] else "failed",
    }


def successful_task(
    root: Path,
    number: int,
    task_id: str,
    protocol: dict[str, Any],
    summary: dict[str, Any],
    validation: dict[str, Any],
    assessment: dict[str, Any],
    run: Path,
    sources: dict[str, set[str]],
) -> dict[str, Any]:
    request_path = run / "approval-request.json"
    patch_path = run / "patch.diff"
    request = load_json(request_path)
    for path in (request_path, patch_path):
        add_source(sources, root, task_id, path)
    statuses = validation_statuses(validation)
    require(set(statuses.values()) == {validation["revision"], "passed"}, f"{task_id}: validation did not pass")
    require(
        summary["validation_revision"] == request["validation_revision"] == validation["revision"],
        f"{task_id}: revision mismatch",
    )
    require(
        summary["targeted_tests_passed"]
        and summary["regression_tests_passed"]
        and summary["ruff_passed"]
        and request["targeted_tests_passed"]
        and request["regression_tests_passed"]
        and request["ruff_passed"],
        f"{task_id}: validation flags conflict",
    )
    changed_files = (run / "changed-files.txt").read_text(encoding="utf-8").splitlines()
    require(
        changed_files == summary["changed_files"] == request["changed_files"] == protocol["expected_changed_files"],
        f"{task_id}: changed-files conflict",
    )
    patch_hash = sha256(patch_path)
    require(
        patch_hash == request["patch_sha256"] == assessment["patch_sha256"],
        f"{task_id}: Candidate patch hash conflict",
    )
    require(summary["status"] == "approval_pending" and summary["approval_pending"], f"{task_id}: run was not pending")
    require(request["status"] == "pending", f"{task_id}: approval request was not pending")
    require(
        assessment["outcome"] == "resolved_candidate"
        and assessment["resolved_candidate"]
        and assessment["submitted"]
        and assessment["decision"] == "reject",
        f"{task_id}: assessment outcome conflict",
    )
    require(
        request["request_id"] == summary["approval_request_id"] == assessment["approval_request_id"],
        f"{task_id}: approval request ID conflict",
    )
    disposition = {
        "status": "rejected",
        "timing": "after_run",
        "action": assessment["approval_action"],
        "evidence_path": relative(run / "assessment.json", root),
    }
    if number == 9:
        audit_path = run / "reject-audit.json"
        decision_path = run / "reject-decision.json"
        manifest_path = run / "approval-package-manifest.json"
        for path in (audit_path, decision_path, manifest_path):
            add_source(sources, root, task_id, path)
        audit = load_json(audit_path)
        require(
            audit["task_id"] == summary["task_id"]
            and audit["run_id"] == RUN_ID
            and audit["session_id"] == summary["session_id"],
            f"{task_id}: Reject audit identity conflict",
        )
        require(
            audit["runtime_status_before_closeout"] == summary["status"] == "approval_pending"
            and audit["runtime_status_after_closeout"] == "rejected"
            and not audit["candidate_applied"],
            f"{task_id}: Reject audit state conflict",
        )
        for key in ("approval_request", "candidate_patch", "approval_package_manifest", "decision_record"):
            reference = audit[key]
            path = run / reference["path"]
            require(
                sha256(path) == reference["sha256"], f"{task_id}: Reject audit hash conflict for {reference['path']}"
            )
        disposition |= {
            "context": "release_preparation_audit_closeout",
            "decided_at": audit["decision_record"]["decided_at"],
            "evidence_path": relative(audit_path, root),
        }
    return {
        "classification": "validated_candidate",
        "validated_candidate": True,
        "validation": statuses,
        "changed_files": changed_files,
        "patch_sha256": patch_hash,
        "approval_request_path": relative(request_path, root),
        "run_final_state": summary["status"],
        "post_run_disposition": disposition,
    }


def failed_task(
    root: Path,
    number: int,
    task_id: str,
    summary: dict[str, Any],
    validation: dict[str, Any],
    assessment: dict[str, Any],
    run: Path,
    sources: dict[str, set[str]],
    errata_applied: list[dict[str, Any]],
) -> dict[str, Any]:
    failure_path = run / "failure.json"
    tool_calls_path = run / "tool-calls.json"
    for path in (failure_path, tool_calls_path):
        add_source(sources, root, task_id, path)
    failure = load_json(failure_path)
    tool_calls = json.loads(tool_calls_path.read_text(encoding="utf-8"))
    require(not summary["submitted"] and not summary["resolved_candidate"], f"{task_id}: unexpected Candidate")
    require(not (run / "approval-request.json").exists(), f"{task_id}: unexpected approval request")
    require(assessment["performance_conclusion"] is None, f"{task_id}: unexpected performance conclusion")
    base = {
        "validated_candidate": False,
        "changed_files": summary["changed_files"],
        "patch_sha256": None,
        "approval_request_path": None,
        "run_final_state": summary["status"],
        "post_run_disposition": {
            "status": "none",
            "timing": "not_applicable",
            "action": assessment.get("approval_action"),
            "evidence_path": relative(run / "assessment.json", root),
        },
    }
    if number == 7:
        require(
            summary["exit_status"] == failure["exit_status"] == "BadGatewayError"
            and summary["last_provider_error_type"] == "BadGatewayError"
            and summary["last_provider_status_code"] == 502
            and summary["provider_failure_exhausted"]
            and summary["provider_retry_count"] == assessment["provider_retry_count"] == 25,
            f"{task_id}: Provider failure evidence conflict",
        )
        require(
            validation["targeted_tests_passed"]
            and not validation["regression_tests_passed"]
            and not validation["ruff_passed"],
            f"{task_id}: incomplete validation evidence conflict",
        )
        return base | {
            "classification": "provider_confounded_incomplete",
            "validation": {
                "revision": validation["validation_revision"],
                "targeted": "passed",
                "regression": "incomplete",
                "ruff": "incomplete",
            },
            "provider_failure": {
                "error_type": "BadGatewayError",
                "http_status": 502,
                "retries_exhausted": True,
                "retry_count": 25,
                "semantic_repair_failure_established": False,
            },
        }
    require(number == 8, f"{task_id}: unclassified failed task")
    erratum_path = run / "metrics-erratum.json"
    add_source(sources, root, task_id, erratum_path)
    erratum = load_json(erratum_path)
    require(
        erratum["task_id"] == summary["task_id"]
        and erratum["attempt"] == RUN_ID
        and erratum["erratum_type"] == "patch_failure_counter_underreporting",
        f"{task_id}: metric erratum identity conflict",
    )
    for reference in erratum["original_evidence"]:
        verify_reference(root, reference)
    successful_targeted = [
        call
        for call in tool_calls
        if call["tool_name"] == "run_pytest" and call["ok"] and call["metadata"]["scope"] == "targeted"
    ]
    successful_regression = [
        call
        for call in tool_calls
        if call["tool_name"] == "run_pytest" and call["ok"] and call["metadata"]["scope"] == "regression"
    ]
    successful_ruff = [call for call in tool_calls if call["tool_name"] == "run_ruff" and call["ok"]]
    patch_failures = [call for call in tool_calls if call["error_code"] == "patch_apply_failed"]
    rollbacks = [call for call in tool_calls if call["tool_name"] == "rollback_changes" and call["ok"]]
    require(
        successful_targeted and successful_regression and successful_ruff, f"{task_id}: prior validation pass missing"
    )
    require(
        len(patch_failures) >= erratum["confirmed_facts"]["minimum_observed_patch_failures"]
        and summary["patch_failures"] == erratum["confirmed_facts"]["summary_patch_failures"] == 0,
        f"{task_id}: metric erratum facts conflict",
    )
    require(
        rollbacks and rollbacks[-1]["metadata"]["clean"] and not summary["changed_files"],
        f"{task_id}: rollback evidence conflict",
    )
    diff_calls = [call for call in tool_calls if call["tool_name"] == "show_git_diff" and call["ok"]]
    require(diff_calls, f"{task_id}: pre-rollback diff missing")
    errata_applied.append(
        {
            "task_id": task_id,
            "type": "metrics_erratum",
            "path": relative(erratum_path, root),
            "sha256": sha256(erratum_path),
        }
    )
    return base | {
        "classification": "agent_closure_failure",
        "validation": {
            "revision": validation["validation_revision"],
            "targeted": "passed_before_rollback",
            "regression": "passed_before_rollback",
            "ruff": "passed_before_rollback",
            "final_recorded_flags": {
                "targeted": validation["targeted_tests_passed"],
                "regression": validation["regression_tests_passed"],
                "ruff": validation["ruff_passed"],
            },
        },
        "pre_rollback_changed_files": diff_calls[-1]["metadata"]["changed_files"],
        "core_repair_found": "flush() -> db.commit()" in assessment["explanation"],
        "rollback_cleaned_final_diff": True,
        "patch_failure_metric": {
            "historical_summary_value": summary["patch_failures"],
            "minimum_confirmed_value": erratum["confirmed_facts"]["minimum_observed_patch_failures"],
            "replacement_cumulative_value": None,
            "root_cause": erratum["confirmed_facts"]["root_cause"],
            "aggregation_rule": erratum["confirmed_facts"]["aggregation_rule"],
        },
        "erratum_path": relative(erratum_path, root),
    }


def task_result(
    root: Path,
    number: int,
    sources: dict[str, set[str]],
    errata_applied: list[dict[str, Any]],
) -> dict[str, Any]:
    task_id = f"FF-{number:03d}"
    experiment = f"ff-{number:03d}-current-baseline"
    result = root / "benchmarks" / "results" / experiment
    run = result / RUN_ID
    protocol_path = result / "protocol.json"
    summary_path = run / "summary.json"
    validation_path = run / "validation-summary.json"
    assessment_path = run / "assessment.json"
    changed_files_path = run / "changed-files.txt"
    for path in (protocol_path, summary_path, validation_path, assessment_path, changed_files_path):
        add_source(sources, root, task_id, path)
    require(
        [path.name for path in result.glob("run-*") if path.is_dir()] == [RUN_ID], f"{task_id}: attempt set changed"
    )
    protocol = load_json(protocol_path)
    summary = load_json(summary_path)
    validation = load_json(validation_path)
    assessment = load_json(assessment_path)
    require(protocol["task_id"].startswith(task_id.lower()), f"{task_id}: Protocol task ID mismatch")
    require(
        summary["task_id"] == protocol["task_id"] and summary["run_id"] == RUN_ID, f"{task_id}: run identity mismatch"
    )
    require(
        protocol["result_labels"]["evaluation_role"] == summary["evaluation_role"] == EVALUATION_ROLE,
        f"{task_id}: evaluation role mismatch",
    )
    require(
        protocol["result_labels"]["metric_eligible"] is summary["metric_eligible"] is False,
        f"{task_id}: metric eligibility mismatch",
    )
    if "task_id" in assessment:
        require(
            assessment["task_id"] == summary["task_id"]
            and assessment["run_id"] == RUN_ID
            and assessment["session_id"] == summary["session_id"]
            and assessment["evaluation_role"] == EVALUATION_ROLE
            and assessment["metric_eligible"] is False,
            f"{task_id}: assessment identity mismatch",
        )
    require(assessment["performance_conclusion"] is None, f"{task_id}: performance conclusion must be null")
    require(
        assessment["candidate_cleaned"]
        and assessment["source_unchanged"]
        and assessment["canonical_fixture_unchanged"],
        f"{task_id}: immutable source guarantees missing",
    )
    if summary["resolved_candidate"]:
        evidence = successful_task(
            root,
            number,
            task_id,
            protocol,
            summary,
            validation,
            assessment,
            run,
            sources,
        )
    else:
        evidence = failed_task(
            root,
            number,
            task_id,
            summary,
            validation,
            assessment,
            run,
            sources,
            errata_applied,
        )
    semantic_scope = None
    amendment_path = run / "assessment-amendment.json"
    if number == 14:
        add_source(sources, root, task_id, amendment_path)
        amendment = load_json(amendment_path)
        require(
            amendment["task_id"] == summary["task_id"]
            and amendment["attempt"] == RUN_ID
            and amendment["confirmed_facts"]["fixture_behavior_match"] is True
            and amendment["confirmed_facts"]["full_semantic_equivalence"] is None,
            f"{task_id}: semantic amendment conflict",
        )
        verify_reference(root, amendment["original_assessment"])
        for reference in amendment["patch_evidence"]:
            verify_reference(root, reference)
            add_source(sources, root, task_id, root / reference["path"])
        semantic_scope = {
            "fixture_behavior_match": True,
            "full_semantic_equivalence": None,
            "amendment_path": relative(amendment_path, root),
        }
        errata_applied.append(
            {
                "task_id": task_id,
                "type": "assessment_amendment",
                "path": relative(amendment_path, root),
                "sha256": sha256(amendment_path),
            }
        )
    return {
        "task_id": task_id,
        "task_slug": summary["task_id"],
        "result_path": relative(result, root),
        "attempt": RUN_ID,
        "session_id": summary["session_id"],
        "evaluation_role": EVALUATION_ROLE,
        "metric_eligible": False,
        "performance_conclusion": None,
        "candidate_applied": False,
        "single_run_no_rerun": True,
        "assessment_path": relative(assessment_path, root),
        "erratum_path": evidence.get("erratum_path"),
        "amendment_path": relative(amendment_path, root) if amendment_path.exists() else None,
        "semantic_scope": semantic_scope,
    } | evidence


def ff009_limit(root: Path, sources: dict[str, set[str]]) -> dict[str, Any]:
    task_id = "FF-009"
    task_path = root / "benchmarks" / "tasks" / "ff-009-session-lifecycle" / "task.json"
    patch_path = task_path.with_name("gold.patch")
    test_path = root / "tests" / "fastfix" / "test_ff009_fixture.py"
    for path in (task_path, patch_path, test_path):
        add_source(sources, root, task_id, path)
    task = load_json(task_path)
    patch = patch_path.read_bytes()
    test = test_path.read_text(encoding="utf-8")
    require(task["expected_buggy_result"] == {"passed": 3, "failed": 1}, "FF-009: metadata Buggy count changed")
    require(task["expected_fixed_result"] == {"passed": 4, "failed": 0}, "FF-009: metadata Gold count changed")
    require(not patch.endswith(b"\n") and b"@@ -18,6 +18,8 @@" in patch, "FF-009: legacy Gold patch limitation changed")
    require('"1 failed, 4 passed"' in test and '"5 passed"' in test, "FF-009: frozen actual counts missing")
    require("before.replace(removed, added)" in test, "FF-009: semantic Gold construction missing")
    return {
        "id": "ff009_evidence_quality",
        "task_id": task_id,
        "canonical_gold_patch_directly_applicable": False,
        "gold_validation_method": "temporary semantic state constructed from exact frozen removed and added lines",
        "metadata_counts": {
            "buggy": task["expected_buggy_result"],
            "gold": task["expected_fixed_result"],
        },
        "frozen_execution_counts": {
            "buggy": {"passed": 4, "failed": 1},
            "gold": {"passed": 5, "failed": 0},
        },
        "canonical_files_rewritten": False,
        "candidate_validation_impact": "none",
    }


def source_commit(root: Path, paths: list[str]) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    commit = result.stdout.strip()
    require(len(commit) == 40, "Unable to identify frozen source commit")
    return commit


def build_assessment(root: Path = ROOT, generated_from_commit: str | None = None) -> dict[str, Any]:
    sources: dict[str, set[str]] = {}
    errata_applied: list[dict[str, Any]] = []
    task_results = [task_result(root, number, sources, errata_applied) for number in TASK_NUMBERS]
    ff009_evidence_limit = ff009_limit(root, sources)
    validated_ids = [task["task_id"] for task in task_results if task["validated_candidate"]]
    primary = Fraction(len(validated_ids), len(task_results))
    provider_confounded = [task for task in task_results if task["classification"] == "provider_confounded_incomplete"]
    require(
        len(provider_confounded) == 1 and provider_confounded[0]["task_id"] == "FF-007",
        "Provider sensitivity set changed",
    )
    sensitivity_denominator = len(task_results) - len(provider_confounded)
    sensitivity = Fraction(len(validated_ids), sensitivity_denominator)
    source_manifest = [
        {
            "path": path,
            "sha256": sha256(root / path),
            "task_ids": sorted(task_ids),
        }
        for path, task_ids in sorted(sources.items())
    ]
    for task in task_results:
        task["source_evidence"] = [
            {"path": entry["path"], "sha256": entry["sha256"]}
            for entry in source_manifest
            if task["task_id"] in entry["task_ids"]
        ]
    commit = generated_from_commit or source_commit(root, [entry["path"] for entry in source_manifest])
    return {
        "schema_version": "1.0",
        "assessment_kind": "development_frozen_aggregate",
        "formal_benchmark": False,
        "metric_eligible": False,
        "evaluation_role": EVALUATION_ROLE,
        "performance_conclusion": None,
        "task_universe": {
            "included_task_ids": [f"FF-{number:03d}" for number in TASK_NUMBERS],
            "task_count": len(task_results),
            "task_type": "manually constructed FastAPI synthetic defects",
            "run_policy": "one development-stage unseen run per task with no rerun after failure",
            "excluded_tasks": [
                {
                    "task_id": "FF-001",
                    "reason": "early multi-stage development and regression runs used a non-uniform protocol",
                },
                {
                    "task_id": "FF-002",
                    "reason": "pre-designed ablation experiment outside the primary development task set",
                },
            ],
        },
        "primary_metric": {
            "name": PRIMARY_METRIC_NAME,
            "numerator": primary.numerator,
            "denominator": primary.denominator,
            "exact_fraction": f"{primary.numerator}/{primary.denominator}",
            "exact_decimal_notation": exact_decimal_notation(primary),
            "decimal_expansion": decimal_expansion(primary),
            "display_percentage": display_percentage(primary),
            "validated_candidate_task_ids": validated_ids,
        },
        "sensitivity_analysis": {
            "name": "exclude_provider_confounded_ff007",
            "level": "secondary",
            "analysis_type": "sensitivity analysis",
            "view": "post-hoc",
            "primary_result": False,
            "resume_primary_metric_allowed": False,
            "excluded_task_ids": ["FF-007"],
            "numerator": sensitivity.numerator,
            "denominator": sensitivity.denominator,
            "exact_fraction": f"{sensitivity.numerator}/{sensitivity.denominator}",
            "display_percentage": display_percentage(sensitivity),
        },
        "task_results": task_results,
        "errata_applied": sorted(errata_applied, key=lambda item: item["task_id"]),
        "limitations": [
            {
                "id": "development_scope",
                "statement": "These are development-stage, manually constructed FastAPI synthetic defects, not a public benchmark, SWE-bench, or production data.",
            },
            {
                "id": "generalization",
                "statement": "The evidence does not establish general repair ability for arbitrary FastAPI repositories.",
            },
            {
                "id": "fixture_scope",
                "statement": "Passing validation establishes behavior only within each frozen fixture and its targeted, regression, and Ruff checks.",
            },
            {
                "id": "candidate_application",
                "statement": "No Candidate was applied to canonical source; successful Candidates remained pending during run-001 and were rejected afterward.",
            },
            {
                "id": "single_run",
                "statement": "All included tasks are single development runs; failed tasks were not rerun.",
            },
            ff009_evidence_limit,
            {
                "id": "ff014_semantic_boundary",
                "task_id": "FF-014",
                "fixture_behavior_match": True,
                "full_semantic_equivalence": None,
                "statement": "The Candidate also changed async def to def, so FastAPI scheduling and thread-context equivalence is not established.",
            },
        ],
        "prohibited_claims": PROHIBITED_CLAIMS,
        "source_manifest": source_manifest,
        "generated_from_commit": commit,
    }


def render_markdown(assessment: dict[str, Any]) -> str:
    metric = assessment["primary_metric"]
    sensitivity = assessment["sensitivity_analysis"]
    rows = [
        "| 任务 | 分类 | Validated Candidate | 原运行终态 | 后续处置 | targeted | regression | Ruff |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task in assessment["task_results"]:
        rows.append(
            f"| {task['task_id']} | `{task['classification']}` | "
            f"{'是' if task['validated_candidate'] else '否'} | `{task['run_final_state']}` | "
            f"`{task['post_run_disposition']['status']}` | `{task['validation']['targeted']}` | "
            f"`{task['validation']['regression']}` | `{task['validation']['ruff']}` |"
        )
    manifest_rows = [f"- `{entry['path']}` — `{entry['sha256']}`" for entry in assessment["source_manifest"]]
    prohibited = [f"- `{claim}`" for claim in assessment["prohibited_claims"]]
    return "\n".join(
        [
            "# FastFix 冻结开发评测聚合",
            "",
            f"在固定的 FF-003—FF-015 共 {metric['denominator']} 个开发阶段单次未见合成任务中，"
            f"{metric['numerator']} 个产生了通过规定验证的待审批 Candidate："
            f"`{metric['name']}={metric['exact_fraction']}`（{metric['display_percentage']}）。",
            "",
            "## 任务集合与口径",
            "",
            "- 主集合固定为 FF-003—FF-015；FF-001 与 FF-002 不进入主分母。",
            "- `evaluation_role=development_unseen_baseline`，`metric_eligible=false`，`formal_benchmark=false`。",
            "- 任务为人工构建的 FastAPI 合成缺陷；不是公开 Benchmark、SWE-bench 或生产数据。",
            "- 所有任务均为单次开发运行，失败后没有重跑；Candidate 未 Apply 到 canonical source。",
            "",
            "## 主结果",
            "",
            f"- 分子：{metric['numerator']}。",
            f"- 分母：{metric['denominator']}。",
            f"- 精确值：`{metric['exact_fraction']} = {metric['exact_decimal_notation']}`。",
            f"- 展示值：`{metric['display_percentage']}`。",
            f"- Validated Candidate：{', '.join(metric['validated_candidate_task_ids'])}。",
            "",
            "## 每题结果",
            "",
            *rows,
            "",
            "## 未产生有效 Candidate",
            "",
            "- FF-007：`provider_confounded_incomplete`。Provider HTTP 502 重试耗尽；targeted 已通过，"
            "regression 与 Ruff 未完成，没有 approval package。这不构成模型语义修复失败证据。",
            "- FF-008：`agent_closure_failure`。核心 `flush → commit` 修复及三道验证曾通过，"
            "但冗余修改清理时 Patch 失败并 rollback，最终 Diff 为空且未提交 Candidate。"
            "`metrics-erratum.json` 已应用，历史 `patch_failures=0` 不作为真实累计值。",
            "",
            "## FF-009 Reject 与证据限制",
            "",
            "- 原 `run-001` 终态为 `approval_pending`；发布前审计随后执行真实 Reject，"
            "二者通过 `run_final_state` 与 `post_run_disposition` 分开记录。",
            "- canonical Gold patch 的旧 hunk/末尾换行问题使其无法由当前 `git apply` 直接应用；"
            "冻结测试以精确增删行构造临时 Gold 语义状态。",
            "- task metadata 的 Buggy/Gold 计数为 `3/1`、`4/0`；当前冻结执行为 `4/1`、`5/0`。"
            "canonical task 与 Gold patch 均未回写；该限制不改变 Candidate 的三道验证通过事实。",
            "",
            "## FF-014 语义边界",
            "",
            "- 已应用 `assessment-amendment.json`：`fixture_behavior_match=true`，`full_semantic_equivalence=null`。",
            "- Candidate 额外将 handler 从 `async def` 改为 `def`；当前证据不能证明 FastAPI 调度和线程上下文完全等价。",
            "",
            "## 次级敏感性分析",
            "",
            f"排除 Provider-confounded FF-007 后为 `{sensitivity['exact_fraction']}`"
            f"（{sensitivity['display_percentage']}）。这是 secondary、post-hoc sensitivity analysis，"
            "不是预设主结果，不得单独作为简历主指标。",
            "",
            "## 禁止替换为的夸大表述",
            "",
            *prohibited,
            "",
            "## 来源与完整性",
            "",
            "JSON 是唯一权威机器可读来源；本 Markdown 由同一数据模型生成。构建器在必要证据缺失、"
            "身份或标签冲突、hash 不一致时失败，并按稳定顺序生成下列 source manifest：",
            "",
            *manifest_rows,
            "",
            f"Frozen source commit：`{assessment['generated_from_commit']}`。",
            "",
        ]
    )


def write_outputs(
    root: Path = ROOT,
    output_dir: Path | None = None,
    generated_from_commit: str | None = None,
) -> tuple[Path, Path]:
    assessment = build_assessment(root, generated_from_commit)
    destination = output_dir or root / "benchmarks" / "results"
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "aggregate-assessment.json"
    markdown_path = destination / "aggregate-assessment.md"
    json_path.write_text(json.dumps(assessment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(render_markdown(assessment), encoding="utf-8", newline="\n")
    return json_path, markdown_path


app = typer.Typer(add_completion=False)


@app.command()
def main(
    output_dir: Path = typer.Option(
        ROOT / "benchmarks" / "results",
        file_okay=False,
        dir_okay=True,
    ),
) -> None:
    for path in write_outputs(output_dir=output_dir):
        typer.echo(relative(path, ROOT) if path.is_relative_to(ROOT) else path)


if __name__ == "__main__":
    app()

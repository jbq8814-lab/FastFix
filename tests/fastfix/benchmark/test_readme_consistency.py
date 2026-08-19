import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_readme_matches_aggregate_assessment_and_local_links_exist() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assessment = json.loads((ROOT / "benchmarks/results/aggregate-assessment.json").read_text(encoding="utf-8"))

    metric = assessment["primary_metric"]
    assert (
        f"`{metric['name']} = {metric['exact_fraction']} ≈ {metric['display_percentage']}`" in readme
        and assessment["formal_benchmark"] is False
        and assessment["metric_eligible"] is False
    )
    for result in assessment["task_results"]:
        defect = result["task_slug"].split("-", 2)[2]
        changed_files = ", ".join(f"`{path}`" for path in result["changed_files"]) or "—"
        assert (
            f"| {result['task_id']} | {defect} | {'是' if result['validated_candidate'] else '否'} "
            f"| `{result['classification']}` | {changed_files} |"
        ) in readme

    links = [link for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme) if "://" not in link]
    assert links and all((ROOT / link).exists() for link in links)

import hashlib
import json
from pathlib import Path

from fastfix.agents.diagnosis import FastFixDiagnosisAgent
from fastfix.diagnosis.evaluation import evaluate_ff001_diagnosis
from fastfix.diagnosis.models import SubmitDiagnosisArgs
from fastfix.environments.tool_environment import FastFixToolEnvironment
from fastfix.tools.fastapi import build_readonly_registry
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"


def hashes() -> dict[str, str]:
    return {
        path.relative_to(FIXTURE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in FIXTURE.rglob("*")
        if path.is_file()
    }


def test_ff001_structured_read_only_diagnosis() -> None:
    before = hashes()
    diagnosis = {
        "summary": "The route returns an unresolved service result.",
        "root_cause": "The route calls an asynchronous service without resolving its result.",
        "evidence": [
            {"path": "app/main.py", "start_line": 14, "end_line": 16, "reason": "Route call."},
            {"path": "app/service.py", "start_line": 4, "end_line": 6, "reason": "Async service."},
        ],
        "suspected_files": ["app/main.py"],
        "recommended_fix": "Await fetch_user in the route before returning.",
        "confidence": 0.99,
    }
    actions = [
        {"tool": "show_tree", "arguments": {}, "tool_call_id": "1"},
        {"tool": "search_code", "arguments": {"query": "fetch_user"}, "tool_call_id": "2"},
        {"tool": "read_file", "arguments": {"path": "app/main.py"}, "tool_call_id": "3"},
        {"tool": "read_file", "arguments": {"path": "app/service.py"}, "tool_call_id": "4"},
        {"tool": "submit_diagnosis", "arguments": diagnosis, "tool_call_id": "5"},
    ]
    model = DeterministicToolcallModel(
        outputs=[make_toolcall_output(None, [{"id": action["tool_call_id"]}], [action]) for action in actions],
        cost_per_call=0,
    )
    environment = FastFixToolEnvironment(
        registry=build_readonly_registry(FIXTURE),
        workspace=FIXTURE,
    )
    agent = FastFixDiagnosisAgent(
        model,
        environment,
        system_template="Use structured read-only tools.",
        instance_template="{{ task }}",
        cost_limit=0,
    )
    result = agent.run("Diagnose FF-001")
    submitted = SubmitDiagnosisArgs.model_validate(json.loads(result["submission"]))
    assert evaluate_ff001_diagnosis(submitted).diagnosis_correct
    assert hashes() == before
    assert all(call["tool_name"] != "bash" for call in environment.tool_call_history)
    assert environment.serialize()["info"]["fastfix_environment"]["tool_call_count"] == 5

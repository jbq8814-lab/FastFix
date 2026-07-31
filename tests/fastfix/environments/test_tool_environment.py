import json
from pathlib import Path

import pytest

from fastfix.environments.tool_environment import FastFixToolEnvironment
from fastfix.tools.fastapi import build_readonly_registry
from minisweagent.exceptions import Submitted


@pytest.fixture
def environment(tmp_path: Path) -> FastFixToolEnvironment:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    return FastFixToolEnvironment(registry=build_readonly_registry(tmp_path), workspace=tmp_path)


def valid_diagnosis() -> dict:
    return {
        "summary": "The route returns the wrong value.",
        "root_cause": "The route does not handle the service result correctly.",
        "evidence": [{"path": "app/main.py", "start_line": 1, "end_line": 1, "reason": "Route code."}],
        "suspected_files": ["app/main.py"],
        "recommended_fix": "Correct the route.",
        "confidence": 0.8,
    }


def test_repository_tools_execute_as_structured_outputs(environment: FastFixToolEnvironment) -> None:
    tree = environment.execute({"tool": "show_tree", "arguments": {}, "tool_call_id": "1"})
    read = environment.execute({"tool": "read_file", "arguments": {"path": "app/main.py"}, "tool_call_id": "2"})
    assert tree["returncode"] == 0
    assert json.loads(tree["output"])["tool_name"] == "show_tree"
    assert read["returncode"] == 0
    assert "1 | value = 1" in json.loads(read["output"])["output"]


def test_registry_errors_are_normal_tool_results(environment: FastFixToolEnvironment) -> None:
    invalid = environment.execute({"tool": "read_file", "arguments": {}, "tool_call_id": "1"})
    unknown = environment.execute({"tool": "unknown", "arguments": {}, "tool_call_id": "2"})
    assert invalid["returncode"] == 1
    assert invalid["extra"]["error_code"] == "invalid_arguments"
    assert unknown["returncode"] == 1
    assert unknown["extra"]["error_code"] == "unknown_tool"


def test_invalid_submit_does_not_end_agent(environment: FastFixToolEnvironment) -> None:
    result = environment.execute({"tool": "submit_diagnosis", "arguments": {}, "tool_call_id": "1"})
    assert result["returncode"] == 1
    assert result["extra"]["error_code"] == "invalid_arguments"


def test_valid_submit_raises_structured_submitted(environment: FastFixToolEnvironment) -> None:
    with pytest.raises(Submitted) as submitted:
        environment.execute({"tool": "submit_diagnosis", "arguments": valid_diagnosis(), "tool_call_id": "1"})
    message = submitted.value.messages[0]
    assert message["extra"]["exit_status"] == "Submitted"
    assert message["extra"]["diagnosis"] == valid_diagnosis()
    assert json.loads(message["extra"]["submission"]) == valid_diagnosis()


def test_history_and_serialization_are_safe(environment: FastFixToolEnvironment, tmp_path: Path) -> None:
    environment.execute({"tool": "show_tree", "arguments": {}, "tool_call_id": "1"})
    environment.execute({"tool": "missing", "arguments": {}, "tool_call_id": "2"})
    serialized = environment.serialize()
    info = serialized["info"]["fastfix_environment"]
    assert info == {
        "tool_call_count": 2,
        "tool_names": ["show_tree", "missing"],
        "tool_error_count": 1,
    }
    assert [call["sequence"] for call in environment.tool_call_history] == [1, 2]
    assert str(tmp_path) not in json.dumps(serialized)
    assert environment.get_template_vars() == {
        "workspace": ".",
        "tool_names": [
            "show_tree",
            "read_file",
            "search_code",
            "inspect_fastapi_routes",
            "submit_diagnosis",
        ],
    }


def test_route_analysis_errors_are_safe_observations(
    environment: FastFixToolEnvironment,
    tmp_path: Path,
) -> None:
    (tmp_path / "app" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    parsed = environment.execute(action := {"tool": "inspect_fastapi_routes", "arguments": {}, "tool_call_id": "1"})
    assert parsed["returncode"] == 0
    assert json.loads(parsed["output"])["metadata"]["parse_error_count"] == 1
    action["arguments"] = {"path": ".env"}
    denied = environment.execute(action)
    assert denied["returncode"] == 1
    assert denied["extra"]["error_code"] == "sensitive_path"

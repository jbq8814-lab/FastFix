import json
from pathlib import Path

import pytest

from fastfix.agents.diagnosis import FastFixDiagnosisAgent
from fastfix.environments.tool_environment import FastFixToolEnvironment
from fastfix.tools.fastapi import build_readonly_registry
from fastfix.tools.models import ToolResult
from fastfix.tools.registry import ToolRegistry, ToolSpec
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output


def action(tool: str, arguments: dict, call_id: str) -> dict:
    return {"tool": tool, "arguments": arguments, "tool_call_id": call_id}


def diagnosis_arguments() -> dict:
    return {
        "summary": "Incorrect route behavior.",
        "root_cause": "The route mishandles the service result.",
        "evidence": [
            {"path": "app/main.py", "start_line": 1, "end_line": 2, "reason": "Route."},
            {"path": "app/service.py", "start_line": 1, "end_line": 2, "reason": "Service."},
        ],
        "suspected_files": ["app/main.py"],
        "recommended_fix": "Use the service result correctly.",
        "confidence": 0.9,
    }


def build_outputs() -> list[dict]:
    actions = [
        action("show_tree", {}, "1"),
        action("search_code", {"query": "fetch_user"}, "2"),
        action("read_file", {"path": "app/main.py"}, "3"),
        action("submit_diagnosis", diagnosis_arguments(), "4"),
    ]
    return [make_toolcall_output(None, [{"id": item["tool_call_id"]}], [item]) for item in actions]


def build_agent(workspace: Path, *, outputs: list[dict] | None = None, **config) -> FastFixDiagnosisAgent:
    model = DeterministicToolcallModel(outputs=outputs or build_outputs(), cost_per_call=0)
    environment = FastFixToolEnvironment(
        registry=build_readonly_registry(workspace),
        workspace=workspace,
    )
    return FastFixDiagnosisAgent(
        model,
        environment,
        system_template="Read only.",
        instance_template="{{ task }}",
        cost_limit=0,
        **config,
    )


def test_agent_reuses_upstream_loop_and_submits(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("fetch_user()\n", encoding="utf-8")
    agent = build_agent(tmp_path)
    result = agent.run("Diagnose")
    trajectory = agent.serialize()
    assert result["exit_status"] == "Submitted"
    assert json.loads(result["submission"])["suspected_files"] == ["app/main.py"]
    assert trajectory["info"]["fastfix"] == {
        "mode": "read_only_diagnosis",
        "write_tools_enabled": False,
    }
    assert trajectory["info"]["fastfix_environment"]["tool_call_count"] == 4
    assert any(message.get("role") == "assistant" for message in trajectory["messages"])
    assert any(message.get("role") == "tool" for message in trajectory["messages"])
    assert "bash" not in agent.env.tool_names


def test_agent_rejects_write_tools(tmp_path: Path) -> None:
    class EmptyArgs(ToolResult):
        pass

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "bash",
            "Forbidden.",
            EmptyArgs,
            lambda arguments: ToolResult(tool_name="bash", ok=True),
        )
    )
    environment = FastFixToolEnvironment(registry=registry, workspace=tmp_path)
    with pytest.raises(ValueError, match="bash"):
        FastFixDiagnosisAgent(
            DeterministicToolcallModel(outputs=[]),
            environment,
            system_template="system",
            instance_template="instance",
        )


def test_step_limit_is_enforced_by_upstream_agent(tmp_path: Path) -> None:
    output = make_toolcall_output(None, [{"id": "1"}], [action("show_tree", {}, "1")])
    agent = build_agent(tmp_path, outputs=[output], step_limit=1)
    assert agent.run("Diagnose")["exit_status"] == "LimitsExceeded"


def test_agent_receives_route_analysis_and_can_submit(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    route = tmp_path / "app" / "main.py"
    route.write_text(
        'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/items/{item_id}")\ndef item(item_id: int): pass\n',
        encoding="utf-8",
    )
    broken = tmp_path / "app" / "broken.py"
    broken.write_text("def broken(:\n", encoding="utf-8")
    before = route.read_bytes()
    broken_before = broken.read_bytes()
    scripted = [
        action("inspect_fastapi_routes", {}, "1"),
        action("read_file", {"path": "app/main.py"}, "2"),
        action("submit_diagnosis", diagnosis_arguments(), "3"),
    ]
    agent = build_agent(
        tmp_path,
        outputs=[make_toolcall_output(None, [{"id": item["tool_call_id"]}], [item]) for item in scripted],
    )
    assert agent.run("Diagnose route")["exit_status"] == "Submitted"
    assert route.read_bytes() == before
    assert broken.read_bytes() == broken_before
    assert [call["tool_name"] for call in agent.env.tool_call_history] == [
        "inspect_fastapi_routes",
        "read_file",
        "submit_diagnosis",
    ]
    tool_messages = [message for message in agent.serialize()["messages"] if message.get("role") == "tool"]
    assert '"route_count":1' in tool_messages[0]["content"]
    assert '"parse_error_count":1' in tool_messages[0]["content"]
    assert any(message.get("role") == "assistant" for message in agent.serialize()["messages"][2:])

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fastfix.diagnosis.models import get_submit_diagnosis_tool
from fastfix.models.tool_call import FastFixLitellmModel, parse_fastfix_tool_calls
from fastfix.tools.repository import build_repository_registry
from minisweagent.exceptions import FormatError

FORMAT_ERROR_TEMPLATE = "{{ error }}"


def tool_call(name: str, arguments: str, call_id: str = "call_123"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def tool_schemas(tmp_path) -> list[dict[str, object]]:
    return [
        *build_repository_registry(tmp_path).get_openai_tools(),
        get_submit_diagnosis_tool(),
    ]


def build_model(tmp_path) -> FastFixLitellmModel:
    schemas = tool_schemas(tmp_path)
    return FastFixLitellmModel(
        model_name="openai/test-model",
        tool_schemas=schemas,
        allowed_tool_names={schema["function"]["name"] for schema in schemas},
    )


def test_query_uses_only_fastfix_tools(tmp_path) -> None:
    model = build_model(tmp_path)
    response = object()
    with patch("fastfix.models.tool_call.litellm.completion", return_value=response) as completion:
        assert model._query([{"role": "user", "content": "diagnose"}]) is response
    kwargs = completion.call_args.kwargs
    assert kwargs["tools"] == model.tool_schemas
    assert {tool["function"]["name"] for tool in kwargs["tools"]} == {
        "show_tree",
        "read_file",
        "search_code",
        "submit_diagnosis",
    }
    assert "bash" not in {tool["function"]["name"] for tool in kwargs["tools"]}


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [tool_call("read_file", "{}"), tool_call("show_tree", "{}")],
        [tool_call("bash", '{"command":"ls"}')],
        [tool_call("read_file", "{")],
        [tool_call("read_file", "[]")],
    ],
)
def test_invalid_tool_calls_raise_format_error(calls) -> None:
    with pytest.raises(FormatError):
        parse_fastfix_tool_calls(
            calls,
            allowed_tool_names={"read_file", "show_tree", "submit_diagnosis"},
            format_error_template=FORMAT_ERROR_TEMPLATE,
        )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("read_file", '{"path":"app/main.py"}'),
        (
            "submit_diagnosis",
            '{"summary":"s","root_cause":"r","evidence":[],"suspected_files":[],"recommended_fix":"f","confidence":0.5}',
        ),
    ],
)
def test_valid_tool_call_is_parsed(name: str, arguments: str) -> None:
    assert parse_fastfix_tool_calls(
        [tool_call(name, arguments)],
        allowed_tool_names={"read_file", "submit_diagnosis"},
        format_error_template=FORMAT_ERROR_TEMPLATE,
    ) == [{"tool": name, "arguments": __import__("json").loads(arguments), "tool_call_id": "call_123"}]


def test_parse_actions_passes_finish_reason(tmp_path) -> None:
    model = build_model(tmp_path)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(tool_calls=[tool_call("read_file", '{"path":"app/main.py"}')]),
                finish_reason="tool_calls",
            )
        ]
    )
    assert model._parse_actions(response)[0]["tool_call_id"] == "call_123"


def test_model_configuration_is_defensive_and_serializes_names_only(tmp_path) -> None:
    schemas = tool_schemas(tmp_path)
    names = {schema["function"]["name"] for schema in schemas}
    model = FastFixLitellmModel(
        model_name="openai/test",
        tool_schemas=schemas,
        allowed_tool_names=names,
        model_kwargs={"api_key": "secret", "temperature": 0},
    )
    schemas[0]["function"]["name"] = "changed"
    serialized = model.serialize()
    assert "changed" not in serialized["info"]["fastfix_model"]["tool_names"]
    assert serialized["info"]["fastfix_model"]["tool_names"] == sorted(names)
    assert "tool_schemas" not in serialized["info"]["config"]["model"]
    assert serialized["info"]["config"]["model"]["model_kwargs"] == {"temperature": 0}

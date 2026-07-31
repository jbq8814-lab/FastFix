from typing import Any

import pytest
from pydantic import BaseModel

from fastfix.security.paths import PathPolicyError
from fastfix.tools.models import ToolResult
from fastfix.tools.registry import ToolRegistry, ToolSpec


class EchoArgs(BaseModel):
    text: str


def echo(arguments: BaseModel) -> ToolResult:
    args = EchoArgs.model_validate(arguments)
    return ToolResult(tool_name="echo", ok=True, output=args.text)


def build_spec(name: str = "echo") -> ToolSpec:
    return ToolSpec(name=name, description="Echo text.", arguments_model=EchoArgs, handler=echo)


def test_register_and_execute_tool() -> None:
    registry = ToolRegistry()
    registry.register(build_spec())
    assert registry.names() == ("echo",)
    assert registry.execute("echo", {"text": "hello"}) == ToolResult(tool_name="echo", ok=True, output="hello")


@pytest.mark.parametrize("name", ["", "   "])
def test_empty_tool_name_is_rejected(name: str) -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(build_spec(name))


def test_duplicate_tool_name_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(build_spec())
    with pytest.raises(ValueError):
        registry.register(build_spec())


def test_unknown_tool_returns_structured_error() -> None:
    result = ToolRegistry().execute("missing", {})
    assert not result.ok
    assert result.error_code == "unknown_tool"


@pytest.mark.parametrize("arguments", [{}, {"text": 123}, {"text": "ok", "extra": True}])
def test_invalid_arguments_are_normalized(arguments: dict[str, Any]) -> None:
    class StrictEchoArgs(EchoArgs):
        model_config = {"extra": "forbid"}

    registry = ToolRegistry()
    registry.register(ToolSpec("echo", "Echo.", StrictEchoArgs, echo))
    result = registry.execute("echo", arguments)
    assert not result.ok
    assert result.error_code == "invalid_arguments"


def test_path_policy_error_is_preserved() -> None:
    def reject(arguments: BaseModel) -> ToolResult:
        raise PathPolicyError("sensitive_path", "Sensitive path is not allowed: .env")

    registry = ToolRegistry()
    registry.register(ToolSpec("read", "Read.", EchoArgs, reject))
    result = registry.execute("read", {"text": ".env"})
    assert not result.ok
    assert result.error_code == "sensitive_path"
    assert result.output == "Sensitive path is not allowed: .env"


def test_unknown_handler_error_is_normalized() -> None:
    def fail(arguments: BaseModel) -> ToolResult:
        raise RuntimeError("host detail")

    registry = ToolRegistry()
    registry.register(ToolSpec("fail", "Fail.", EchoArgs, fail))
    result = registry.execute("fail", {"text": "value"})
    assert not result.ok
    assert result.error_code == "tool_execution_error"
    assert "host detail" not in result.output


def test_openai_schema_and_registration_order_are_stable() -> None:
    registry = ToolRegistry()
    registry.register(build_spec("first"))
    registry.register(build_spec("second"))
    tools = registry.get_openai_tools()
    assert registry.names() == ("first", "second")
    assert [tool["function"]["name"] for tool in tools] == ["first", "second"]
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["parameters"]["required"] == ["text"]
    assert tools[0]["function"]["parameters"]["properties"]["text"]["type"] == "string"

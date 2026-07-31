from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from fastfix.security.paths import PathPolicyError
from fastfix.tools.models import ToolResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: Callable[[BaseModel], ToolResult]


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name.strip():
            raise ValueError("Tool name must not be empty")
        if spec.name in self._specs:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        spec = self._specs.get(tool_name)
        if spec is None:
            return ToolResult(tool_name=tool_name, ok=False, error_code="unknown_tool", output="Unknown tool.")
        try:
            validated = spec.arguments_model.model_validate(arguments)
            return spec.handler(validated)
        except ValidationError:
            return ToolResult(
                tool_name=tool_name,
                ok=False,
                error_code="invalid_arguments",
                output="Invalid tool arguments.",
            )
        except PathPolicyError as error:
            return ToolResult(tool_name=tool_name, ok=False, error_code=error.code, output=str(error))
        except Exception:
            return ToolResult(
                tool_name=tool_name,
                ok=False,
                error_code="tool_execution_error",
                output="Tool execution failed.",
            )

    def get_openai_tools(self) -> list[dict[str, Any]]:
        tools = []
        for spec in self._specs.values():
            parameters = spec.arguments_model.model_json_schema()
            parameters.pop("title", None)
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": parameters,
                    },
                }
            )
        return tools

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

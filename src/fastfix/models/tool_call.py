import copy
import json

import litellm
from jinja2 import StrictUndefined, Template

from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.utils.serialize import recursive_merge


def _raise_format_error(
    error: str,
    *,
    format_error_template: str,
    has_tool_calls: bool,
    template_kwargs: dict[str, object],
) -> None:
    raise FormatError(
        {
            "role": "user",
            "content": Template(format_error_template, undefined=StrictUndefined).render(
                error=error,
                actions=[],
                has_tool_calls=has_tool_calls,
                **template_kwargs,
            ),
            "extra": {"interrupt_type": "FormatError"},
        }
    )


def parse_fastfix_tool_calls(
    tool_calls: list[object],
    *,
    allowed_tool_names: set[str],
    format_error_template: str,
    template_kwargs: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    template_kwargs = template_kwargs or {}
    if not tool_calls:
        _raise_format_error(
            "No tool call found. Every response must call exactly one FastFix tool.",
            format_error_template=format_error_template,
            has_tool_calls=False,
            template_kwargs=template_kwargs,
        )
    if len(tool_calls) != 1:
        _raise_format_error(
            "Multiple tool calls are not allowed. Call exactly one FastFix tool.",
            format_error_template=format_error_template,
            has_tool_calls=True,
            template_kwargs=template_kwargs,
        )

    tool_call = tool_calls[0]
    try:
        name = tool_call.function.name
        raw_arguments = tool_call.function.arguments
        tool_call_id = tool_call.id
    except AttributeError:
        _raise_format_error(
            "Malformed tool call.",
            format_error_template=format_error_template,
            has_tool_calls=True,
            template_kwargs=template_kwargs,
        )
    if name not in allowed_tool_names:
        _raise_format_error(
            f"Unknown tool: {name}",
            format_error_template=format_error_template,
            has_tool_calls=True,
            template_kwargs=template_kwargs,
        )
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError):
        _raise_format_error(
            "Tool arguments are not valid JSON.",
            format_error_template=format_error_template,
            has_tool_calls=True,
            template_kwargs=template_kwargs,
        )
    if not isinstance(arguments, dict):
        _raise_format_error(
            "Tool arguments must be a JSON object.",
            format_error_template=format_error_template,
            has_tool_calls=True,
            template_kwargs=template_kwargs,
        )
    return [{"tool": name, "arguments": arguments, "tool_call_id": tool_call_id}]


class FastFixLitellmModel(LitellmModel):
    def __init__(
        self,
        *,
        tool_schemas: list[dict[str, object]],
        allowed_tool_names: set[str],
        **kwargs,
    ):
        if not tool_schemas:
            raise ValueError("tool_schemas must not be empty")
        schema_names = [schema.get("function", {}).get("name") for schema in tool_schemas]
        if not all(isinstance(name, str) and name for name in schema_names):
            raise ValueError("Every tool schema must have a function name")
        if len(schema_names) != len(set(schema_names)):
            raise ValueError("Tool schema names must be unique")
        if set(schema_names) != allowed_tool_names:
            raise ValueError("Tool schema names must match allowed_tool_names")
        self.tool_schemas = copy.deepcopy(tool_schemas)
        self.allowed_tool_names = set(allowed_tool_names)
        super().__init__(**kwargs)

    def _query(self, messages: list[dict[str, str]], **kwargs):
        try:
            return litellm.completion(
                model=self.config.model_name,
                messages=messages,
                tools=self.tool_schemas,
                **(self.config.model_kwargs | kwargs),
            )
        except litellm.exceptions.AuthenticationError as error:
            error.message += " You can permanently set your API key with `mini-extra config set KEY VALUE`."
            raise

    def _parse_actions(self, response) -> list[dict]:
        return parse_fastfix_tool_calls(
            response.choices[0].message.tool_calls or [],
            allowed_tool_names=self.allowed_tool_names,
            format_error_template=self.config.format_error_template,
            template_kwargs={"finish_reason": response.choices[0].finish_reason},
        )

    def serialize(self) -> dict:
        serialized = recursive_merge(
            super().serialize(),
            {"info": {"fastfix_model": {"tool_names": sorted(self.allowed_tool_names)}}},
        )
        model_kwargs = serialized["info"]["config"]["model"].get("model_kwargs", {})
        serialized["info"]["config"]["model"]["model_kwargs"] = {
            key: value
            for key, value in model_kwargs.items()
            if key.casefold() not in {"api_key", "authorization", "authorization_header"}
        }
        return serialized

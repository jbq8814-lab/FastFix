import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fastfix.diagnosis.models import SubmitDiagnosisArgs
from fastfix.tools.models import ToolResult
from fastfix.tools.registry import ToolRegistry
from minisweagent.exceptions import Submitted


class FastFixToolEnvironment:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        workspace: Path,
    ):
        self.registry = registry
        self.workspace = workspace
        self.tool_call_history: list[dict[str, object]] = []

    @property
    def tool_names(self) -> tuple[str, ...]:
        return (*self.registry.names(), "submit_diagnosis")

    def _record(self, result: ToolResult) -> None:
        self.tool_call_history.append(
            {
                "sequence": len(self.tool_call_history) + 1,
                "tool_name": result.tool_name,
                "ok": result.ok,
                "error_code": result.error_code,
                "metadata": result.metadata,
            }
        )

    @staticmethod
    def _output(result: ToolResult) -> dict[str, Any]:
        return {
            "output": result.model_dump_json(),
            "returncode": 0 if result.ok else 1,
            "exception_info": "",
            "extra": {
                "tool_name": result.tool_name,
                "tool_ok": result.ok,
                "error_code": result.error_code,
                "metadata": result.metadata,
            },
        }

    def execute(self, action: dict) -> dict[str, Any]:
        tool_name = action.get("tool", "")
        arguments = action.get("arguments", {})
        if tool_name != "submit_diagnosis":
            result = self.registry.execute(tool_name, arguments)
            self._record(result)
            return self._output(result)

        try:
            diagnosis = SubmitDiagnosisArgs.model_validate(arguments)
        except ValidationError:
            result = ToolResult(
                tool_name=tool_name,
                ok=False,
                error_code="invalid_arguments",
                output="Invalid diagnosis arguments.",
            )
            self._record(result)
            return self._output(result)

        result = ToolResult(tool_name=tool_name, ok=True, output="Diagnosis submitted.")
        self._record(result)
        diagnosis_json = json.dumps(diagnosis.model_dump(mode="json"), ensure_ascii=False)
        raise Submitted(
            {
                "role": "exit",
                "content": diagnosis_json,
                "extra": {
                    "exit_status": "Submitted",
                    "submission": diagnosis_json,
                    "diagnosis": diagnosis.model_dump(mode="json"),
                },
            }
        )

    def get_template_vars(self, **kwargs) -> dict[str, object]:
        return {"workspace": ".", "tool_names": list(self.tool_names)}

    def serialize(self) -> dict[str, object]:
        return {
            "info": {
                "fastfix_environment": {
                    "tool_call_count": len(self.tool_call_history),
                    "tool_names": [call["tool_name"] for call in self.tool_call_history],
                    "tool_error_count": sum(not call["ok"] for call in self.tool_call_history),
                }
            }
        }

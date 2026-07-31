from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    tool_name: str
    ok: bool
    output: str = ""
    error_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

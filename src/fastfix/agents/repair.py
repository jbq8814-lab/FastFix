import copy
import json
import time

from pydantic import Field

from fastfix.environments.repair_environment import FastFixRepairEnvironment
from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.exceptions import LimitsExceeded, TimeExceeded
from minisweagent.utils.serialize import recursive_merge

REQUIRED_TOOL_NAMES = {
    "apply_patch",
    "replace_text",
    "run_pytest",
    "run_ruff",
    "show_git_diff",
    "submit_repair",
    "reopen_repair",
}
FORBIDDEN_TOOL_NAMES = {"bash", "run_command", "edit_file", "python", "shell"}


class FastFixRepairAgentConfig(AgentConfig):
    context_recent_rounds: int = Field(default=4, ge=1, le=10)
    context_max_chars: int = Field(default=60_000, ge=10_000, le=500_000)
    context_compact_threshold: int = Field(default=2_000, ge=200, le=50_000)


class FastFixRepairAgent(DefaultAgent):
    def __init__(self, model, env: FastFixRepairEnvironment, **kwargs):
        tools = set(env.tool_names)
        missing = REQUIRED_TOOL_NAMES - tools
        forbidden = FORBIDDEN_TOOL_NAMES & tools
        if missing:
            raise ValueError(f"Required repair tools are missing: {sorted(missing)}")
        if forbidden:
            raise ValueError(f"Shell or unrestricted tools are not allowed: {sorted(forbidden)}")
        self.context_projection_calls: list[dict[str, int | float | bool]] = []
        super().__init__(model, env, config_class=FastFixRepairAgentConfig, **kwargs)

    @staticmethod
    def _context_chars(messages: list[dict]) -> int:
        return len(
            json.dumps(
                [{key: value for key, value in message.items() if key != "extra"} for message in messages],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _tool_records(messages: list[dict]) -> list[dict[str, object]]:
        records = []
        for index, message in enumerate(messages):
            actions = message.get("extra", {}).get("actions", [])
            if message.get("role") != "assistant" or len(actions) != 1 or index + 1 >= len(messages):
                continue
            result = messages[index + 1]
            if result.get("role") != "tool":
                continue
            records.append(
                {
                    "sequence": len(records) + 1,
                    "assistant_index": index,
                    "result_index": index + 1,
                    "action": actions[0],
                    "result": result,
                }
            )
        return records

    @staticmethod
    def _marker(record: dict[str, object], current_revision: int, current_validation_epoch: int) -> str:
        action = record["action"]
        result = record["result"]
        tool = action.get("tool", "")
        sequence = record["sequence"]
        original_chars = len(str(result.get("content", "")))
        if tool == "read_file":
            return (
                "[FastFix context projection: omitted old read_file output; "
                f"path={action.get('arguments', {}).get('path', '')}; sequence={sequence}; "
                f"original_chars={original_chars}]"
            )
        revision = result.get("extra", {}).get("repair_revision")
        validation_epoch = result.get("extra", {}).get("validation_epoch")
        if tool in {"run_pytest", "run_ruff"} and (
            revision != current_revision or validation_epoch != current_validation_epoch
        ):
            return (
                "[FastFix context projection: stale/omitted validation output; "
                f"tool={tool}; revision={revision}; current_revision={current_revision}; "
                f"validation_epoch={validation_epoch}; current_validation_epoch={current_validation_epoch}; "
                f"sequence={sequence}; original_chars={original_chars}]"
            )
        return (
            "[FastFix context projection: omitted old tool output; "
            f"tool={tool}; sequence={sequence}; original_chars={original_chars}]"
        )

    @classmethod
    def _compact_result(
        cls,
        record: dict[str, object],
        current_revision: int,
        current_validation_epoch: int,
    ) -> None:
        result = record["result"]
        marker = cls._marker(record, current_revision, current_validation_epoch)
        result["content"] = marker
        extra = result.get("extra", {})
        extra["raw_output"] = marker
        extra.pop("state_card", None)
        extra["context_projection"] = "compacted"

    def _required_record_indices(self, records: list[dict[str, object]]) -> set[int]:
        required = {record["result_index"] for record in records[-self.config.context_recent_rounds :]}
        if records:
            required.add(records[-1]["result_index"])
        current_revision = self.env.repair_state.revision
        latest_reads: dict[str, int] = {}
        latest_validation: dict[str, int] = {}
        latest_diff: int | None = None
        latest_failure: int | None = None
        for record in records:
            action = record["action"]
            result = record["result"]
            tool = action.get("tool")
            result_index = record["result_index"]
            if tool == "read_file" and result.get("extra", {}).get("tool_ok"):
                latest_reads[action.get("arguments", {}).get("path", "")] = result_index
            if tool == "show_git_diff" and result.get("extra", {}).get("tool_ok"):
                latest_diff = result_index
            if result.get("extra", {}).get("tool_ok") is False:
                latest_failure = result_index
            if (
                tool in {"run_pytest", "run_ruff"}
                and result.get("extra", {}).get("tool_ok")
                and result.get("extra", {}).get("repair_revision") == current_revision
                and result.get("extra", {}).get("validation_epoch") == self.env.repair_state.validation_epoch
            ):
                scope = action.get("arguments", {}).get("scope", tool)
                latest_validation[str(scope)] = result_index
        required.update(latest_reads.values())
        required.update(latest_validation.values())
        if latest_diff is not None:
            required.add(latest_diff)
        if latest_failure is not None:
            required.add(latest_failure)
        return required

    @staticmethod
    def _validate_tool_protocol(messages: list[dict]) -> None:
        pending: list[str] = []
        seen: set[str] = set()
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                if pending:
                    raise ValueError("Projected messages contain missing tool results.")
                tool_calls = message.get("tool_calls") or []
                if not isinstance(tool_calls, list) or any(not isinstance(call, dict) for call in tool_calls):
                    raise ValueError("Projected messages contain malformed tool calls.")
                identifiers = [call.get("id") for call in tool_calls]
                if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
                    raise ValueError("Projected messages contain an invalid tool call ID.")
                if len(identifiers) != len(set(identifiers)) or any(identifier in seen for identifier in identifiers):
                    raise ValueError("Projected messages contain duplicate tool call IDs.")
                pending = identifiers
                seen.update(identifiers)
            elif role == "tool":
                if not pending or message.get("tool_call_id") != pending[0]:
                    raise ValueError("Projected messages contain an orphaned or out-of-order tool result.")
                pending.pop(0)
            elif pending:
                raise ValueError("Projected messages interrupt a tool call/result sequence.")
        if pending:
            raise ValueError("Projected messages contain missing tool results.")

    def project_messages(self) -> tuple[list[dict], dict[str, int | float | bool]]:
        projected = copy.deepcopy(self.messages)
        records = self._tool_records(projected)
        required = self._required_record_indices(records)
        compacted = 0
        current_revision = self.env.repair_state.revision
        current_validation_epoch = self.env.repair_state.validation_epoch
        for record in records:
            result_index = record["result_index"]
            action = record["action"]
            result = record["result"]
            stale_validation = action.get("tool") in {"run_pytest", "run_ruff"} and (
                result.get("extra", {}).get("repair_revision") != current_revision
                or result.get("extra", {}).get("validation_epoch") != current_validation_epoch
            )
            if stale_validation or (
                result_index not in required
                and len(str(result.get("content", ""))) > self.config.context_compact_threshold
            ):
                self._compact_result(record, current_revision, current_validation_epoch)
                compacted += 1
        for record in records:
            if self._context_chars(projected) <= self.config.context_max_chars:
                break
            result_index = record["result_index"]
            if (
                result_index not in required
                and record["result"].get("extra", {}).get("context_projection") != "compacted"
            ):
                self._compact_result(record, current_revision, current_validation_epoch)
                compacted += 1
        raw_chars = self._context_chars(self.messages)
        projected_chars = self._context_chars(projected)
        omitted_chars = max(raw_chars - projected_chars, 0)
        visible = [{key: value for key, value in message.items() if key != "extra"} for message in projected]
        return (
            visible,
            {
                "model_call": self.n_calls,
                "raw_chars": raw_chars,
                "projected_chars": projected_chars,
                "omitted_chars": omitted_chars,
                "compacted_message_count": compacted,
                "reduction_ratio": round(omitted_chars / raw_chars, 6) if raw_chars else 0.0,
                "projection_limit": self.config.context_max_chars,
                "exceeded_projection_limit": projected_chars > self.config.context_max_chars,
            },
        )

    def query(self) -> dict:
        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "LimitsExceeded",
                    "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                }
            )
        if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time):
            raise TimeExceeded(
                {
                    "role": "exit",
                    "content": "TimeExceeded",
                    "extra": {"exit_status": "TimeExceeded", "submission": ""},
                }
            )
        self.n_calls += 1
        projected, statistics = self.project_messages()
        self._validate_tool_protocol(projected)
        self.context_projection_calls.append(statistics)
        message = self.model.query(projected)
        self.cost += message.get("extra", {}).get("cost", 0.0)
        self.add_messages(message)
        return message

    def _projection_summary(self) -> dict[str, object]:
        model_call_count = len(self.context_projection_calls)
        raw_chars = sum(int(call["raw_chars"]) for call in self.context_projection_calls)
        projected_chars = sum(int(call["projected_chars"]) for call in self.context_projection_calls)
        omitted_chars = max(raw_chars - projected_chars, 0)
        return {
            "metric": "json_serialized_characters",
            "scope": "cumulative_model_visible_characters_across_calls",
            "model_calls": model_call_count,
            "model_call_count": model_call_count,
            "raw_chars": raw_chars,
            "projected_chars": projected_chars,
            "omitted_chars": omitted_chars,
            "max_raw_chars_per_call": max(
                (int(call["raw_chars"]) for call in self.context_projection_calls), default=0
            ),
            "max_projected_chars_per_call": max(
                (int(call["projected_chars"]) for call in self.context_projection_calls), default=0
            ),
            "average_raw_chars_per_call": round(raw_chars / model_call_count, 6) if model_call_count else 0.0,
            "average_projected_chars_per_call": (
                round(projected_chars / model_call_count, 6) if model_call_count else 0.0
            ),
            "configured_projection_limit": self.config.context_max_chars,
            "calls_exceeding_projection_limit": sum(
                bool(call["exceeded_projection_limit"]) for call in self.context_projection_calls
            ),
            "compacted_message_count": sum(
                int(call["compacted_message_count"]) for call in self.context_projection_calls
            ),
            "reduction_ratio": round(omitted_chars / raw_chars, 6) if raw_chars else 0.0,
            "recent_rounds": self.config.context_recent_rounds,
            "max_chars": self.config.context_max_chars,
            "calls": self.context_projection_calls,
        }

    def serialize(self, *extra_dicts) -> dict:
        return recursive_merge(
            super().serialize(*extra_dicts),
            {
                "info": {
                    "fastfix": {
                        "mode": "structured_repair",
                        "shell_enabled": False,
                        "validation_gate_enabled": True,
                        "context_projection_enabled": True,
                    },
                    "context_projection": self._projection_summary(),
                }
            },
        )

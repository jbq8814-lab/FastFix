from fastfix.environments.tool_environment import FastFixToolEnvironment
from minisweagent.agents.default import DefaultAgent
from minisweagent.utils.serialize import recursive_merge

WRITE_TOOL_NAMES = {"bash", "run_command", "edit_file", "apply_patch"}


class FastFixDiagnosisAgent(DefaultAgent):
    def __init__(self, model, env: FastFixToolEnvironment, **kwargs):
        forbidden = WRITE_TOOL_NAMES.intersection(env.registry.names())
        if forbidden:
            raise ValueError(f"Write or shell tools are not allowed: {sorted(forbidden)}")
        super().__init__(model, env, **kwargs)

    def serialize(self, *extra_dicts) -> dict:
        return recursive_merge(
            super().serialize(*extra_dicts),
            {"info": {"fastfix": {"mode": "read_only_diagnosis", "write_tools_enabled": False}}},
        )

from pathlib import Path
from typing import Any

from fastfix.sandbox import ValidationBackend

if __package__:
    from . import run_task_baseline as generic
else:
    import run_task_baseline as generic

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = ROOT / "benchmarks" / "tasks" / "ff-003-response-model-field-mismatch"
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-003-response-model-field-mismatch"
PROTOCOL_PATH = ROOT / "benchmarks" / "experiments" / "ff-003-current-baseline" / "protocol.json"
DEFAULT_RUNTIME_ROOT = ROOT / ".fastfix-runtime" / "experiments" / "ff-003-current-baseline"
DEFAULT_RESULTS_ROOT = ROOT / "benchmarks" / "results" / "ff-003-current-baseline"

BaselineDependencies = generic.BaselineDependencies
BaselineSettings = generic.BaselineSettings
SecureRunnerError = generic.SecureRunnerError


def load_protocol() -> dict[str, Any]:
    return generic.load_protocol(PROTOCOL_PATH)


def agent_config() -> dict[str, Any]:
    return generic.agent_config(generic.load_context(PROTOCOL_PATH))


def baseline_registry(workspace: Path, validation_backend: ValidationBackend):
    return generic.baseline_registry(workspace, validation_backend, generic.load_context(PROTOCOL_PATH))


def run_baseline(
    settings: BaselineSettings,
    *,
    dependencies: BaselineDependencies | None = None,
) -> tuple[int, dict[str, Any] | None]:
    return generic.run_baseline(settings, protocol_path=PROTOCOL_PATH, dependencies=dependencies)


def inspect_baseline(settings: BaselineSettings) -> dict[str, Any]:
    return generic.inspect_baseline(settings, protocol_path=PROTOCOL_PATH)


def preflight(
    settings: BaselineSettings,
    *,
    dependencies: BaselineDependencies | None = None,
) -> dict[str, Any]:
    return generic.preflight(settings, protocol_path=PROTOCOL_PATH, dependencies=dependencies)


def build_parser():
    return generic.build_parser(default_protocol=PROTOCOL_PATH, expose_protocol=False)


def main(argv: list[str] | None = None) -> int:
    return generic.main(argv, default_protocol=PROTOCOL_PATH, expose_protocol=False)


if __name__ == "__main__":
    raise SystemExit(main())

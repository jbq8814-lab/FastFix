import importlib.util
from pathlib import Path


def load_runner():
    path = Path(__file__).parents[3] / "benchmarks" / "docker" / "validation_runner.py"
    spec = importlib.util.spec_from_file_location("fastfix_validation_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bounded_output_retains_head_and_tail() -> None:
    runner = load_runner()
    output = runner.BoundedOutput(100)
    output.add(b"a" * 60)
    output.add(b"b" * 1_000_000)
    assert output.truncated
    assert output.text().startswith("a" * 50)
    assert output.text().endswith("b" * 50)
    assert len(output.text()) < 200


def test_runner_only_accepts_pytest_and_ruff() -> None:
    runner = load_runner()
    assert runner.parser().parse_args(["--timeout-seconds", "5", "pytest"]).tool == "pytest"
    assert runner.parser().parse_args(["--timeout-seconds", "5", "ruff"]).tool == "ruff"

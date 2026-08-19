import inspect
import sys
from pathlib import Path

import pytest

from fastfix.sandbox.local import LocalValidationBackend
from fastfix.sandbox.models import ValidationExecution
from fastfix.security.paths import PathPolicyError
from fastfix.tools.validation import (
    RunPytestArgs,
    RunRuffArgs,
    WorkspaceValidationTools,
)


def workspace(tmp_path: Path, test_source: str = "def test_ok():\n    assert True\n") -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "app" / "other_dir").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text(test_source, encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir()
    return tmp_path


def tools(tmp_path: Path, test_source: str = "def test_ok():\n    assert True\n") -> WorkspaceValidationTools:
    return WorkspaceValidationTools(
        workspace(tmp_path, test_source),
        python_executable=Path(sys.executable),
    )


def test_targeted_pytest_passes_with_real_counts(tmp_path: Path) -> None:
    result = tools(tmp_path).run_pytest(RunPytestArgs(scope="targeted", targets=["tests/test_main.py::test_ok"]))
    assert result.ok
    assert result.metadata["passed"] == 1
    assert result.metadata["failed"] is None
    assert result.metadata["targets"] == ["tests/test_main.py::test_ok"]


def test_targeted_pytest_failure_is_observation(tmp_path: Path) -> None:
    result = tools(tmp_path, "def test_bad():\n    assert False\n").run_pytest(
        RunPytestArgs(scope="targeted", targets=["tests/test_main.py::test_bad"])
    )
    assert not result.ok and result.error_code == "validation_failed"
    assert result.metadata["failed"] == 1 and not result.metadata["timed_out"]


def test_regression_defaults_to_tests(tmp_path: Path) -> None:
    result = tools(tmp_path).run_pytest(RunPytestArgs(scope="regression"))
    assert result.ok and result.metadata["targets"] == ["tests"]
    assert result.metadata["required_targets"] == ["tests"] and result.metadata["scope_complete"]
    assert result.metadata["passed"] == 1


@pytest.mark.parametrize(
    ("targets",),  # noqa: PT006
    [
        (["tests/test_main.py"],),
        (["tests/test_main.py::test_ok"],),
        (["tests/unit"],),
    ],
)
def test_regression_rejects_incomplete_explicit_targets(tmp_path: Path, targets: list[str]) -> None:
    with pytest.raises(PathPolicyError) as error:
        tools(tmp_path).run_pytest(RunPytestArgs(scope="regression", targets=targets))
    assert error.value.code == "validation_scope_incomplete"
    assert str(error.value) == "Regression pytest must use the configured complete targets."


def test_regression_accepts_explicit_complete_historical_target(tmp_path: Path) -> None:
    result = tools(tmp_path).run_pytest(RunPytestArgs(scope="regression", targets=["tests"]))
    assert result.ok and result.metadata["targets"] == result.metadata["required_targets"] == ["tests"]


def test_empty_regression_targets_use_complete_configured_scope(tmp_path: Path) -> None:
    result = tools(tmp_path).run_pytest(RunPytestArgs(scope="regression", targets=[]))
    assert result.ok and result.metadata["targets"] == result.metadata["required_targets"] == ["tests"]


@pytest.mark.parametrize(
    "target",
    [
        "-k",
        "tests/test_main.py;whoami",
        "../tests/test_main.py",
        "app/main.py",
    ],
)
def test_pytest_target_injection_is_rejected(tmp_path: Path, target: str) -> None:
    validation = tools(tmp_path)
    with pytest.raises(PathPolicyError):
        validation.run_pytest(RunPytestArgs(scope="targeted", targets=[target]))


def test_pytest_timeout_is_structured(tmp_path: Path) -> None:
    result = tools(
        tmp_path,
        "import time\n\ndef test_slow():\n    time.sleep(10)\n",
    ).run_pytest(
        RunPytestArgs(
            scope="targeted",
            targets=["tests/test_main.py::test_slow"],
            timeout_seconds=5,
        )
    )
    assert not result.ok and result.error_code == "command_timeout"
    assert result.metadata["timed_out"]


def test_ruff_passes_and_failure_is_structured(tmp_path: Path) -> None:
    validation = tools(tmp_path)
    passed = validation.run_ruff(RunRuffArgs(paths=["app"]))
    assert passed.ok and passed.metadata["passed"] and passed.metadata["scope_complete"]
    assert passed.metadata["paths"] == passed.metadata["required_paths"] == ["app"]
    (tmp_path / "app" / "main.py").write_text("import os\n", encoding="utf-8")
    failed = validation.run_ruff(RunRuffArgs())
    assert not failed.ok and failed.error_code == "validation_failed"
    assert not failed.metadata["passed"]


def test_ruff_rejects_unrelated_subdirectory_after_source_change(tmp_path: Path) -> None:
    validation = tools(tmp_path)
    (tmp_path / "app" / "main.py").write_text("value = 2\n", encoding="utf-8")
    with pytest.raises(PathPolicyError) as error:
        validation.run_ruff(RunRuffArgs(paths=["app/other_dir"]))
    assert error.value.code == "validation_scope_incomplete"
    assert str(error.value) == "Ruff must use the configured complete source paths."


def test_empty_ruff_paths_use_complete_configured_scope(tmp_path: Path) -> None:
    result = tools(tmp_path).run_ruff(RunRuffArgs(paths=[]))
    assert result.ok and result.metadata["paths"] == result.metadata["required_paths"] == ["app"]


@pytest.mark.parametrize("path", ["--fix", "../app", "tests"])
def test_ruff_path_injection_is_rejected(tmp_path: Path, path: str) -> None:
    validation = tools(tmp_path)
    with pytest.raises(PathPolicyError):
        validation.run_ruff(RunRuffArgs(paths=[path]))


def test_validation_output_is_bounded(tmp_path: Path) -> None:
    result = tools(
        tmp_path,
        "def test_loud():\n    print('x' * 25000)\n    assert False\n",
    ).run_pytest(RunPytestArgs(scope="targeted", targets=["tests/test_main.py::test_loud"]))
    assert result.metadata["output_truncated"]
    assert len(result.output) < 20_100


def test_validation_subprocess_is_explicitly_non_shell() -> None:
    assert "shell=False" in inspect.getsource(LocalValidationBackend.run)


class StaticBackend:
    def run(self, **_) -> ValidationExecution:
        return ValidationExecution(returncode=0, output="1 passed\n", duration_seconds=0.1)


def test_validation_backend_configuration_is_exclusive(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    with pytest.raises(ValueError):
        WorkspaceValidationTools(root)
    with pytest.raises(ValueError):
        WorkspaceValidationTools(root, python_executable=Path(sys.executable), backend=StaticBackend())
    assert WorkspaceValidationTools(root, backend=StaticBackend()).run_pytest(RunPytestArgs(scope="regression")).ok

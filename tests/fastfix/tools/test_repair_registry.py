import subprocess
import sys
from pathlib import Path

from fastfix.sandbox.models import ValidationExecution
from fastfix.tools.repair import build_repair_registry


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        shell=False,
    )


def test_repair_registry_has_fixed_controlled_order(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "FastFix Tests")
    git(tmp_path, "config", "user.email", "fastfix@example.invalid")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "baseline")
    registry = build_repair_registry(tmp_path, python_executable=Path(sys.executable))
    assert registry.names() == (
        "show_tree",
        "read_file",
        "search_code",
        "inspect_fastapi_routes",
        "replace_text",
        "apply_patch",
        "run_pytest",
        "run_ruff",
        "show_git_diff",
        "rollback_changes",
    )
    assert "submit_repair" not in registry.names()
    route_schema = next(
        tool["function"] for tool in registry.get_openai_tools() if tool["function"]["name"] == "inspect_fastapi_routes"
    )
    assert route_schema["parameters"]["properties"]["max_files"]["default"] == 50
    assert route_schema["parameters"]["properties"]["max_routes"]["default"] == 100


class StaticBackend:
    def run(self, **_) -> ValidationExecution:
        return ValidationExecution(returncode=0, output="1 passed\n", duration_seconds=0.1)


def test_repair_registry_accepts_explicit_validation_backend(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "FastFix Tests")
    git(tmp_path, "config", "user.email", "fastfix@example.invalid")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "baseline")
    assert "run_pytest" in build_repair_registry(tmp_path, validation_backend=StaticBackend()).names()

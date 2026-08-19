from pathlib import Path

from fastfix.tools.repository import build_repository_registry

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"


def test_ff001_tree_is_visible_without_generated_caches() -> None:
    result = build_repository_registry(FIXTURE).execute("show_tree", {})
    assert result.ok
    assert "app/\n  __init__.py\n  main.py" in result.output
    assert "app/service.py" not in result.output
    assert "  service.py" in result.output
    assert "tests/\n  test_users.py" in result.output
    assert "__pycache__" not in result.output


def test_ff001_defect_file_and_search_results_are_readable() -> None:
    registry = build_repository_registry(FIXTURE)
    read_result = registry.execute("read_file", {"path": "app/main.py"})
    search_result = registry.execute("search_code", {"query": "fetch_user"})
    assert read_result.ok
    assert "16 |     return fetch_user(user_id)" in read_result.output
    assert search_result.ok
    assert "app/main.py:" in search_result.output
    assert "app/service.py:" in search_result.output


def test_ff001_sensitive_paths_are_rejected() -> None:
    registry = build_repository_registry(FIXTURE)
    assert registry.execute("read_file", {"path": ".git/config"}).error_code == "sensitive_path"
    assert registry.execute("read_file", {"path": ".env"}).error_code == "sensitive_path"


def test_ff001_openai_tool_schemas_are_complete() -> None:
    registry = build_repository_registry(FIXTURE)
    tools = registry.get_openai_tools()
    assert registry.names() == ("show_tree", "read_file", "search_code")
    assert [tool["function"]["name"] for tool in tools] == list(registry.names())
    assert all(tool["type"] == "function" and tool["function"]["parameters"]["type"] == "object" for tool in tools)

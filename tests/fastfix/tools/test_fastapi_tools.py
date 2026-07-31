import json
from pathlib import Path

import pytest

from fastfix.tools.fastapi import (
    FastApiTools,
    build_fastapi_registry,
    build_readonly_registry,
    register_fastapi_tools,
)
from fastfix.tools.registry import ToolRegistry
from fastfix.tools.repository import build_repository_registry


def write_route(path: Path, route: str = "/") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'from fastapi import FastAPI\napp = FastAPI()\n@app.get("{route}")\ndef handler(): pass\n',
        encoding="utf-8",
    )


def payload(result) -> dict:
    assert result.ok
    return json.loads(result.output)


def test_scans_python_files_stably_and_excludes_tests_and_caches(tmp_path: Path) -> None:
    write_route(tmp_path / "z.py", "/z")
    write_route(tmp_path / "app" / "a.py", "/a")
    write_route(tmp_path / "tests" / "test_routes.py", "/test")
    write_route(tmp_path / "__pycache__" / "cached.py", "/cached")
    result = payload(build_fastapi_registry(tmp_path).execute("inspect_fastapi_routes", {}))
    assert [(route["file"], route["path"]) for route in result["routes"]] == [
        ("app/a.py", "/a"),
        ("z.py", "/z"),
    ]
    assert result["scanned_files"] == 2
    assert result["route_count"] == 2
    assert not result["truncated"]


def test_syntax_error_does_not_block_valid_files(tmp_path: Path) -> None:
    write_route(tmp_path / "good.py")
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    result = payload(build_fastapi_registry(tmp_path).execute("inspect_fastapi_routes", {}))
    assert result["route_count"] == 1
    assert result["parse_errors"][0]["file"] == "bad.py"
    assert result["parse_errors"][0]["line"] == 1


def test_empty_repository_returns_zero_routes(tmp_path: Path) -> None:
    result = payload(build_fastapi_registry(tmp_path).execute("inspect_fastapi_routes", {}))
    assert result == {
        "parse_errors": [],
        "route_count": 0,
        "routes": [],
        "scanned_files": 0,
        "truncated": False,
        "truncation_reasons": [],
    }


def test_file_and_route_limits_return_partial_results(tmp_path: Path) -> None:
    write_route(tmp_path / "a.py", "/a")
    write_route(tmp_path / "b.py", "/b")
    registry = build_fastapi_registry(tmp_path)
    file_limited = payload(registry.execute("inspect_fastapi_routes", {"max_files": 1}))
    route_limited = payload(registry.execute("inspect_fastapi_routes", {"max_routes": 1}))
    assert file_limited["route_count"] == 1
    assert file_limited["truncation_reasons"] == ["file_limit_exceeded"]
    assert route_limited["route_count"] == 1
    assert route_limited["truncation_reasons"] == ["output_truncated"]


def test_large_file_and_output_are_bounded(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("x" * 100, encoding="utf-8")
    large = payload(FastApiTools(tmp_path, max_file_bytes=20).build_registry().execute("inspect_fastapi_routes", {}))
    assert large["parse_errors"][0]["message"] == "File exceeds the analysis size limit."
    assert large["truncated"]

    write_route(tmp_path / "routes.py", "/" + "x" * 300)
    bounded = FastApiTools(tmp_path, max_output_chars=300).build_registry().execute("inspect_fastapi_routes", {})
    assert len(bounded.output) <= 300
    assert payload(bounded)["truncation_reasons"] == ["output_truncated"]


@pytest.mark.parametrize("path", ["../outside", str(Path.cwd().anchor), ".env"])
def test_unsafe_paths_are_rejected(tmp_path: Path, path: str) -> None:
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    result = build_fastapi_registry(tmp_path).execute("inspect_fastapi_routes", {"path": path})
    assert not result.ok
    assert result.error_code in {"path_not_allowed", "sensitive_path"}


def test_non_python_file_is_an_invalid_path(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("text\n", encoding="utf-8")
    result = build_fastapi_registry(tmp_path).execute("inspect_fastapi_routes", {"path": "notes.txt"})
    assert not result.ok
    assert result.error_code == "invalid_path"


def test_allowed_paths_limit_analysis(tmp_path: Path) -> None:
    write_route(tmp_path / "app" / "main.py")
    write_route(tmp_path / "other" / "main.py")
    registry = build_fastapi_registry(tmp_path, allowed_paths=("app",))
    assert payload(registry.execute("inspect_fastapi_routes", {"path": "."}))["route_count"] == 1
    denied = registry.execute("inspect_fastapi_routes", {"path": "other"})
    assert not denied.ok
    assert denied.error_code == "path_not_allowed"


def test_symlink_escape_is_rejected(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    write_route(outside / "routes.py")
    link = tmp_path / "escape.py"
    try:
        link.symlink_to(outside / "routes.py")
    except OSError:
        pytest.skip("Current user cannot create symbolic links.")
    result = build_fastapi_registry(tmp_path).execute("inspect_fastapi_routes", {"path": "escape.py"})
    assert not result.ok
    assert result.error_code == "path_not_allowed"


def test_registry_name_schema_and_explicit_registration(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_fastapi_tools(registry, tmp_path)
    [tool] = registry.get_openai_tools()
    assert registry.names() == ("inspect_fastapi_routes",)
    assert tool["function"]["name"] == "inspect_fastapi_routes"
    assert tool["function"]["parameters"]["additionalProperties"] is False
    assert tool["function"]["parameters"]["properties"]["max_files"]["default"] == 50
    assert tool["function"]["parameters"]["properties"]["max_routes"]["default"] == 100
    assert build_readonly_registry(tmp_path).names() == (
        "show_tree",
        "read_file",
        "search_code",
        "inspect_fastapi_routes",
    )
    assert build_repository_registry(tmp_path).names() == ("show_tree", "read_file", "search_code")

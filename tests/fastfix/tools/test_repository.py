from pathlib import Path

import pytest

from fastfix.tools.repository import build_repository_registry


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "alpha.py").write_text("fetch_user(1)\nFETCH_USER = 2\n", encoding="utf-8")
    (tmp_path / "app" / "beta.py").write_text("fetch_user(2)\n", encoding="utf-8")
    (tmp_path / "app" / "notes.txt").write_text("fetch_user docs\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("assert fetch_user(1)\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cache.py").write_text("fetch_user\n", encoding="utf-8")
    return tmp_path


def test_show_tree_is_sorted_and_ignores_caches(repository: Path) -> None:
    result = build_repository_registry(repository).execute("show_tree", {})
    assert result.ok
    assert result.output.splitlines() == [
        "app/",
        "  alpha.py",
        "  beta.py",
        "  notes.txt",
        "tests/",
        "  test_app.py",
    ]
    assert result.metadata == {"root": ".", "entry_count": 6, "truncated": False}


def test_show_tree_depth_and_max_entries(repository: Path) -> None:
    registry = build_repository_registry(repository)
    shallow = registry.execute("show_tree", {"depth": 1})
    limited = registry.execute("show_tree", {"max_entries": 2})
    assert shallow.output.splitlines() == ["app/", "tests/"]
    assert limited.output.splitlines() == ["app/", "  alpha.py"]
    assert limited.metadata["truncated"] is True


def test_read_file_numbers_lines_and_ranges(repository: Path) -> None:
    registry = build_repository_registry(repository)
    result = registry.execute("read_file", {"path": "app/alpha.py"})
    ranged = registry.execute("read_file", {"path": "app/alpha.py", "start_line": 2, "end_line": 2})
    assert result.output == "1 | fetch_user(1)\n2 | FETCH_USER = 2"
    assert ranged.output == "2 | FETCH_USER = 2"
    assert ranged.metadata["start_line"] == 2
    assert ranged.metadata["end_line"] == 2


def test_read_file_max_lines_and_invalid_range(repository: Path) -> None:
    registry = build_repository_registry(repository)
    limited = registry.execute("read_file", {"path": "app/alpha.py", "max_lines": 1})
    invalid = registry.execute("read_file", {"path": "app/alpha.py", "start_line": 2, "end_line": 1})
    assert limited.output == "1 | fetch_user(1)"
    assert limited.metadata["truncated"] is True
    assert not invalid.ok
    assert invalid.error_code == "invalid_arguments"


def test_read_empty_and_non_utf8_file(repository: Path) -> None:
    (repository / "empty.txt").write_text("", encoding="utf-8")
    (repository / "binary.txt").write_bytes(b"prefix\xffsuffix\n")
    registry = build_repository_registry(repository)
    empty = registry.execute("read_file", {"path": "empty.txt"})
    binary = registry.execute("read_file", {"path": "binary.txt"})
    assert empty.ok and empty.output == "" and empty.metadata["total_lines"] == 0
    assert binary.ok
    assert "\ufffd" in binary.output


def test_read_file_rejects_directory(repository: Path) -> None:
    result = build_repository_registry(repository).execute("read_file", {"path": "app"})
    assert not result.ok
    assert result.error_code == "not_a_file"


def test_search_code_finds_stable_matches(repository: Path) -> None:
    result = build_repository_registry(repository).execute("search_code", {"query": "fetch_user"})
    assert result.ok
    assert result.output.splitlines() == [
        "app/alpha.py:1: fetch_user(1)",
        "app/beta.py:1: fetch_user(2)",
        "tests/test_app.py:1: assert fetch_user(1)",
    ]
    assert result.metadata == {"query": "fetch_user", "match_count": 3, "truncated": False}


def test_search_code_case_glob_limit_and_no_match(repository: Path) -> None:
    registry = build_repository_registry(repository)
    insensitive = registry.execute(
        "search_code",
        {"query": "FETCH_USER", "case_sensitive": False, "path": "app"},
    )
    text_only = registry.execute("search_code", {"query": "fetch_user", "glob": "*.txt"})
    limited = registry.execute("search_code", {"query": "fetch_user", "max_results": 1})
    missing = registry.execute("search_code", {"query": "not present"})
    assert insensitive.metadata["match_count"] == 3
    assert text_only.output == "app/notes.txt:1: fetch_user docs"
    assert limited.metadata == {"query": "fetch_user", "match_count": 1, "truncated": True}
    assert missing.ok and missing.output == "" and missing.metadata["match_count"] == 0


def test_search_code_limits_line_content(repository: Path) -> None:
    (repository / "app" / "long.py").write_text(f"needle {'x' * 400}\n", encoding="utf-8")
    result = build_repository_registry(repository).execute("search_code", {"query": "needle"})
    content = result.output.split(": ", 1)[1]
    assert len(content) == 300
    assert content.endswith("...")

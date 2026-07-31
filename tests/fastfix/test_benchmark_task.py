import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = ROOT / "benchmarks" / "tasks" / "ff-001-missing-await" / "task.json"


def test_ff001_metadata_references_existing_files():
    task = json.loads(TASK_FILE.read_text(encoding="utf-8"))
    assert task["task_id"] == "FF-001"
    assert task["provenance"] == "synthetic"
    assert task["failing_tests"] == ["tests/test_users.py::test_get_user_returns_user"]
    for key in ("fixture_repo", "issue_file", "gold_patch"):
        assert not Path(task[key]).is_absolute()
        assert (ROOT / task[key]).exists()
    for path in task["allowed_paths"] + task["buggy_files"]:
        assert not Path(path).is_absolute()
    assert (ROOT / task["fixture_repo"] / task["buggy_files"][0]).is_file()


def test_ff001_gold_patch_only_changes_declared_buggy_file():
    task = json.loads(TASK_FILE.read_text(encoding="utf-8"))
    patch = (ROOT / task["gold_patch"]).read_text(encoding="utf-8")
    changed_files = {
        line.split()[2].removeprefix("a/") for line in patch.splitlines() if line.startswith("diff --git ")
    }
    assert patch.strip()
    assert changed_files == {"app/main.py"}
    assert changed_files == set(task["buggy_files"])


def test_root_pytest_excludes_fixture_repositories():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'norecursedirs = ["benchmarks/fixture_repos"]' in config

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "fixture_repos" / "ff-001-missing-await"
PATCH = ROOT / "benchmarks" / "tasks" / "ff-001-missing-await" / "gold.patch"


def run(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def require(condition: bool, message: str, result: subprocess.CompletedProcess[str]) -> None:
    if not condition:
        raise RuntimeError(f"{message}\n{output(result)}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fastfix-ff001-") as temp:
        temp_dir = Path(temp)
        fixture = temp_dir / "fixture"
        shutil.copytree(FIXTURE, fixture)
        env = os.environ | {"UV_CACHE_DIR": str(temp_dir / "uv-cache")}
        pytest_command = ["uv", "run", "--project", str(fixture), "--extra", "test", "python", "-m", "pytest", "-q"]

        buggy = run(pytest_command, fixture, env)
        buggy_output = output(buggy)
        require(
            buggy.returncode != 0
            and re.findall(r"(\d+) failed", buggy_output) == ["1"]
            and re.findall(r"(\d+) passed", buggy_output) == ["1"],
            "Buggy fixture did not produce exactly 1 failed and 1 passed.",
            buggy,
        )
        print("BUGGY STATE: PASS（成功复现预期失败）")

        patch_check = run(["git", "apply", "--check", str(PATCH)], fixture, env)
        require(patch_check.returncode == 0, "Gold patch check failed.", patch_check)
        patch_apply = run(["git", "apply", str(PATCH)], fixture, env)
        require(patch_apply.returncode == 0, "Gold patch application failed.", patch_apply)
        print("PATCH APPLY: PASS")

        fixed = run(pytest_command, fixture, env)
        fixed_output = output(fixed)
        require(
            fixed.returncode == 0
            and not re.findall(r"(\d+) failed", fixed_output)
            and re.findall(r"(\d+) passed", fixed_output) == ["2"],
            "Fixed fixture did not produce exactly 2 passed.",
            fixed,
        )
        print("FIXED STATE: PASS")

        ruff = run(
            ["uv", "run", "--project", str(fixture), "--extra", "test", "python", "-m", "ruff", "check", "."],
            fixture,
            env,
        )
        require(ruff.returncode == 0, "Ruff check failed.", ruff)
        print("RUFF: PASS")


if __name__ == "__main__":
    main()

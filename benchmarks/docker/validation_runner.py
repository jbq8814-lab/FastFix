import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

SOURCE = Path("/candidate")
WORKSPACE = Path("/workspace")
RESULT = Path("/tmp/fastfix-validation-result.json")
MAX_OUTPUT_BYTES = 20_000
EXCLUDED = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
RUNNER_ERROR_EXIT = 125


class BoundedOutput:
    def __init__(self, limit: int = MAX_OUTPUT_BYTES):
        self.half = limit // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = chunk
        if len(self.head) < self.half:
            size = min(self.half - len(self.head), len(remaining))
            self.head.extend(remaining[:size])
            remaining = remaining[size:]
        if remaining:
            self.tail.extend(remaining)
            if len(self.tail) > self.half:
                del self.tail[: len(self.tail) - self.half]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def text(self) -> str:
        marker = b"\n... output truncated ...\n" if self.truncated else b""
        return bytes(self.head + marker + self.tail).decode("utf-8", errors="replace")


def environment() -> dict[str, str]:
    return {
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }


def copy_workspace() -> None:
    for source in SOURCE.iterdir():
        if source.name in EXCLUDED:
            continue
        destination = WORKSPACE / source.name
        if source.is_symlink():
            destination.symlink_to(source.readlink())
        elif source.is_dir():
            shutil.copytree(
                source,
                destination,
                symlinks=True,
                ignore=lambda _, names: [name for name in names if name in EXCLUDED],
            )
        else:
            shutil.copy2(source, destination)


def terminate_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        pass


def execute(tool: str, arguments: list[str], timeout_seconds: int) -> dict[str, object]:
    output = BoundedOutput()
    start = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", tool, *arguments],
        cwd=WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        env=environment(),
        start_new_session=True,
    )

    def read_output() -> None:
        if process.stdout is None:
            return
        while chunk := process.stdout.read(64 * 1024):
            output.add(chunk)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_group(process)
        returncode = 124
    reader.join(timeout=5)
    return {
        "returncode": returncode,
        "output": output.text(),
        "duration_seconds": time.monotonic() - start,
        "timed_out": timed_out,
        "output_truncated": output.truncated,
        "runner_error": None,
        "metadata": {
            "container_uid": os.geteuid(),
            "candidate_writable": os.access(SOURCE, os.W_OK),
            "docker_socket_present": Path("/var/run/docker.sock").exists(),
        },
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("tool", choices=("pytest", "ruff"))
    value.add_argument("--timeout-seconds", type=int, required=True)
    value.add_argument("arguments", nargs=argparse.REMAINDER)
    return value


def main() -> int:
    args = parser().parse_args()
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    payload: dict[str, object]
    try:
        copy_workspace()
        payload = execute(args.tool, arguments, args.timeout_seconds)
    except Exception as error:
        payload = {
            "returncode": RUNNER_ERROR_EXIT,
            "output": "",
            "duration_seconds": 0.0,
            "timed_out": False,
            "output_truncated": False,
            "runner_error": type(error).__name__,
            "metadata": {},
        }
    serialized = json.dumps(payload)
    try:
        RESULT.write_text(serialized, encoding="utf-8")
    except OSError as error:
        payload = {
            "returncode": RUNNER_ERROR_EXIT,
            "output": "",
            "duration_seconds": 0.0,
            "timed_out": False,
            "output_truncated": False,
            "runner_error": type(error).__name__,
            "metadata": {},
        }
        serialized = json.dumps(payload)
    print(serialized, flush=True)
    return int(payload["returncode"])


if __name__ == "__main__":
    raise SystemExit(main())

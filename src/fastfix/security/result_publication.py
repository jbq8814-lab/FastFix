import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class ResultPublicationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def publication_state_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.publication.json")


def read_publication_state(destination: Path) -> dict[str, Any] | None:
    path = publication_state_path(destination)
    if path.is_symlink():
        return {"state": "invalid", "error": "result_publication_state_invalid"}
    if not path.exists():
        return None
    if not path.is_file():
        return {"state": "invalid", "error": "result_publication_state_invalid"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "invalid", "error": "result_publication_state_invalid"}
    if not isinstance(value, dict) or not isinstance(value.get("state"), str):
        return {"state": "invalid", "error": "result_publication_state_invalid"}
    return value


def _write_state(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    target = path if exclusive else path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with target.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    if not exclusive:
        os.replace(target, path)


def _snapshot(root: Path, *, code: str) -> dict[str, str]:
    try:
        if root.is_symlink() or not root.is_dir():
            raise OSError("Result bundle is not a directory.")
        paths = sorted(root.rglob("*"))
        if any(path.is_symlink() or (not path.is_dir() and not path.is_file()) for path in paths):
            raise OSError("Result bundle contains an unsupported entry.")
        files = [path for path in paths if path.is_file()]
        hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        for path in files:
            if path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
        return hashes
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResultPublicationError(code, "Result bundle could not be enumerated and read.") from error


def create_result_manifest(root: Path, expected_files: Iterable[str]) -> dict[str, str]:
    manifest = _snapshot(root, code="result_publication_failed")
    if set(manifest) != set(expected_files):
        raise ResultPublicationError("result_publication_failed", "Result bundle file set is incomplete.")
    return manifest


def verify_published_result(root: Path, manifest: dict[str, str]) -> None:
    if _snapshot(root, code="result_readability_failed") != manifest:
        raise ResultPublicationError(
            "result_readability_failed",
            "Published result file set or content hash differs from the prepared bundle.",
        )


def restore_windows_acl(
    path: Path,
    *,
    platform: str | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if (platform or os.name) != "nt":
        return
    try:
        result = run_command(
            ["icacls", str(path), "/inheritance:e", "/t", "/c"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ResultPublicationError(
            "windows_acl_repair_failed",
            "Windows result ACL inheritance could not be restored.",
        ) from error
    if result.returncode:
        raise ResultPublicationError(
            "windows_acl_repair_failed",
            "Windows result ACL inheritance could not be restored.",
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def publish_result_bundle(
    temporary: Path,
    destination: Path,
    manifest: dict[str, str],
    *,
    acl_restorer: Callable[[Path], None] = restore_windows_acl,
    verifier: Callable[[Path, dict[str, str]], None] = verify_published_result,
) -> None:
    state_path = publication_state_path(destination)
    if destination.exists() or destination.is_symlink():
        raise ResultPublicationError("result_exists", "Result directory already exists.")
    if state_path.exists():
        raise ResultPublicationError(
            "result_publication_incomplete",
            "A previous result publication did not complete.",
        )
    state = {
        "schema_version": 1,
        "state": "publishing",
        "destination": destination.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _write_state(state_path, state, exclusive=True)
    except OSError as error:
        raise ResultPublicationError(
            "result_publication_failed", "Result publication state could not be created."
        ) from error
    published = False
    try:
        if destination.exists() or destination.is_symlink():
            raise ResultPublicationError("result_exists", "Result directory already exists.")
        try:
            os.replace(temporary, destination)
        except OSError as error:
            raise ResultPublicationError(
                "result_publication_failed", "Result directory could not be published."
            ) from error
        published = True
        acl_restorer(destination)
        verifier(destination, manifest)
        state_path.unlink()
    except BaseException as error:
        publication_error = (
            error
            if isinstance(error, ResultPublicationError)
            else ResultPublicationError(
                "result_publication_failed",
                "Result publication did not complete.",
            )
        )
        quarantine: Path | None = None
        if published and destination.exists():
            quarantine = destination.with_name(f".{destination.name}.failed-{uuid4().hex}")
            try:
                os.replace(destination, quarantine)
            except OSError:
                quarantine = None
        state |= {
            "state": "failed",
            "error": publication_error.code,
            "quarantine": quarantine.name if quarantine is not None else None,
        }
        try:
            _write_state(state_path, state)
        except OSError:
            pass
        if publication_error is error:
            raise
        raise publication_error from error

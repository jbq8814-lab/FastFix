import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from fastfix.approval.models import (
    ApprovalRequest,
    ManifestEntry,
    PackageManifest,
    ValidationResultSummary,
    ValidationSummary,
)
from fastfix.repair.state import RepairSessionState
from fastfix.security.paths import PathPolicyError, WorkspacePathPolicy

PACKAGE_FILES = ("approval-request.json", "patch.diff", "validation-summary.json")
TASK_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_CHANGED_FILES = 5
MAX_CHANGED_LINES = 300
MAX_PATCH_CHARS = 50_000


class ApprovalPackageError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _RepositorySnapshot:
    head: str
    branch: str | None
    status: str
    git_directory: Path


@dataclass(frozen=True)
class _Patch:
    content: str
    changed_files: list[str]
    added_lines: int
    deleted_lines: int


class ApprovalPackageManager:
    def __init__(
        self,
        output_root: Path,
        *,
        allowed_source_paths: tuple[str, ...] = ("app",),
        timeout_seconds: int = 30,
    ):
        if not output_root.is_dir():
            raise ApprovalPackageError("invalid_output_root", "Output root must be an existing directory.")
        self.output_root = output_root.resolve()
        self.allowed_source_paths = allowed_source_paths
        self.timeout_seconds = timeout_seconds
        self.git = shutil.which("git")
        if self.git is None:
            raise ApprovalPackageError("git_not_found", "Git executable was not found.")

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    def _git(
        self,
        repository: Path,
        arguments: list[str],
        *,
        operation: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.git, "-C", str(repository), *arguments],
                cwd=repository.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                shell=False,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as error:
            raise ApprovalPackageError("git_timeout", f"Git timed out during {operation}.") from error
        if result.returncode not in allowed_returncodes:
            raise ApprovalPackageError("git_command_failed", f"Git failed during {operation}.")
        return result

    def _snapshot(self, repository: Path, *, clean: bool) -> _RepositorySnapshot:
        if not repository.is_dir():
            raise ApprovalPackageError("invalid_repository", "Repository must be an existing directory.")
        repository = repository.resolve()
        worktree = self._git(
            repository,
            ["rev-parse", "--is-inside-work-tree"],
            operation="repository validation",
        )
        if worktree.stdout.strip() != "true":
            raise ApprovalPackageError("invalid_repository", "Repository must be a Git worktree.")
        head = self._git(
            repository,
            ["rev-parse", "--verify", "HEAD^{commit}"],
            operation="HEAD validation",
        ).stdout.strip()
        branch_result = self._git(
            repository,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            operation="branch inspection",
            allowed_returncodes=(0, 1),
        )
        status = self._git(
            repository,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            operation="status inspection",
        ).stdout
        if clean and status.strip():
            raise ApprovalPackageError("source_not_clean", "Source repository must be completely clean.")
        git_directory = Path(
            self._git(
                repository,
                ["rev-parse", "--absolute-git-dir"],
                operation="Git directory inspection",
            ).stdout.strip()
        ).resolve()
        return _RepositorySnapshot(
            head=head,
            branch=branch_result.stdout.strip() or None,
            status=status,
            git_directory=git_directory,
        )

    def _validate_repositories(
        self,
        source: Path,
        candidate: Path,
        source_head: str,
    ) -> tuple[_RepositorySnapshot, _RepositorySnapshot]:
        source = source.resolve()
        candidate = candidate.resolve()
        if source == candidate:
            raise ApprovalPackageError("candidate_not_independent", "Source and candidate must be different.")
        if self.output_root == source or self.output_root.is_relative_to(source):
            raise ApprovalPackageError("unsafe_output_root", "Output root must be outside the source repository.")
        if self.output_root == candidate or self.output_root.is_relative_to(candidate):
            raise ApprovalPackageError("unsafe_output_root", "Output root must be outside the candidate repository.")
        source_snapshot = self._snapshot(source, clean=True)
        if source_snapshot.head != source_head:
            raise ApprovalPackageError("source_head_mismatch", "Source HEAD changed after candidate creation.")
        candidate_snapshot = self._snapshot(candidate, clean=False)
        if not (candidate / ".git").is_dir() or (candidate / ".git").is_symlink():
            raise ApprovalPackageError(
                "candidate_not_independent", "Candidate must have an independent .git directory."
            )
        if candidate_snapshot.git_directory != (candidate / ".git").resolve():
            raise ApprovalPackageError("candidate_not_independent", "Candidate Git metadata must be local.")
        if candidate_snapshot.git_directory == source_snapshot.git_directory:
            raise ApprovalPackageError("candidate_not_independent", "Candidate must not share source Git metadata.")
        if candidate_snapshot.head != source_head:
            raise ApprovalPackageError("candidate_head_mismatch", "Candidate HEAD does not match the source snapshot.")
        if candidate_snapshot.branch is not None:
            raise ApprovalPackageError("candidate_not_detached", "Candidate must use detached HEAD.")
        if self._git(candidate, ["remote"], operation="candidate remote inspection").stdout.strip():
            raise ApprovalPackageError("candidate_has_remote", "Candidate must not have Git remotes.")
        staged = self._git(
            candidate,
            ["diff", "--cached", "--quiet", "HEAD", "--"],
            operation="candidate staged change inspection",
            allowed_returncodes=(0, 1),
        )
        if staged.returncode:
            raise ApprovalPackageError("candidate_staged_changes", "Candidate must not contain staged changes.")
        if self._git(
            candidate,
            ["ls-files", "--others", "--exclude-standard"],
            operation="candidate untracked file inspection",
        ).stdout.strip():
            raise ApprovalPackageError("candidate_untracked_files", "Candidate must not contain untracked files.")
        return source_snapshot, candidate_snapshot

    @staticmethod
    def _parse_name_status(output: str) -> list[str]:
        paths = []
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) != 2 or fields[0] != "M":
                raise ApprovalPackageError("patch_operation_not_allowed", "Patch may only modify existing files.")
            paths.append(fields[1])
        return paths

    def _patch(self, candidate: Path) -> _Patch:
        name_status = self._git(
            candidate,
            ["diff", "--name-status", "--find-renames", "--find-copies", "HEAD", "--"],
            operation="patch name inspection",
        )
        changed_files = self._parse_name_status(name_status.stdout)
        if not changed_files:
            raise ApprovalPackageError("empty_patch", "Candidate must contain a non-empty working-tree diff.")
        if len(changed_files) > MAX_CHANGED_FILES:
            raise ApprovalPackageError("patch_too_large", "Patch may modify at most 5 files.")

        policy = WorkspacePathPolicy(candidate, allowed_paths=self.allowed_source_paths)
        for path in changed_files:
            if path == "tests" or path.startswith("tests/"):
                raise ApprovalPackageError("tests_modified", "Patch must not modify tests.")
            try:
                policy.resolve(path, expect="file", must_exist=True)
            except PathPolicyError as error:
                raise ApprovalPackageError("patch_out_of_scope", "Patch modifies a disallowed path.") from error

        raw = self._git(candidate, ["diff", "--raw", "--no-abbrev", "HEAD", "--"], operation="patch mode inspection")
        for line in raw.stdout.splitlines():
            metadata = line.split("\t", 1)[0].split()
            modes = [value.lstrip(":") for value in metadata[:2]]
            if len(metadata) < 5 or "160000" in modes or modes[0] != modes[1]:
                raise ApprovalPackageError(
                    "patch_operation_not_allowed", "Patch changes a forbidden Git object or mode."
                )

        numstat = self._git(candidate, ["diff", "--numstat", "HEAD", "--"], operation="patch size inspection")
        added = deleted = 0
        numstat_paths = []
        for line in numstat.stdout.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                raise ApprovalPackageError("binary_patch", "Binary patches are not allowed.")
            added += int(fields[0])
            deleted += int(fields[1])
            numstat_paths.append(fields[2])
        if numstat_paths != changed_files:
            raise ApprovalPackageError("patch_inconsistent", "Git patch metadata is inconsistent.")
        if added + deleted > MAX_CHANGED_LINES:
            raise ApprovalPackageError("patch_too_large", "Patch may add or delete at most 300 lines.")
        if self._git(
            candidate,
            ["-c", "core.whitespace=cr-at-eol", "diff", "--check", "--"],
            operation="patch whitespace validation",
            allowed_returncodes=(0, 2),
        ).returncode:
            raise ApprovalPackageError("patch_check_failed", "Patch failed Git whitespace validation.")
        patch = self._git(
            candidate,
            ["diff", "--no-ext-diff", "--no-color", "HEAD", "--"],
            operation="patch export",
        ).stdout.replace("\r\n", "\n")
        if not patch or len(patch) > MAX_PATCH_CHARS:
            raise ApprovalPackageError("patch_too_large", "Patch is empty or exceeds 50,000 characters.")
        return _Patch(patch, changed_files, added, deleted)

    @staticmethod
    def _validated_result(result: dict[str, object] | None, *, ruff: bool = False) -> ValidationResultSummary:
        if result is None:
            raise ApprovalPackageError("validation_incomplete", "Required validation result is missing.")
        passed = result.get("returncode") == 0 and result.get("timed_out") is False
        if ruff:
            passed = passed and result.get("passed") is True
        if not passed or result.get("error_code") is not None:
            raise ApprovalPackageError("validation_failed", "Required validation did not pass.")
        try:
            return ValidationResultSummary(
                passed=True,
                returncode=0,
                timed_out=False,
                duration_seconds=float(result.get("duration_seconds", 0.0)),
                passed_count=result.get("passed") if not ruff and type(result.get("passed")) is int else None,
                failed_count=result.get("failed") if type(result.get("failed")) is int else None,
                skipped_count=result.get("skipped") if type(result.get("skipped")) is int else None,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise ApprovalPackageError("validation_invalid", "Validation metadata is invalid.") from error

    def _validation(self, state: RepairSessionState) -> ValidationSummary:
        results = (state.targeted_test_result, state.regression_test_result, state.ruff_result)
        if (
            not state.changed_files
            or not state.regression_scope_complete
            or not state.ruff_scope_complete
            or not all(
                revision == state.revision
                for revision in (state.targeted_test_revision, state.regression_test_revision, state.ruff_revision)
            )
            or not all(result and result.get("validation_epoch") == state.validation_epoch for result in results)
        ):
            raise ApprovalPackageError("validation_incomplete", "Validation revisions are incomplete or stale.")
        validated = (
            self._validated_result(state.targeted_test_result),
            self._validated_result(state.regression_test_result),
            self._validated_result(state.ruff_result, ruff=True),
        )
        image_ids = [result.get("image_id") for result in results if result is not None]
        network_modes = [result.get("network_mode") for result in results if result is not None]
        if (
            len(image_ids) != 3
            or not all(isinstance(value, str) and value for value in image_ids)
            or len(set(image_ids)) != 1
            or network_modes != ["none", "none", "none"]
        ):
            raise ApprovalPackageError(
                "sandbox_validation_required", "Validation must use one restricted Docker image."
            )
        return ValidationSummary(
            revision=state.revision,
            validation_epoch=state.validation_epoch,
            reopen_count=state.reopen_count,
            last_reopen_reason=state.last_reopen_reason,
            reopen_history=state.reopen_history,
            sandbox_image_id=image_ids[0],
            sandbox_network_mode="none",
            targeted=validated[0],
            regression=validated[1],
            ruff=validated[2],
        )

    @staticmethod
    def _json(model: BaseModel) -> bytes:
        payload = model.model_dump(mode="json")
        return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _write_file(self, path: Path, content: bytes) -> None:
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())

    @staticmethod
    def _remove_readonly(function, path: str, _: object) -> None:
        target = Path(path)
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
        function(path)

    def _write_package(
        self,
        request: ApprovalRequest,
        patch: _Patch,
        validation: ValidationSummary,
    ) -> Path:
        final = self.output_root / request.request_id
        if final.exists() or final.is_symlink():
            raise ApprovalPackageError("package_exists", "Approval package already exists.")
        temporary = Path(tempfile.mkdtemp(prefix=f".{request.request_id}.tmp-", dir=self.output_root))
        try:
            contents = {
                "approval-request.json": self._json(request),
                "patch.diff": patch.content.encode("utf-8"),
                "validation-summary.json": self._json(validation),
            }
            for name, content in contents.items():
                self._write_file(temporary / name, content)
            manifest = PackageManifest(
                request_id=request.request_id,
                files=[
                    ManifestEntry(path=name, bytes=len(content), sha256=self._sha256(content))
                    for name, content in sorted(contents.items())
                ],
            )
            self._write_file(temporary / "manifest.json", self._json(manifest))
            os.replace(temporary, final)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary, onerror=self._remove_readonly)
            raise
        for path in final.iterdir():
            try:
                path.chmod(stat.S_IREAD)
            except OSError:
                pass
        return final

    @staticmethod
    def _new_request_id() -> str:
        return str(uuid4())

    def create(
        self,
        *,
        task_id: str,
        source: Path,
        candidate: Path,
        source_head: str,
        repair_state: RepairSessionState,
    ) -> Path:
        if not TASK_ID.fullmatch(task_id):
            raise ApprovalPackageError("invalid_task_id", "Task ID contains unsupported characters.")
        request_id = self._new_request_id()
        final = self.output_root / request_id
        if final.exists() or final.is_symlink():
            raise ApprovalPackageError("package_exists", "Approval package already exists.")
        source = source.resolve()
        candidate = candidate.resolve()
        source_before, candidate_before = self._validate_repositories(source, candidate, source_head)
        patch = self._patch(candidate)
        validation = self._validation(repair_state)
        if sorted(repair_state.changed_files) != patch.changed_files:
            raise ApprovalPackageError("validation_inconsistent", "Validated files do not match the candidate patch.")
        patch_sha256 = self._sha256(patch.content.encode("utf-8"))
        request = ApprovalRequest(
            request_id=request_id,
            task_id=task_id,
            created_at=datetime.now(timezone.utc),
            source_head=source_head,
            candidate_head=candidate_before.head,
            patch_sha256=patch_sha256,
            changed_files=patch.changed_files,
            added_lines=patch.added_lines,
            deleted_lines=patch.deleted_lines,
            targeted_tests_passed=True,
            regression_tests_passed=True,
            ruff_passed=True,
            validation_revision=validation.revision,
            validation_epoch=validation.validation_epoch,
            reopen_count=validation.reopen_count,
            sandbox_image_id=validation.sandbox_image_id,
            sandbox_network_mode=validation.sandbox_network_mode,
            risk_notes=["Candidate patch is pending explicit human approval."],
        )
        package = self._write_package(request, patch, validation)
        try:
            if self._snapshot(source, clean=True) != source_before:
                raise ApprovalPackageError("source_changed", "Source repository changed during package creation.")
            if self._snapshot(candidate, clean=False) != candidate_before:
                raise ApprovalPackageError("candidate_changed", "Candidate repository changed during package creation.")
            self.verify_package(package)
        except BaseException:
            shutil.rmtree(package, onerror=self._remove_readonly)
            raise
        return package

    def verify_package(self, package: Path) -> ApprovalRequest:
        if package.is_symlink():
            raise ApprovalPackageError("invalid_package", "Approval package must not be a symbolic link.")
        package = package.resolve()
        if not package.is_dir() or package.is_symlink():
            raise ApprovalPackageError("invalid_package", "Approval package must be a directory.")
        try:
            manifest = PackageManifest.model_validate_json((package / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise ApprovalPackageError("manifest_invalid", "Package manifest is missing or invalid.") from error
        if manifest.request_id != package.name:
            raise ApprovalPackageError("manifest_invalid", "Manifest request ID does not match the package directory.")
        for entry in manifest.files:
            relative = PurePosixPath(entry.path)
            if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
                raise ApprovalPackageError("manifest_path_invalid", "Manifest path is unsafe.")
        entries = {entry.path: entry for entry in manifest.files}
        if len(entries) != len(manifest.files) or set(entries) != set(PACKAGE_FILES):
            raise ApprovalPackageError("manifest_invalid", "Manifest file list is incomplete or duplicated.")
        if {path.name for path in package.iterdir()} != {*PACKAGE_FILES, "manifest.json"}:
            raise ApprovalPackageError("package_contents_invalid", "Package contains unexpected files.")
        for name, entry in entries.items():
            path = package / name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(package):
                raise ApprovalPackageError("manifest_path_invalid", "Manifest path is unsafe.")
            content = path.read_bytes()
            if len(content) != entry.bytes or self._sha256(content) != entry.sha256:
                raise ApprovalPackageError("package_tampered", "Approval package content hash does not match.")
        try:
            request = ApprovalRequest.model_validate_json(
                (package / "approval-request.json").read_text(encoding="utf-8")
            )
            validation = ValidationSummary.model_validate_json(
                (package / "validation-summary.json").read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as error:
            raise ApprovalPackageError("package_invalid", "Approval package metadata is invalid.") from error
        if request.request_id != manifest.request_id:
            raise ApprovalPackageError("package_invalid", "Approval request ID does not match its manifest.")
        if request.patch_sha256 != self._sha256((package / "patch.diff").read_bytes()):
            raise ApprovalPackageError("package_tampered", "Patch hash does not match the approval request.")
        if (
            request.validation_revision != validation.revision
            or request.validation_epoch != validation.validation_epoch
            or request.reopen_count != validation.reopen_count
            or request.sandbox_image_id != validation.sandbox_image_id
            or request.sandbox_network_mode != validation.sandbox_network_mode
        ):
            raise ApprovalPackageError("package_invalid", "Approval request and validation summary do not match.")
        return request

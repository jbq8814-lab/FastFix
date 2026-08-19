import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from fastfix.approval.models import (
    ApplicationRecord,
    ApprovalActionResult,
    ApprovalDecision,
    ApprovalRequest,
    DecisionRecord,
    RollbackRecord,
)
from fastfix.approval.package import ApprovalPackageError, ApprovalPackageManager
from fastfix.security.paths import PathPolicyError, WorkspacePathPolicy
from fastfix.tools.editing import DIFF_HEADER, FORBIDDEN_PATCH_MARKERS
from fastfix.workspace.candidate import CandidateWorkspace, CandidateWorkspaceError

MAX_CHANGED_FILES = 5
MAX_CHANGED_LINES = 300
MAX_PATCH_CHARS = 50_000


class ApprovalActionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ApprovalActionManager:
    def __init__(
        self,
        actions_root: Path,
        *,
        package_manager: ApprovalPackageManager,
        allowed_source_paths: tuple[str, ...] = ("app",),
        timeout_seconds: int = 30,
    ):
        if not actions_root.is_dir():
            raise ApprovalActionError("approval_invalid", "Actions root must be an existing directory.")
        self.actions_root = actions_root.resolve()
        self.package_manager = package_manager
        self.allowed_source_paths = allowed_source_paths
        self.timeout_seconds = timeout_seconds
        self.git = shutil.which("git")
        if self.git is None:
            raise ApprovalActionError("approval_invalid", "Git executable was not found.")

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    def _git(
        self,
        repository: Path,
        arguments: list[str],
        *,
        input_text: str | None = None,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        try:
            command = [self.git, "-C", str(repository), *arguments]
            if input_text is None:
                result = subprocess.run(
                    command,
                    cwd=repository.parent,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    shell=False,
                    env=self._environment(),
                )
            else:
                binary = subprocess.run(
                    command,
                    cwd=repository.parent,
                    input=input_text.encode("utf-8"),
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    shell=False,
                    env=self._environment(),
                )
                result = subprocess.CompletedProcess(
                    binary.args,
                    binary.returncode,
                    binary.stdout.decode("utf-8", errors="replace"),
                    binary.stderr.decode("utf-8", errors="replace"),
                )
        except subprocess.TimeoutExpired as error:
            raise ApprovalActionError("patch_apply_failed", "Git command timed out.") from error
        if result.returncode not in allowed_returncodes:
            raise ApprovalActionError("patch_apply_failed", "Git command failed.")
        return result

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _json(model: BaseModel) -> bytes:
        return (json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )

    @staticmethod
    def _write_file(path: Path, content: bytes) -> None:
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())

    @staticmethod
    def _remove_readonly(function, path: str, _: object) -> None:
        target = Path(path)
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
        function(path)

    def _action_path(self, request_id: str) -> Path:
        return self.actions_root / request_id

    def _ensure_new_action(self, request_id: str) -> Path:
        action = self._action_path(request_id)
        if action.exists() or action.is_symlink():
            raise ApprovalActionError("approval_already_decided", "Approval request already has a final decision.")
        return action

    def _publish_action(self, request_id: str, files: dict[str, bytes]) -> Path:
        final = self._ensure_new_action(request_id)
        temporary = Path(tempfile.mkdtemp(prefix=f".{request_id}.tmp-", dir=self.actions_root))
        try:
            for name, content in files.items():
                self._write_file(temporary / name, content)
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

    def _package(self, package: Path) -> tuple[ApprovalRequest, str, str]:
        try:
            request = self.package_manager.verify_package(package)
            patch = (package / "patch.diff").read_text(encoding="utf-8")
            manifest_sha256 = self._sha256((package / "manifest.json").read_bytes())
        except (ApprovalPackageError, OSError) as error:
            raise ApprovalActionError("approval_package_invalid", "Approval package verification failed.") from error
        return request, patch, manifest_sha256

    def _validate_roots(self, source: Path, candidate: CandidateWorkspace, package: Path) -> None:
        for path in (self.actions_root, package.resolve()):
            if path == source or path.is_relative_to(source):
                raise ApprovalActionError("approval_invalid", "Action and package paths must be outside Source.")
            if path == candidate.path or path.is_relative_to(candidate.path):
                raise ApprovalActionError("approval_invalid", "Action and package paths must be outside Candidate.")

    def _source_state(self, source: Path) -> tuple[str, str, str, str]:
        if not source.is_dir():
            raise ApprovalActionError("source_changed", "Source must be an existing Git repository.")
        try:
            worktree = self._git(source, ["rev-parse", "--is-inside-work-tree"]).stdout.strip()
            head = self._git(source, ["rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
            staged = self._git(
                source,
                ["diff", "--cached", "--name-only", "HEAD", "--"],
            ).stdout
            untracked = self._git(source, ["ls-files", "--others", "--exclude-standard"]).stdout
            diff = self._git(source, ["diff", "--no-ext-diff", "--no-color", "HEAD", "--"]).stdout.replace("\r\n", "\n")
        except ApprovalActionError as error:
            raise ApprovalActionError("source_changed", "Unable to inspect source repository.") from error
        if worktree != "true":
            raise ApprovalActionError("source_changed", "Source must be a Git worktree.")
        return head, staged, untracked, diff

    def _clean_source(self, source: Path, expected_head: str) -> None:
        head, staged, untracked, diff = self._source_state(source)
        if head != expected_head or staged.strip() or untracked.strip() or diff:
            raise ApprovalActionError("source_changed", "Source repository is not at the approved clean state.")

    def _validate_patch(self, source: Path, patch: str, request: ApprovalRequest) -> None:
        if not patch or len(patch) > MAX_PATCH_CHARS or any(marker in patch for marker in FORBIDDEN_PATCH_MARKERS):
            raise ApprovalActionError("approval_package_invalid", "Approval patch contains a forbidden operation.")
        if 'diff --git "' in patch or "\ndiff --git '" in patch:
            raise ApprovalActionError("approval_package_invalid", "Quoted patch paths are not supported.")
        headers = DIFF_HEADER.findall(patch)
        if not headers or len(headers) > MAX_CHANGED_FILES:
            raise ApprovalActionError("approval_package_invalid", "Approval patch has an invalid file count.")
        paths = []
        policy = WorkspacePathPolicy(source, allowed_paths=self.allowed_source_paths)
        for before, after in headers:
            if before != after or before.startswith("/") or ".." in Path(before).parts:
                raise ApprovalActionError("approval_package_invalid", "Approval patch path is invalid.")
            if before == "tests" or before.startswith("tests/"):
                raise ApprovalActionError("approval_package_invalid", "Approval patch must not modify tests.")
            try:
                policy.resolve(before, expect="file", must_exist=True)
            except PathPolicyError as error:
                raise ApprovalActionError("approval_package_invalid", "Approval patch is out of scope.") from error
            paths.append(Path(before).as_posix())
        sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
        for section, path in zip((item for item in sections if item), paths, strict=True):
            old_headers = re.findall(r"^--- (.+)$", section, re.MULTILINE)
            new_headers = re.findall(r"^\+\+\+ (.+)$", section, re.MULTILINE)
            if old_headers != [f"a/{path}"] or new_headers != [f"b/{path}"]:
                raise ApprovalActionError("approval_package_invalid", "Approval patch headers are inconsistent.")
        added = sum(line.startswith("+") and not line.startswith("+++") for line in patch.splitlines())
        deleted = sum(line.startswith("-") and not line.startswith("---") for line in patch.splitlines())
        if added + deleted > MAX_CHANGED_LINES:
            raise ApprovalActionError("approval_package_invalid", "Approval patch exceeds the line limit.")
        if (
            paths != request.changed_files
            or added != request.added_lines
            or deleted != request.deleted_lines
            or self._sha256(patch.encode("utf-8")) != request.patch_sha256
        ):
            raise ApprovalActionError("approval_package_invalid", "Approval patch metadata is inconsistent.")

    def _diff_metadata(self, source: Path) -> tuple[list[str], int, int]:
        names = self._git(source, ["diff", "--name-only", "HEAD", "--"]).stdout.splitlines()
        added = deleted = 0
        for line in self._git(source, ["diff", "--numstat", "HEAD", "--"]).stdout.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                raise ApprovalActionError("apply_verification_failed", "Applied diff metadata is invalid.")
            added += int(fields[0])
            deleted += int(fields[1])
        return names, added, deleted

    def _match_source_newlines(self, source: Path, patch: str) -> str:
        policy = WorkspacePathPolicy(source, allowed_paths=self.allowed_source_paths)
        sections = re.split(r"(?=^diff --git )", patch.replace("\r\n", "\n"), flags=re.MULTILINE)
        normalized = []
        for section in (item for item in sections if item):
            match = re.match(r"^diff --git [ab]/(\S+) [ab]/(\S+)$", section, re.MULTILINE)
            if match is None or match.group(1) != match.group(2):
                raise ApprovalActionError("approval_package_invalid", "Patch section has an invalid header.")
            path = policy.resolve(match.group(1), expect="file", must_exist=True)
            normalized.append(section.replace("\n", "\r\n" if b"\r\n" in path.read_bytes() else "\n"))
        return "".join(normalized)

    def _check_candidate(
        self,
        candidate: CandidateWorkspace,
        source: Path,
        request: ApprovalRequest,
    ) -> None:
        if (
            candidate.source.resolve() != source.resolve()
            or candidate.source_head != request.source_head
            or not candidate.path.is_dir()
        ):
            raise ApprovalActionError("approval_invalid", "Candidate does not belong to this approval request.")

    def _apply(self, source: Path, patch: str, *, reverse: bool = False, check: bool = False) -> None:
        arguments = ["-c", "core.whitespace=cr-at-eol", "apply"]
        if reverse:
            arguments.append("-R")
        if check:
            arguments.append("--check")
        arguments.extend(["--whitespace=error-all", "-"])
        result = self._git(
            source,
            arguments,
            input_text=self._match_source_newlines(source, patch),
            allowed_returncodes=(0, 1, 128),
        )
        if result.returncode:
            raise ApprovalActionError(
                "patch_check_failed" if check else "patch_apply_failed",
                f"Patch operation failed: {result.stderr.strip()}",
            )

    def _live_patch(self, source: Path) -> str:
        return self._source_state(source)[3]

    def _restore_after_failure(self, source: Path, patch: str, expected_head: str) -> None:
        try:
            self._apply(source, patch, reverse=True)
            self._clean_source(source, expected_head)
        except ApprovalActionError as error:
            raise ApprovalActionError(
                "apply_verification_failed", f"Applied patch could not be safely restored: {error}"
            ) from error

    def _cleanup_candidate(self, candidate: CandidateWorkspace) -> str | None:
        try:
            candidate.cleanup()
        except CandidateWorkspaceError:
            return "Candidate cleanup failed; retry through its CandidateWorkspace lifecycle."
        return None

    def decide(
        self,
        *,
        package: Path,
        source: Path,
        candidate: CandidateWorkspace,
        decision: ApprovalDecision | None,
    ) -> ApprovalActionResult:
        if decision is None:
            raise ApprovalActionError("approval_invalid", "An explicit approval decision is required.")
        request, patch, manifest_sha256 = self._package(package)
        if decision.request_id != request.request_id:
            raise ApprovalActionError("approval_invalid", "Decision request ID does not match the approval package.")
        if decision.expected_patch_sha256 is not None and decision.expected_patch_sha256 != request.patch_sha256:
            raise ApprovalActionError("approval_invalid", "Decision patch hash does not match the approval package.")
        self._ensure_new_action(request.request_id)
        source = source.resolve()
        self._check_candidate(candidate, source, request)
        self._validate_roots(source, candidate, package)
        self._validate_patch(source, patch, request)
        self._clean_source(source, request.source_head)
        if decision.decision == "reject":
            record = DecisionRecord(
                request_id=request.request_id,
                decision="reject",
                decided_at=datetime.now(timezone.utc),
                actor=decision.actor,
                note=decision.note,
                patch_sha256=request.patch_sha256,
                package_manifest_sha256=manifest_sha256,
                source_head=request.source_head,
            )
            try:
                self._publish_action(request.request_id, {"decision.json": self._json(record)})
            except OSError as error:
                raise ApprovalActionError("audit_write_failed", "Unable to publish rejection audit.") from error
            return ApprovalActionResult(
                request_id=request.request_id,
                status="rejected",
                cleanup_warning=self._cleanup_candidate(candidate),
            )
        return self._approve(
            source=source,
            candidate=candidate,
            decision=decision,
            request=request,
            patch=patch,
            manifest_sha256=manifest_sha256,
        )

    def _approve(
        self,
        *,
        source: Path,
        candidate: CandidateWorkspace,
        decision: ApprovalDecision,
        request: ApprovalRequest,
        patch: str,
        manifest_sha256: str,
    ) -> ApprovalActionResult:
        head_before = request.source_head
        self._apply(source, patch, check=True)
        try:
            self._apply(source, patch)
        except ApprovalActionError:
            if self._live_patch(source):
                self._restore_after_failure(source, patch, head_before)
            raise
        try:
            live_patch = self._live_patch(source)
            head_after, staged, untracked, _ = self._source_state(source)
            changed_files, added_lines, deleted_lines = self._diff_metadata(source)
            if (
                self._sha256(live_patch.encode("utf-8")) != request.patch_sha256
                or head_after != head_before
                or staged.strip()
                or untracked.strip()
                or changed_files != request.changed_files
                or added_lines != request.added_lines
                or deleted_lines != request.deleted_lines
            ):
                raise ApprovalActionError("apply_verification_failed", "Applied source diff does not match approval.")
            reverse_patch = self._git(
                source,
                ["diff", "-R", "--no-ext-diff", "--no-color", "HEAD", "--"],
            ).stdout.replace("\r\n", "\n")
            if not reverse_patch:
                raise ApprovalActionError("apply_verification_failed", "Reverse patch is empty.")
            self._apply(source, reverse_patch, check=True)
        except ApprovalActionError as error:
            self._restore_after_failure(source, patch, head_before)
            if error.code == "apply_verification_failed":
                raise
            raise ApprovalActionError("apply_verification_failed", "Applied source verification failed.") from error
        application = ApplicationRecord(
            request_id=request.request_id,
            applied_at=datetime.now(timezone.utc),
            patch_sha256=request.patch_sha256,
            reverse_patch_sha256=self._sha256(reverse_patch.encode("utf-8")),
            package_manifest_sha256=manifest_sha256,
            source_head_before=head_before,
            source_head_after=head_after,
            changed_files=changed_files,
            added_lines=added_lines,
            deleted_lines=deleted_lines,
        )
        application_bytes = self._json(application)
        decision_record = DecisionRecord(
            request_id=request.request_id,
            decision="approve",
            decided_at=datetime.now(timezone.utc),
            actor=decision.actor,
            note=decision.note,
            patch_sha256=request.patch_sha256,
            package_manifest_sha256=manifest_sha256,
            source_head=request.source_head,
            application_sha256=self._sha256(application_bytes),
        )
        try:
            self._publish_action(
                request.request_id,
                {
                    "decision.json": self._json(decision_record),
                    "application.json": application_bytes,
                    "reverse.patch": reverse_patch.encode("utf-8"),
                },
            )
        except BaseException as error:
            self._restore_after_failure(source, patch, head_before)
            if isinstance(error, ApprovalActionError):
                raise
            raise ApprovalActionError("audit_write_failed", "Unable to publish application audit.") from error
        return ApprovalActionResult(
            request_id=request.request_id,
            status="approved",
            cleanup_warning=self._cleanup_candidate(candidate),
        )

    def _read_audit(self, action: Path) -> tuple[DecisionRecord, ApplicationRecord, bytes, bytes]:
        if {path.name for path in action.iterdir()} != {"decision.json", "application.json", "reverse.patch"}:
            raise ApprovalActionError("rollback_not_safe", "Application audit contains unexpected files.")
        try:
            decision_bytes = (action / "decision.json").read_bytes()
            application_bytes = (action / "application.json").read_bytes()
            reverse_bytes = (action / "reverse.patch").read_bytes()
            decision = DecisionRecord.model_validate_json(decision_bytes)
            application = ApplicationRecord.model_validate_json(application_bytes)
        except (OSError, ValidationError) as error:
            raise ApprovalActionError("rollback_not_safe", "Application audit is incomplete or invalid.") from error
        if (
            decision.decision != "approve"
            or decision.application_sha256 != self._sha256(application_bytes)
            or application.reverse_patch_sha256 != self._sha256(reverse_bytes)
            or decision.request_id != application.request_id
        ):
            raise ApprovalActionError("rollback_not_safe", "Application audit hashes do not match.")
        return decision, application, application_bytes, reverse_bytes

    def rollback(
        self,
        *,
        package: Path,
        source: Path,
        request_id: str,
        actor: str,
        note: str = "",
    ) -> ApprovalActionResult:
        try:
            rollback_identity = ApprovalDecision(
                decision="reject",
                request_id=request_id,
                actor=actor,
                note=note,
            )
        except ValidationError as error:
            raise ApprovalActionError("approval_invalid", "Rollback actor or note is invalid.") from error
        request, patch, manifest_sha256 = self._package(package)
        action = self._action_path(request_id)
        if request.request_id != request_id or not action.is_dir() or action.is_symlink():
            raise ApprovalActionError("rollback_not_safe", "Approval action does not exist.")
        if (action / "rollback.json").exists() or (action / "rollback.json").is_symlink():
            raise ApprovalActionError("rollback_not_safe", "Approval request was already rolled back.")
        decision, application, application_bytes, reverse_bytes = self._read_audit(action)
        if (
            decision.package_manifest_sha256 != manifest_sha256
            or application.package_manifest_sha256 != manifest_sha256
            or application.patch_sha256 != request.patch_sha256
            or decision.patch_sha256 != request.patch_sha256
            or self._sha256(patch.encode("utf-8")) != request.patch_sha256
        ):
            raise ApprovalActionError("rollback_not_safe", "Package and application audit do not match.")
        source = source.resolve()
        head, staged, untracked, live_patch = self._source_state(source)
        if (
            head != application.source_head_after
            or staged.strip()
            or untracked.strip()
            or self._sha256(live_patch.encode("utf-8")) != application.patch_sha256
        ):
            raise ApprovalActionError("rollback_not_safe", "Source contains changes outside the approved patch.")
        reverse_patch = reverse_bytes.decode("utf-8")
        try:
            self._apply(source, reverse_patch, check=True)
            self._apply(source, reverse_patch)
            self._clean_source(source, application.source_head_after)
        except UnicodeDecodeError as error:
            raise ApprovalActionError("rollback_failed", "Unable to reverse the approved patch.") from error
        except ApprovalActionError as error:
            try:
                if self._sha256(self._live_patch(source).encode("utf-8")) != application.patch_sha256:
                    self._apply(source, patch)
                if self._sha256(self._live_patch(source).encode("utf-8")) != application.patch_sha256:
                    raise ApprovalActionError("rollback_failed", "Approved source state could not be restored.")
            except ApprovalActionError as restore_error:
                raise ApprovalActionError(
                    "rollback_failed", "Rollback failed and source restore failed."
                ) from restore_error
            raise ApprovalActionError("rollback_failed", "Unable to reverse the approved patch.") from error
        record = RollbackRecord(
            request_id=request_id,
            rolled_back_at=datetime.now(timezone.utc),
            actor=rollback_identity.actor,
            note=rollback_identity.note,
            application_sha256=self._sha256(application_bytes),
            reverse_patch_sha256=application.reverse_patch_sha256,
            source_head_before=head,
            source_head_after=self._source_state(source)[0],
        )
        rollback_path = action / "rollback.json"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=action,
                prefix=".rollback.tmp-",
                delete=False,
            ) as output:
                output.write(self._json(record))
                output.flush()
                os.fsync(output.fileno())
                temporary = Path(output.name)
            if rollback_path.exists() or rollback_path.is_symlink():
                raise ApprovalActionError("rollback_not_safe", "Approval request was already rolled back.")
            os.replace(temporary, rollback_path)
        except BaseException as error:
            if temporary is not None and temporary.exists():
                temporary.unlink()
            try:
                self._apply(source, patch)
                restored_head, restored_staged, restored_untracked, restored_patch = self._source_state(source)
                if (
                    restored_head != application.source_head_after
                    or restored_staged.strip()
                    or restored_untracked.strip()
                    or self._sha256(restored_patch.encode("utf-8")) != application.patch_sha256
                ):
                    raise ApprovalActionError("rollback_failed", "Source restore did not reproduce approved patch.")
            except ApprovalActionError as restore_error:
                raise ApprovalActionError(
                    "rollback_failed", "Rollback audit failed and source restore failed."
                ) from restore_error
            if isinstance(error, ApprovalActionError):
                raise
            raise ApprovalActionError("audit_write_failed", "Unable to publish rollback audit.") from error
        return ApprovalActionResult(request_id=request_id, status="rolled_back")

    def retry_candidate_cleanup(
        self,
        *,
        request_id: str,
        package: Path,
        source: Path,
        candidate: CandidateWorkspace,
    ) -> None:
        action = self._action_path(request_id)
        if not (action / "decision.json").is_file():
            raise ApprovalActionError("approval_invalid", "No final decision exists for candidate cleanup.")
        try:
            decision = DecisionRecord.model_validate_json((action / "decision.json").read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise ApprovalActionError("approval_invalid", "Decision audit is invalid.") from error
        request, _, manifest_sha256 = self._package(package)
        if (
            decision.request_id != request_id
            or request.request_id != request_id
            or decision.package_manifest_sha256 != manifest_sha256
            or decision.source_head != request.source_head
        ):
            raise ApprovalActionError("approval_invalid", "Decision and approval package do not match.")
        self._check_candidate(candidate, source.resolve(), request)
        if self._cleanup_candidate(candidate) is not None:
            raise ApprovalActionError("candidate_cleanup_failed", "Candidate cleanup retry failed.")

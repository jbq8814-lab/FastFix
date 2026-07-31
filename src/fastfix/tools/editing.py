import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from fastfix.security.paths import PathPolicyError, WorkspacePathPolicy
from fastfix.tools.models import ToolResult

FORBIDDEN_PATCH_MARKERS = (
    "new file mode",
    "deleted file mode",
    "rename from",
    "rename to",
    "copy from",
    "copy to",
    "GIT binary patch",
    "Binary files",
    "/dev/null",
)
DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


class EditingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplyPatchArgs(EditingArgs):
    patch: str = Field(min_length=1, max_length=50_000)


class ReplaceTextArgs(EditingArgs):
    path: str
    old_text: str = Field(min_length=1, max_length=20_000)
    new_text: str = Field(max_length=20_000)
    expected_occurrences: int = Field(default=1, ge=1, le=10)


class ShowGitDiffArgs(EditingArgs):
    context_lines: int = Field(default=3, ge=0, le=10)
    max_chars: int = Field(default=20_000, ge=100, le=50_000)


class RollbackChangesArgs(EditingArgs):
    reason: str = Field(default="", max_length=500)


class WorkspaceGitTools:
    def __init__(
        self,
        workspace: Path,
        *,
        allowed_paths: tuple[str, ...] = ("app",),
        timeout_seconds: int = 30,
    ):
        if not workspace.is_dir():
            raise ValueError("Workspace must be an existing directory.")
        self.workspace = workspace.resolve()
        self.policy = WorkspacePathPolicy(workspace, allowed_paths=allowed_paths)
        self.allowed_paths = allowed_paths
        self.timeout_seconds = timeout_seconds
        self.git = shutil.which("git")
        if self.git is None:
            raise ValueError("Git executable was not found.")
        if self._run(["rev-parse", "--is-inside-work-tree"]).returncode:
            raise ValueError("Workspace must be a Git repository.")
        if self._run(["rev-parse", "--verify", "HEAD"]).returncode:
            raise ValueError("Workspace must have a valid HEAD.")
        if self._run(["status", "--porcelain"]).stdout.strip():
            raise ValueError("Workspace must be clean.")

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    def _run(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [self.git, *arguments],
            cwd=self.workspace,
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            timeout=self.timeout_seconds,
            shell=False,
            env=self._environment() | (environment or {}),
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )

    def _safe_output(self, result: subprocess.CompletedProcess[str]) -> str:
        return (result.stdout + result.stderr).replace(str(self.workspace), ".")

    @staticmethod
    def _newline_style(content: bytes) -> str:
        crlf = content.count(b"\r\n")
        return "\r\n" if crlf > content.count(b"\n") - crlf else "\n"

    @staticmethod
    def _normalize_newlines(text: str, newline: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.fastfix-",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.chmod(temporary_path, path.stat().st_mode)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def replace_text(self, arguments: BaseModel) -> ToolResult:
        args = ReplaceTextArgs.model_validate(arguments)
        path = self.policy.resolve(args.path, expect="file", must_exist=True)
        if args.old_text == args.new_text:
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="no_effect",
                output="old_text and new_text must differ.",
            )
        original = path.read_bytes()
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="decode_error",
                output="Target file is not valid UTF-8 text.",
            )
        newline = self._newline_style(original)
        old_text = self._normalize_newlines(args.old_text, newline)
        new_text = self._normalize_newlines(args.new_text, newline)
        occurrences = text.count(old_text)
        if occurrences == 0:
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="text_not_found",
                output="old_text was not found.",
            )
        if occurrences != args.expected_occurrences:
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="occurrence_mismatch",
                output=f"Expected {args.expected_occurrences} occurrences but found {occurrences}.",
                metadata={"actual_occurrences": occurrences},
            )

        updated = text.replace(old_text, new_text).encode("utf-8")
        if updated == original:
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="no_effect",
                output="Replacement produced no file-content change.",
            )
        if len(updated) > len(original) + 50 * 1024:
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="edit_validation_failed",
                output="Replacement exceeds the allowed file growth.",
            )
        relative_path = self.policy.to_relative(path)
        try:
            before_names = self._run(["diff", "--name-only", "--", *self.allowed_paths])
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="command_timeout",
                output="Edit validation timed out; no changes were written.",
            )
        if before_names.returncode:
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="edit_validation_failed",
                output=self._safe_output(before_names),
            )
        was_changed = relative_path in before_names.stdout.splitlines()
        self._atomic_write(path, updated)
        try:
            check = self._run(
                ["diff", "--no-ext-diff", "--check", "--", relative_path],
                environment=(
                    {
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "core.whitespace",
                        "GIT_CONFIG_VALUE_0": "cr-at-eol",
                    }
                    if newline == "\r\n"
                    else None
                ),
            )
            names = self._run(["diff", "--name-only", "--", *self.allowed_paths])
        except subprocess.TimeoutExpired:
            self._atomic_write(path, original)
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="command_timeout",
                output="Edit validation timed out; the original file was restored.",
            )
        changed_files = sorted(names.stdout.splitlines())
        if check.returncode or names.returncode or (relative_path not in changed_files and not was_changed):
            self._atomic_write(path, original)
            return ToolResult(
                tool_name="replace_text",
                ok=False,
                error_code="edit_validation_failed" if check.returncode or names.returncode else "no_effect",
                output=(
                    self._safe_output(check or names)
                    or "Replacement produced no allowed Git diff; the original file was restored."
                ),
            )
        return ToolResult(
            tool_name="replace_text",
            ok=True,
            metadata={
                "path": relative_path,
                "replacement_count": occurrences,
                "changed_files": changed_files,
                "bytes_before": len(original),
                "bytes_after": len(updated),
            },
        )

    def _validate_patch(self, patch: str) -> tuple[list[str], int, int]:
        if any(marker in patch for marker in FORBIDDEN_PATCH_MARKERS):
            raise PathPolicyError("patch_invalid", "Patch contains a forbidden operation.")
        if 'diff --git "' in patch or "\ndiff --git '" in patch:
            raise PathPolicyError("patch_invalid", "Quoted patch paths are not supported.")
        headers = DIFF_HEADER.findall(patch)
        if not headers:
            raise PathPolicyError("patch_invalid", "Patch must contain a diff --git header.")
        if len(headers) > 5:
            raise PathPolicyError("patch_too_large", "Patch may modify at most 5 files.")

        paths = []
        for before, after in headers:
            if before != after:
                raise PathPolicyError("patch_invalid", "Patch paths must match.")
            if before.startswith("/") or ".." in Path(before).parts:
                raise PathPolicyError("patch_out_of_scope", "Patch path is not allowed.")
            try:
                self.policy.resolve(before, expect="file", must_exist=True)
            except PathPolicyError as error:
                raise PathPolicyError("patch_out_of_scope", str(error)) from error
            paths.append(Path(before).as_posix())

        sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
        for section, path in zip((item for item in sections if item), paths, strict=True):
            old_headers = re.findall(r"^--- (.+)$", section, re.MULTILINE)
            new_headers = re.findall(r"^\+\+\+ (.+)$", section, re.MULTILINE)
            if old_headers != [f"a/{path}"] or new_headers != [f"b/{path}"]:
                raise PathPolicyError("patch_invalid", "Patch file headers do not match the diff path.")

        added = sum(line.startswith("+") and not line.startswith("+++") for line in patch.splitlines())
        deleted = sum(line.startswith("-") and not line.startswith("---") for line in patch.splitlines())
        if added + deleted > 300:
            raise PathPolicyError("patch_too_large", "Patch may add or delete at most 300 lines.")
        return list(dict.fromkeys(paths)), added, deleted

    def _match_workspace_newlines(self, patch: str) -> str:
        sections = re.split(r"(?=^diff --git )", patch.replace("\r\n", "\n"), flags=re.MULTILINE)
        normalized = []
        for section in (item for item in sections if item):
            match = DIFF_HEADER.match(section)
            if match is None:
                raise PathPolicyError("patch_invalid", "Patch section is missing a valid header.")
            path = self.policy.resolve(match.group(1), expect="file", must_exist=True)
            newline = "\r\n" if b"\r\n" in path.read_bytes() else "\n"
            normalized.append(section.replace("\n", newline))
        return "".join(normalized)

    def apply_patch(self, arguments: BaseModel) -> ToolResult:
        args = ApplyPatchArgs.model_validate(arguments)
        paths, added, deleted = self._validate_patch(args.patch)
        patch = self._match_workspace_newlines(args.patch)
        try:
            check = self._run(["apply", "--check", "--whitespace=error-all", "-"], input_text=patch)
            if check.returncode:
                return ToolResult(
                    tool_name="apply_patch",
                    ok=False,
                    error_code="patch_apply_failed",
                    output=self._safe_output(check),
                )
            result = self._run(["apply", "--whitespace=error-all", "-"], input_text=patch)
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_name="apply_patch",
                ok=False,
                error_code="command_timeout",
                output="Patch command timed out.",
            )
        if result.returncode:
            return ToolResult(
                tool_name="apply_patch",
                ok=False,
                error_code="patch_apply_failed",
                output=self._safe_output(result),
            )
        try:
            changed_files, _, _ = self._diff_stats()
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_name="apply_patch",
                ok=False,
                error_code="changed_files_unavailable",
                output="Patch applied, but the current Git diff could not be read.",
                metadata={"diff_may_have_changed": True},
            )
        return ToolResult(
            tool_name="apply_patch",
            ok=True,
            output=self._safe_output(result),
            metadata={"changed_files": changed_files, "added_lines": added, "deleted_lines": deleted},
        )

    def _diff_stats(self) -> tuple[list[str], int, int]:
        names = self._run(["diff", "--name-only", "--", *self.allowed_paths]).stdout.splitlines()
        numstat = self._run(["diff", "--numstat", "--", *self.allowed_paths]).stdout.splitlines()
        added = 0
        deleted = 0
        for line in numstat:
            additions, deletions, _ = line.split("\t", 2)
            if additions.isdigit():
                added += int(additions)
            if deletions.isdigit():
                deleted += int(deletions)
        return sorted(names), added, deleted

    def show_git_diff(self, arguments: BaseModel) -> ToolResult:
        args = ShowGitDiffArgs.model_validate(arguments)
        try:
            result = self._run(["diff", "--no-ext-diff", f"--unified={args.context_lines}", "--", *self.allowed_paths])
            changed_files, added, deleted = self._diff_stats()
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_name="show_git_diff",
                ok=False,
                error_code="command_timeout",
                output="Git diff command timed out.",
            )
        output = self._safe_output(result)
        truncated = len(output) > args.max_chars
        return ToolResult(
            tool_name="show_git_diff",
            ok=result.returncode == 0,
            output=output[: args.max_chars],
            error_code=None if result.returncode == 0 else "tool_execution_error",
            metadata={
                "changed_files": changed_files,
                "diff_chars": len(output),
                "truncated": truncated,
                "added_lines": added,
                "deleted_lines": deleted,
            },
        )

    def rollback_changes(self, arguments: BaseModel) -> ToolResult:
        RollbackChangesArgs.model_validate(arguments)
        try:
            changed_files, _, _ = self._diff_stats()
            result = self._run(["restore", "--source=HEAD", "--worktree", "--staged", "--", *self.allowed_paths])
            remaining, _, _ = self._diff_stats()
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_name="rollback_changes",
                ok=False,
                error_code="command_timeout",
                output="Rollback command timed out.",
            )
        clean = result.returncode == 0 and not remaining
        return ToolResult(
            tool_name="rollback_changes",
            ok=clean,
            output=self._safe_output(result),
            error_code=None if clean else "tool_execution_error",
            metadata={"rolled_back_files": changed_files, "changed_files": remaining, "clean": clean},
        )

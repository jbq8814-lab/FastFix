import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from fastfix.security.paths import WorkspacePathPolicy


class CandidateWorkspaceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _SourceSnapshot:
    path: Path
    head: str
    branch: str | None
    status: str
    git_directory: Path


@dataclass
class CandidateWorkspace:
    source: Path
    path: Path
    source_head: str
    source_branch: str | None
    _manager: "CandidateWorkspaceManager"

    def cleanup(self) -> None:
        self._manager.cleanup(self)

    def __enter__(self) -> "CandidateWorkspace":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()


class CandidateWorkspaceManager:
    def __init__(self, parent: Path, *, timeout_seconds: int = 60):
        if not parent.is_dir():
            raise CandidateWorkspaceError("invalid_parent", "Candidate parent must be an existing directory.")
        self.parent = parent.resolve()
        self.timeout_seconds = timeout_seconds
        self.git = shutil.which("git")
        if self.git is None:
            raise CandidateWorkspaceError("git_not_found", "Git executable was not found.")
        self._managed: dict[Path, Path] = {}

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    def _run(self, arguments: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.git, *arguments],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                shell=False,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as error:
            raise CandidateWorkspaceError("git_timeout", "Git command timed out.") from error

    def _git(
        self,
        repository: Path,
        arguments: list[str],
        *,
        operation: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        result = self._run(["-C", str(repository), *arguments], cwd=repository.parent)
        if result.returncode not in allowed_returncodes:
            raise CandidateWorkspaceError("git_command_failed", f"Git command failed during {operation}.")
        return result

    def _snapshot_source(self, source: Path) -> _SourceSnapshot:
        if not source.is_dir():
            raise CandidateWorkspaceError("invalid_source", "Source must be an existing directory.")
        source = source.resolve()
        work_tree = self._run(["-C", str(source), "rev-parse", "--is-inside-work-tree"], cwd=source.parent)
        if work_tree.returncode or work_tree.stdout.strip() != "true":
            raise CandidateWorkspaceError("not_git_repository", "Source must be a Git worktree.")
        head_result = self._run(["-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"], cwd=source.parent)
        if head_result.returncode:
            raise CandidateWorkspaceError("invalid_head", "Source must have a valid HEAD commit.")
        head = head_result.stdout.strip()
        status = self._git(
            source,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            operation="source status validation",
        ).stdout
        if status.strip():
            raise CandidateWorkspaceError(
                "source_not_clean", "Source worktree must be clean, including untracked files."
            )
        tracked = self._git(source, ["ls-files", "-z"], operation="tracked file inspection").stdout.split("\0")
        if any(WorkspacePathPolicy._is_sensitive(Path(path)) for path in tracked if path):
            raise CandidateWorkspaceError("sensitive_tracked_path", "Source must not track sensitive files.")
        branch_result = self._git(
            source,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            operation="source branch inspection",
            allowed_returncodes=(0, 1),
        )
        git_directory = self._git(
            source,
            ["rev-parse", "--absolute-git-dir"],
            operation="source Git directory inspection",
        ).stdout.strip()
        return _SourceSnapshot(
            path=source,
            head=head,
            branch=branch_result.stdout.strip() or None,
            status=status,
            git_directory=Path(git_directory).resolve(),
        )

    def _target(self, source: Path, target: Path | None) -> Path:
        if target is None:
            target = self.parent / f"ff-c-{uuid4().hex[:12]}"
        elif not target.is_absolute():
            target = self.parent / target
        if target.exists() or target.is_symlink():
            raise CandidateWorkspaceError("target_exists", "Candidate target must not already exist.")
        target = target.resolve()
        if target.exists() or target.is_symlink():
            raise CandidateWorkspaceError("target_exists", "Candidate target must not already exist.")
        if target == source or target.is_relative_to(source):
            raise CandidateWorkspaceError("unsafe_target", "Candidate target must be outside the source repository.")
        return target

    def _assert_source_unchanged(self, snapshot: _SourceSnapshot) -> None:
        current = self._snapshot_source(snapshot.path)
        if (current.head, current.branch, current.status, current.git_directory) != (
            snapshot.head,
            snapshot.branch,
            snapshot.status,
            snapshot.git_directory,
        ):
            raise CandidateWorkspaceError("source_changed", "Source repository changed during candidate creation.")

    def _validate_candidate(
        self,
        candidate: Path,
        snapshot: _SourceSnapshot,
        *,
        require_clean: bool = True,
    ) -> None:
        if not candidate.is_dir() or not (candidate / ".git").is_dir() or (candidate / ".git").is_symlink():
            raise CandidateWorkspaceError(
                "candidate_not_independent", "Candidate must have an independent .git directory."
            )
        work_tree = self._git(candidate, ["rev-parse", "--is-inside-work-tree"], operation="candidate validation")
        if work_tree.stdout.strip() != "true":
            raise CandidateWorkspaceError("candidate_invalid", "Candidate must be a Git worktree.")
        git_directory = Path(
            self._git(
                candidate,
                ["rev-parse", "--absolute-git-dir"],
                operation="candidate Git directory inspection",
            ).stdout.strip()
        ).resolve()
        if git_directory != (candidate / ".git").resolve() or git_directory == snapshot.git_directory:
            raise CandidateWorkspaceError("candidate_not_independent", "Candidate Git metadata is not independent.")
        head = self._git(candidate, ["rev-parse", "--verify", "HEAD^{commit}"], operation="candidate HEAD validation")
        if head.stdout.strip() != snapshot.head:
            raise CandidateWorkspaceError("head_mismatch", "Candidate HEAD does not match the source snapshot.")
        branch = self._git(
            candidate,
            ["symbolic-ref", "--quiet", "HEAD"],
            operation="detached HEAD validation",
            allowed_returncodes=(0, 1),
        )
        if branch.returncode == 0:
            raise CandidateWorkspaceError("candidate_not_detached", "Candidate must use detached HEAD.")
        status = self._git(
            candidate,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            operation="candidate status validation",
        )
        if require_clean and status.stdout.strip():
            raise CandidateWorkspaceError("candidate_not_clean", "Candidate worktree must be clean.")
        if self._git(candidate, ["remote"], operation="candidate remote validation").stdout.strip():
            raise CandidateWorkspaceError("candidate_has_remote", "Candidate must not retain Git remotes.")
        autocrlf = self._git(candidate, ["config", "--local", "--get", "core.autocrlf"], operation="Git configuration")
        if autocrlf.stdout.strip() != "false":
            raise CandidateWorkspaceError("candidate_invalid", "Candidate must set core.autocrlf=false.")
        self._assert_source_unchanged(snapshot)

    def create(self, source: Path, *, target: Path | None = None) -> CandidateWorkspace:
        snapshot = self._snapshot_source(source)
        candidate = self._target(snapshot.path, target)
        self._managed[candidate] = snapshot.path
        try:
            clone = self._run(
                [
                    "-c",
                    "core.longpaths=true",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(snapshot.path),
                    str(candidate),
                ],
                cwd=candidate.parent,
            )
            if clone.returncode:
                raise CandidateWorkspaceError("clone_failed", "Unable to create the candidate repository.")
            self._git(candidate, ["config", "core.autocrlf", "false"], operation="Git configuration")
            self._git(candidate, ["checkout", "--detach", snapshot.head], operation="detached checkout")
            self._git(candidate, ["remote", "remove", "origin"], operation="remote removal")
            self._validate_candidate(candidate, snapshot)
        except BaseException:
            self._cleanup_path(candidate)
            raise
        return CandidateWorkspace(
            source=snapshot.path,
            path=candidate,
            source_head=snapshot.head,
            source_branch=snapshot.branch,
            _manager=self,
        )

    def recover(
        self,
        *,
        source: Path,
        candidate: Path,
        source_head: str,
        source_branch: str | None,
    ) -> CandidateWorkspace:
        snapshot = self._snapshot_source(source)
        candidate = candidate.resolve()
        if snapshot.head != source_head or snapshot.branch != source_branch:
            raise CandidateWorkspaceError("source_changed", "Source repository changed after candidate creation.")
        if not candidate.is_relative_to(self.parent) or candidate == snapshot.path:
            raise CandidateWorkspaceError("not_managed", "Candidate is outside this manager's parent directory.")
        self._validate_candidate(candidate, snapshot, require_clean=False)
        staged = self._git(
            candidate,
            ["diff", "--cached", "--quiet", "HEAD", "--"],
            operation="candidate staged change inspection",
            allowed_returncodes=(0, 1),
        )
        if staged.returncode:
            raise CandidateWorkspaceError("candidate_not_clean", "Candidate must not contain staged changes.")
        if self._git(
            candidate,
            ["ls-files", "--others", "--exclude-standard"],
            operation="candidate untracked file inspection",
        ).stdout.strip():
            raise CandidateWorkspaceError("candidate_not_clean", "Candidate must not contain untracked files.")
        self._managed[candidate] = snapshot.path
        return CandidateWorkspace(
            source=snapshot.path,
            path=candidate,
            source_head=snapshot.head,
            source_branch=snapshot.branch,
            _manager=self,
        )

    @staticmethod
    def _remove_readonly(function, path: str, _: tuple[type[BaseException], BaseException, TracebackType]) -> None:
        candidate = Path(path)
        candidate.chmod(candidate.stat().st_mode | stat.S_IWRITE)
        function(path)

    def _cleanup_path(self, candidate: Path) -> None:
        source = self._managed.get(candidate)
        if source is None:
            return
        if candidate == source or source.is_relative_to(candidate):
            raise CandidateWorkspaceError(
                "unsafe_cleanup", "Refusing to remove a path containing the source repository."
            )
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.exists():
            for attempt in range(3):
                try:
                    shutil.rmtree(candidate, onerror=self._remove_readonly)
                    break
                except OSError as error:
                    if not candidate.exists():
                        break
                    if attempt == 2:
                        raise CandidateWorkspaceError(
                            "cleanup_failed", "Unable to remove candidate workspace."
                        ) from error
                    time.sleep(0.05)
        if candidate.exists() or candidate.is_symlink():
            raise CandidateWorkspaceError("cleanup_failed", "Unable to remove candidate workspace.")
        self._managed.pop(candidate, None)

    def cleanup(self, candidate: CandidateWorkspace) -> None:
        if candidate._manager is not self:
            raise CandidateWorkspaceError("not_managed", "Candidate belongs to another manager.")
        self._cleanup_path(candidate.path)

from pathlib import Path

from fastfix.sandbox.models import ValidationBackend
from fastfix.tools.editing import (
    ApplyPatchArgs,
    ReplaceTextArgs,
    RollbackChangesArgs,
    ShowGitDiffArgs,
    WorkspaceGitTools,
)
from fastfix.tools.fastapi import FastApiTools
from fastfix.tools.registry import ToolRegistry, ToolSpec
from fastfix.tools.repository import (
    ReadFileArgs,
    RepositoryTools,
    SearchCodeArgs,
    ShowTreeArgs,
)
from fastfix.tools.validation import RunPytestArgs, RunRuffArgs, WorkspaceValidationTools


def build_repair_registry(
    workspace: Path,
    *,
    python_executable: Path | None = None,
    validation_backend: ValidationBackend | None = None,
    allowed_source_paths: tuple[str, ...] = ("app",),
    test_paths: tuple[str, ...] = ("tests",),
    include_route_inspection: bool = True,
) -> ToolRegistry:
    repository = RepositoryTools(workspace)
    git = WorkspaceGitTools(workspace, allowed_paths=allowed_source_paths)
    validation = WorkspaceValidationTools(
        workspace,
        python_executable=python_executable,
        backend=validation_backend,
        source_paths=allowed_source_paths,
        test_paths=test_paths,
    )
    registry = ToolRegistry()
    for spec in (
        ToolSpec("show_tree", "Show a bounded, sorted repository directory tree.", ShowTreeArgs, repository.show_tree),
        ToolSpec(
            "read_file", "Read a bounded range of UTF-8 text with line numbers.", ReadFileArgs, repository.read_file
        ),
        ToolSpec(
            "search_code", "Search repository text files for a literal string.", SearchCodeArgs, repository.search_code
        ),
    ):
        registry.register(spec)
    if include_route_inspection:
        FastApiTools(workspace, allowed_paths=allowed_source_paths).register(registry)
    for spec in (
        ToolSpec(
            "replace_text",
            "Replace an exact bounded text occurrence in an allowed source file.",
            ReplaceTextArgs,
            git.replace_text,
        ),
        ToolSpec(
            "apply_patch", "Apply a bounded unified diff to allowed source files.", ApplyPatchArgs, git.apply_patch
        ),
        ToolSpec(
            "run_pytest",
            "Run targeted pytest or the configured complete regression suite.",
            RunPytestArgs,
            validation.run_pytest,
        ),
        ToolSpec("run_ruff", "Run Ruff on the configured complete source paths.", RunRuffArgs, validation.run_ruff),
        ToolSpec(
            "show_git_diff", "Show the bounded Git diff for allowed source files.", ShowGitDiffArgs, git.show_git_diff
        ),
        ToolSpec(
            "rollback_changes",
            "Restore allowed source files to HEAD.",
            RollbackChangesArgs,
            git.rollback_changes,
        ),
    ):
        registry.register(spec)
    return registry

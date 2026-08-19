from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from fastfix.security.paths import PathPolicyError, WorkspacePathPolicy
from fastfix.tools.models import ToolResult
from fastfix.tools.registry import ToolRegistry, ToolSpec

IGNORED_DIRECTORIES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".git"}


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShowTreeArgs(ToolArgs):
    path: str = "."
    depth: int = Field(default=3, ge=0, le=6)
    max_entries: int = Field(default=200, ge=1, le=500)


class ReadFileArgs(ToolArgs):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    max_lines: int = Field(default=200, ge=1, le=400)


class SearchCodeArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=200)
    path: str = "."
    glob: str = "*.py"
    case_sensitive: bool = True
    max_results: int = Field(default=50, ge=1, le=100)


class RepositoryTools:
    def __init__(
        self,
        workspace: Path,
        *,
        allowed_paths: tuple[str, ...] = (".",),
    ):
        self.policy = WorkspacePathPolicy(workspace, allowed_paths=allowed_paths)

    def build_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="show_tree",
                description="Show a bounded, sorted repository directory tree.",
                arguments_model=ShowTreeArgs,
                handler=self.show_tree,
            )
        )
        registry.register(
            ToolSpec(
                name="read_file",
                description="Read a bounded range of UTF-8 text with line numbers.",
                arguments_model=ReadFileArgs,
                handler=self.read_file,
            )
        )
        registry.register(
            ToolSpec(
                name="search_code",
                description="Search repository text files for a literal string.",
                arguments_model=SearchCodeArgs,
                handler=self.search_code,
            )
        )
        return registry

    def _safe_children(self, directory: Path) -> list[Path]:
        children = []
        for child in directory.iterdir():
            if child.name in IGNORED_DIRECTORIES or child.is_symlink():
                continue
            relative = self.policy.to_relative(child)
            try:
                self.policy.resolve(relative)
            except PathPolicyError:
                continue
            children.append(child)
        return sorted(children, key=lambda child: (not child.is_dir(), child.name.casefold(), child.name))

    def show_tree(self, arguments: BaseModel) -> ToolResult:
        args = ShowTreeArgs.model_validate(arguments)
        root = self.policy.resolve(args.path, expect="directory")
        entries: list[str] = []

        def visit(directory: Path, level: int) -> None:
            if level > args.depth or len(entries) > args.max_entries:
                return
            for child in self._safe_children(directory):
                if len(entries) > args.max_entries:
                    return
                suffix = "/" if child.is_dir() else ""
                entries.append(f"{'  ' * (level - 1)}{child.name}{suffix}")
                if child.is_dir():
                    visit(child, level + 1)

        visit(root, 1)
        truncated = len(entries) > args.max_entries
        visible = entries[: args.max_entries]
        return ToolResult(
            tool_name="show_tree",
            ok=True,
            output="\n".join(visible),
            metadata={
                "root": self.policy.to_relative(root),
                "entry_count": len(visible),
                "truncated": truncated,
            },
        )

    def read_file(self, arguments: BaseModel) -> ToolResult:
        args = ReadFileArgs.model_validate(arguments)
        if args.end_line is not None and args.end_line < args.start_line:
            return ToolResult(
                tool_name="read_file",
                ok=False,
                error_code="invalid_arguments",
                output="end_line must be greater than or equal to start_line.",
            )
        path = self.policy.resolve(args.path, expect="file")
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except UnicodeError:
            return ToolResult(tool_name="read_file", ok=False, error_code="decode_error", output="File decode failed.")

        requested_end = min(args.end_line or len(lines), len(lines))
        actual_end = min(requested_end, args.start_line + args.max_lines - 1)
        selected = lines[args.start_line - 1 : actual_end]
        return ToolResult(
            tool_name="read_file",
            ok=True,
            output="\n".join(f"{number} | {line}" for number, line in enumerate(selected, args.start_line)),
            metadata={
                "path": self.policy.to_relative(path),
                "total_lines": len(lines),
                "start_line": args.start_line,
                "end_line": actual_end,
                "truncated": actual_end < requested_end,
            },
        )

    @staticmethod
    def _matches_glob(path: Path, glob: str) -> bool:
        return path.match(glob)

    def _searchable_files(self, root: Path, glob: str) -> list[Path]:
        files: list[Path] = []

        def visit(directory: Path) -> None:
            for child in self._safe_children(directory):
                if child.is_dir():
                    visit(child)
                elif self._matches_glob(child.relative_to(root), glob):
                    files.append(child)

        visit(root)
        return sorted(files, key=lambda path: self.policy.to_relative(path))

    def search_code(self, arguments: BaseModel) -> ToolResult:
        args = SearchCodeArgs.model_validate(arguments)
        root = self.policy.resolve(args.path, expect="directory")
        query = args.query if args.case_sensitive else args.query.casefold()
        matches: list[tuple[str, int, str]] = []
        for path in self._searchable_files(root, args.glob):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "\x00" in text:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                candidate = line if args.case_sensitive else line.casefold()
                if query in candidate:
                    content = line.strip()
                    if len(content) > 300:
                        content = f"{content[:297]}..."
                    matches.append((self.policy.to_relative(path), line_number, content))
                    if len(matches) > args.max_results:
                        break
            if len(matches) > args.max_results:
                break
        truncated = len(matches) > args.max_results
        visible = matches[: args.max_results]
        return ToolResult(
            tool_name="search_code",
            ok=True,
            output="\n".join(f"{path}:{line_number}: {line}" for path, line_number, line in visible),
            metadata={
                "query": args.query,
                "match_count": len(visible),
                "truncated": truncated,
            },
        )


def build_repository_registry(
    workspace: Path,
    *,
    allowed_paths: tuple[str, ...] = (".",),
) -> ToolRegistry:
    return RepositoryTools(workspace, allowed_paths=allowed_paths).build_registry()

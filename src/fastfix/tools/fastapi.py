import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from fastfix.analysis.fastapi_routes import analyze_fastapi_file
from fastfix.security.paths import PathPolicyError, WorkspacePathPolicy
from fastfix.tools.models import ToolResult
from fastfix.tools.registry import ToolRegistry, ToolSpec
from fastfix.tools.repository import RepositoryTools

IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "tests",
}


class InspectFastApiRoutesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "."
    max_files: int = Field(default=50, ge=1, le=200)
    max_routes: int = Field(default=100, ge=1, le=500)


class FastApiTools:
    def __init__(
        self,
        workspace: Path,
        *,
        allowed_paths: tuple[str, ...] = (".",),
        max_file_bytes: int = 512_000,
        max_output_chars: int = 100_000,
    ):
        if max_file_bytes < 1 or max_output_chars < 256:
            raise ValueError("Analysis limits must be positive and bounded.")
        self.policy = WorkspacePathPolicy(workspace, allowed_paths=allowed_paths)
        self.max_file_bytes = max_file_bytes
        self.max_output_chars = max_output_chars

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="inspect_fastapi_routes",
                description="Statically inspect bounded FastAPI route declarations without importing repository code.",
                arguments_model=InspectFastApiRoutesArgs,
                handler=self.inspect_fastapi_routes,
            )
        )

    def build_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        self.register(registry)
        return registry

    def _files(self, roots: list[Path], limit: int) -> tuple[list[Path], bool]:
        files: list[Path] = []
        seen: set[Path] = set()

        def visit(directory: Path) -> bool:
            for child in sorted(directory.iterdir(), key=lambda path: (not path.is_dir(), path.name.casefold())):
                if child.is_symlink() or child.name.casefold() in IGNORED_DIRECTORIES:
                    continue
                try:
                    safe = self.policy.resolve(self.policy.to_relative(child))
                except PathPolicyError:
                    continue
                if safe.is_dir():
                    if visit(safe):
                        return True
                elif safe.suffix.lower() == ".py" and safe not in seen:
                    seen.add(safe)
                    files.append(safe)
                    if len(files) > limit:
                        return True
            return False

        for root in roots:
            if root.is_file():
                if root.suffix.lower() != ".py":
                    raise PathPolicyError("invalid_path", "Analysis path must be a directory or Python file.")
                if root not in seen:
                    seen.add(root)
                    files.append(root)
            elif visit(root):
                return files[:limit], True
        return files[:limit], len(files) > limit

    def _read(self, path: Path) -> str:
        if path.stat().st_size > self.max_file_bytes:
            raise ValueError("File exceeds the analysis size limit.")
        with path.open("rb") as source:
            content = source.read(self.max_file_bytes + 1)
        if len(content) > self.max_file_bytes:
            raise ValueError("File exceeds the analysis size limit.")
        return content.decode("utf-8")

    def _bounded_output(self, payload: dict[str, object], reasons: list[str]) -> str:
        output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        while len(output) > self.max_output_chars and payload["routes"]:
            payload["routes"].pop()
            payload["route_count"] = len(payload["routes"])
            payload["truncated"] = True
            if "output_truncated" not in reasons:
                reasons.append("output_truncated")
            payload["truncation_reasons"] = reasons
            output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(output) > self.max_output_chars:
            return json.dumps(
                {
                    "scanned_files": payload["scanned_files"],
                    "route_count": 0,
                    "routes": [],
                    "parse_errors": [],
                    "truncated": True,
                    "truncation_reasons": ["output_truncated"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return output

    def inspect_fastapi_routes(self, arguments: BaseModel) -> ToolResult:
        args = InspectFastApiRoutesArgs.model_validate(arguments)
        roots = list(self.policy.allowed_roots) if args.path == "." else [self.policy.resolve(args.path)]
        if any(not root.is_dir() and not root.is_file() for root in roots):
            return ToolResult(
                tool_name="inspect_fastapi_routes",
                ok=False,
                error_code="invalid_path",
                output="Analysis path must be a directory or Python file.",
            )
        try:
            files, file_truncated = self._files(roots, args.max_files)
        except OSError:
            return ToolResult(
                tool_name="inspect_fastapi_routes",
                ok=False,
                error_code="analysis_failed",
                output="Repository analysis failed.",
            )
        routes: list[dict[str, object]] = []
        parse_errors: list[dict[str, object]] = []
        reasons = ["file_limit_exceeded"] if file_truncated else []
        scanned_files = 0
        for path in files:
            relative = self.policy.to_relative(path)
            scanned_files += 1
            try:
                source = self._read(path)
                discovered = analyze_fastapi_file(source, relative)
            except SyntaxError as error:
                parse_errors.append(
                    {
                        "file": relative,
                        "line": error.lineno,
                        "message": (error.msg or "Invalid Python syntax.")[:200],
                    }
                )
                continue
            except (OSError, UnicodeError, ValueError) as error:
                parse_errors.append({"file": relative, "line": None, "message": str(error)[:200]})
                if "output_truncated" not in reasons:
                    reasons.append("output_truncated")
                continue
            remaining = args.max_routes - len(routes)
            routes.extend(discovered[:remaining])
            if len(discovered) > remaining:
                reasons.append("output_truncated")
                break
        payload: dict[str, object] = {
            "scanned_files": scanned_files,
            "route_count": len(routes),
            "routes": sorted(routes, key=lambda route: (route["file"], route["line"])),
            "parse_errors": sorted(parse_errors, key=lambda error: (error["file"], error["line"] or 0)),
            "truncated": bool(reasons),
            "truncation_reasons": list(dict.fromkeys(reasons)),
        }
        output = self._bounded_output(payload, reasons)
        result_payload = json.loads(output)
        return ToolResult(
            tool_name="inspect_fastapi_routes",
            ok=True,
            output=output,
            metadata={
                "scanned_files": result_payload["scanned_files"],
                "route_count": result_payload["route_count"],
                "parse_error_count": len(result_payload["parse_errors"]),
                "truncated": result_payload["truncated"],
                "truncation_reasons": result_payload["truncation_reasons"],
            },
        )


def register_fastapi_tools(
    registry: ToolRegistry,
    workspace: Path,
    *,
    allowed_paths: tuple[str, ...] = (".",),
) -> None:
    FastApiTools(workspace, allowed_paths=allowed_paths).register(registry)


def build_fastapi_registry(
    workspace: Path,
    *,
    allowed_paths: tuple[str, ...] = (".",),
) -> ToolRegistry:
    return FastApiTools(workspace, allowed_paths=allowed_paths).build_registry()


def build_readonly_registry(
    workspace: Path,
    *,
    allowed_paths: tuple[str, ...] = (".",),
) -> ToolRegistry:
    registry = RepositoryTools(workspace, allowed_paths=allowed_paths).build_registry()
    register_fastapi_tools(registry, workspace, allowed_paths=allowed_paths)
    return registry

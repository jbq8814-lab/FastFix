from pathlib import Path
from typing import Literal


class PathPolicyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class WorkspacePathPolicy:
    def __init__(
        self,
        workspace: Path,
        *,
        allowed_paths: tuple[str, ...] = (".",),
    ):
        self.workspace = workspace.resolve()
        self.allowed_roots = tuple(self._resolve_allowed(path) for path in allowed_paths)
        if not self.allowed_roots:
            raise ValueError("allowed_paths must not be empty")

    def _resolve_allowed(self, allowed_path: str) -> Path:
        path = Path(allowed_path)
        if not allowed_path or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Invalid allowed path: {allowed_path!r}")
        resolved = (self.workspace / path).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError(f"Allowed path escapes workspace: {allowed_path!r}")
        return resolved

    @staticmethod
    def _is_sensitive(path: Path) -> bool:
        parts = tuple(part.lower() for part in path.parts)
        name = path.name.lower()
        return (
            any(part in {".git", ".ssh", ".aws"} for part in parts)
            or any(part == ".env" or part.startswith(".env.") for part in parts)
            or name in {"id_rsa", "id_ed25519"}
            or path.suffix.lower() in {".pem", ".key"}
        )

    def resolve(
        self,
        user_path: str,
        *,
        expect: Literal["file", "directory", "any"] = "any",
        must_exist: bool = True,
    ) -> Path:
        if not user_path:
            raise PathPolicyError("path_not_allowed", "Path must not be empty.")
        relative = Path(user_path)
        if relative.is_absolute():
            raise PathPolicyError("path_not_allowed", "Absolute paths are not allowed.")
        if ".." in relative.parts:
            raise PathPolicyError("path_not_allowed", f"Path traversal is not allowed: {user_path}")

        resolved = (self.workspace / relative).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise PathPolicyError("path_not_allowed", f"Path is outside the workspace: {user_path}")
        if not any(resolved.is_relative_to(root) for root in self.allowed_roots):
            raise PathPolicyError("path_not_allowed", f"Path is outside the allowed paths: {user_path}")

        lexical_relative = relative
        resolved_relative = resolved.relative_to(self.workspace)
        if self._is_sensitive(lexical_relative) or self._is_sensitive(resolved_relative):
            raise PathPolicyError("sensitive_path", f"Sensitive path is not allowed: {user_path}")
        if must_exist and not resolved.exists():
            raise PathPolicyError("path_not_found", f"Path does not exist: {user_path}")
        if resolved.exists() and expect == "file" and not resolved.is_file():
            raise PathPolicyError("not_a_file", f"Path is not a file: {user_path}")
        if resolved.exists() and expect == "directory" and not resolved.is_dir():
            raise PathPolicyError("not_a_directory", f"Path is not a directory: {user_path}")
        return resolved

    def to_relative(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise PathPolicyError("path_not_allowed", "Path is outside the workspace.")
        relative = resolved.relative_to(self.workspace)
        return "." if relative == Path() else relative.as_posix()

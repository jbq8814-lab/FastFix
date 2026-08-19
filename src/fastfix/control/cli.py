"""fastfix.control 命令行入口：固定子命令 + JSON stdout，供 AgentGuard 受控调用。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from pydantic import ValidationError

from fastfix.control.models import ControlResult, DiagnosisContext, validate_session_reference
from fastfix.control.service import ControlInterfaceError, ControlInterfaceService

app = typer.Typer(add_completion=False, help="Narrow AgentGuard recovery control interface.")
DEFAULT_MODEL = "gpt-4o-mini"


def _service(repo_root: Path) -> ControlInterfaceService:
    return ControlInterfaceService(repo_root)


def _emit(result: ControlResult) -> None:
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
    raise typer.Exit(0)


def _reject(error: ControlInterfaceError) -> None:
    print(
        json.dumps(
            {
                "command": "rejected",
                "status": "rejected",
                "message": str(error)[:500],
                "error_code": error.code,
            },
            ensure_ascii=False,
        )
    )
    raise typer.Exit(2)


@app.command()
def status(
    session: str = typer.Option(..., help="Session reference <experiment>/sessions/<uuid>."),
    repo_root: Path = typer.Option(Path.cwd(), help="FastFix repository root."),
) -> None:
    try:
        _emit(_service(repo_root).status(session))
    except ControlInterfaceError as error:
        _reject(error)


@app.command()
def rerun_validation(
    session: str = typer.Option(...),
    idempotency_key: str = typer.Option(None),
    repo_root: Path = typer.Option(Path.cwd()),
) -> None:
    try:
        _emit(_service(repo_root).rerun_validation(session, key=idempotency_key))
    except ControlInterfaceError as error:
        _reject(error)


@app.command()
def rollback(
    session: str = typer.Option(...),
    idempotency_key: str = typer.Option(...),
    repo_root: Path = typer.Option(Path.cwd()),
) -> None:
    try:
        _emit(_service(repo_root).rollback(session, key=idempotency_key))
    except ControlInterfaceError as error:
        _reject(error)


@app.command()
def reopen_repair(
    session: str = typer.Option(...),
    idempotency_key: str = typer.Option(...),
    diagnosis_context: str = typer.Option(
        None,
        help="Diagnosis context as inline JSON, or @path to a JSON file, or '-' to read stdin.",
    ),
    model: str = typer.Option(DEFAULT_MODEL),
    repo_root: Path = typer.Option(Path.cwd()),
) -> None:
    try:
        if diagnosis_context is None:
            raise ControlInterfaceError("missing_context", "reopen-repair requires --diagnosis-context.")
        raw = _read_context(diagnosis_context)
        context = DiagnosisContext.model_validate(raw)
        validate_session_reference(session)
        _emit(_service(repo_root).reopen_repair(session, context, key=idempotency_key, model_name=model))
    except ControlInterfaceError as error:
        _reject(error)
    except ValidationError:
        _reject(ControlInterfaceError("invalid_context", "Diagnosis context failed schema validation."))


def _read_context(spec: str) -> dict:
    try:
        if spec == "-":
            return json.loads(sys.stdin.read())
        if spec.startswith("@"):
            path = Path(spec[1:])
            if not path.is_file() or path.stat().st_size > 64 * 1024:
                raise ControlInterfaceError("invalid_context_file", "Context file must exist and stay small.")
            return json.loads(path.read_text(encoding="utf-8"))
        return json.loads(spec)
    except (json.JSONDecodeError, OSError) as error:
        raise ControlInterfaceError("invalid_context", f"Diagnosis context is not valid JSON: {error}") from error


if __name__ == "__main__":
    app()

import ast
import re
from typing import Any

HTTP_DECORATORS = {"get", "post", "put", "patch", "delete", "options", "head", "api_route"}
PATH_PARAMETER = re.compile(r"{([^{}]+)}")


def _expression(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    value = ast.unparse(node)
    return value if len(value) <= 300 else f"{value[:297]}..."


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _default_type(node: ast.AST | None) -> str:
    if node is None:
        return "required"
    if isinstance(node, ast.Constant):
        return "none" if node.value is None else "constant"
    if isinstance(node, ast.Call):
        return "call"
    if isinstance(node, ast.Name):
        return "name"
    if isinstance(node, ast.Attribute):
        return "attribute"
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return "collection"
    return "expression"


def _parameters(arguments: ast.arguments) -> list[dict[str, str | None]]:
    positional = [*arguments.posonlyargs, *arguments.args]
    positional_defaults = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    parameters = [
        {
            "name": argument.arg,
            "annotation": _expression(argument.annotation),
            "default_type": _default_type(default),
        }
        for argument, default in zip(positional, positional_defaults, strict=True)
    ]
    if arguments.vararg is not None:
        parameters.append(
            {
                "name": arguments.vararg.arg,
                "annotation": _expression(arguments.vararg.annotation),
                "default_type": "required",
            }
        )
    parameters.extend(
        {
            "name": argument.arg,
            "annotation": _expression(argument.annotation),
            "default_type": _default_type(default),
        }
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
    )
    if arguments.kwarg is not None:
        parameters.append(
            {
                "name": arguments.kwarg.arg,
                "annotation": _expression(arguments.kwarg.annotation),
                "default_type": "required",
            }
        )
    return parameters


class _BodyCalls(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: set[str] = set()
        self.awaited: set[str] = set()
        self.awaited_nodes: set[int] = set()

    def visit_Await(self, node: ast.Await) -> None:
        if isinstance(node.value, ast.Call):
            self.awaited_nodes.add(id(node.value))
            if name := _name(node.value.func):
                self.awaited.add(name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if name := _name(node.func):
            self.calls.add(name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


def _body_calls(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], list[str], list[str]]:
    visitor = _BodyCalls()
    for statement in function.body:
        visitor.visit(statement)
    unawaited: set[str] = set()

    class UnawaitedCalls(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if id(node) not in visitor.awaited_nodes and (name := _name(node.func)):
                unawaited.add(name)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

    unawaited_visitor = UnawaitedCalls()
    for statement in function.body:
        unawaited_visitor.visit(statement)
    return sorted(visitor.calls), sorted(visitor.awaited), sorted(unawaited)


def _static_strings(node: ast.AST | None, *, upper: bool = False) -> list[str] | None:
    if node is None:
        return None
    values = node.elts if isinstance(node, (ast.List, ast.Tuple, ast.Set)) else [node]
    result = []
    for value in values:
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return None
        result.append(value.value.upper() if upper else value.value)
    return list(dict.fromkeys(result))


def _dependencies(
    decorator_keywords: dict[str, ast.AST],
    arguments: ast.arguments,
) -> list[str]:
    dependencies = []
    configured = decorator_keywords.get("dependencies")
    if isinstance(configured, (ast.List, ast.Tuple, ast.Set)):
        dependencies.extend(filter(None, (_expression(value) for value in configured.elts)))
    elif configured is not None:
        dependencies.append(_expression(configured) or "")
    for default in [*arguments.defaults, *(item for item in arguments.kw_defaults if item is not None)]:
        if isinstance(default, ast.Call) and _name(default.func) == "Depends":
            dependencies.append(_expression(default) or "")
    return list(dict.fromkeys(dependency for dependency in dependencies if dependency))


def _constructors(tree: ast.Module) -> tuple[set[str], set[str]]:
    constructors: set[str] = set()
    modules: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module in {"fastapi", "fastapi.routing"}:
            constructors.update(
                alias.asname or alias.name for alias in statement.names if alias.name in {"FastAPI", "APIRouter"}
            )
        elif isinstance(statement, ast.Import):
            modules.update(alias.asname or alias.name for alias in statement.names if alias.name == "fastapi")
    return constructors, modules


def _router_objects(tree: ast.Module, constructors: set[str], modules: set[str]) -> set[str]:
    routers: set[str] = set()
    for statement in tree.body:
        target: ast.AST | None = None
        value: ast.AST | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        constructor = _name(value.func)
        if constructor in constructors or any(
            constructor == f"{module}.{name}" for module in modules for name in ("FastAPI", "APIRouter")
        ):
            routers.add(target.id)
    return routers


def _route(
    file: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    decorator: ast.Call,
    router_object: str,
    method: str,
) -> dict[str, Any]:
    keywords = {keyword.arg: keyword.value for keyword in decorator.keywords if keyword.arg is not None}
    path_node = decorator.args[0] if decorator.args else keywords.get("path")
    path = path_node.value if isinstance(path_node, ast.Constant) and isinstance(path_node.value, str) else None
    methods = [method.upper()] if method != "api_route" else _static_strings(keywords.get("methods"), upper=True)
    tags = _static_strings(keywords.get("tags"))
    called, awaited, unawaited = _body_calls(function)
    return {
        "file": file,
        "line": decorator.lineno,
        "router_object": router_object,
        "http_methods": methods,
        "path": path if path is not None else _expression(path_node),
        "handler_name": function.name,
        "handler_async": isinstance(function, ast.AsyncFunctionDef),
        "parameters": _parameters(function.args),
        "path_parameters": PATH_PARAMETER.findall(path) if path is not None else [],
        "response_model": _expression(keywords.get("response_model")),
        "status_code": (
            keywords["status_code"].value
            if isinstance(keywords.get("status_code"), ast.Constant)
            else _expression(keywords.get("status_code"))
        ),
        "dependencies": _dependencies(keywords, function.args),
        "tags": tags if tags is not None else ([_expression(keywords["tags"])] if "tags" in keywords else None),
        "called_functions": called,
        "awaited_calls": awaited,
        "unawaited_calls": unawaited,
    }


def analyze_fastapi_file(source: str, file: str) -> list[dict[str, Any]]:
    tree = ast.parse(source, filename=file)
    constructors, modules = _constructors(tree)
    routers = _router_objects(tree, constructors, modules)
    routes = []
    for statement in tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in statement.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            router_object = _name(decorator.func.value)
            method = decorator.func.attr
            if router_object in routers and method in HTTP_DECORATORS:
                routes.append(_route(file, statement, decorator, router_object, method))
    return sorted(routes, key=lambda route: (route["file"], route["line"], route["handler_name"]))

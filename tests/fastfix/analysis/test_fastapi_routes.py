from fastfix.analysis.fastapi_routes import analyze_fastapi_file


def test_fastapi_and_router_routes_include_static_facts() -> None:
    routes = analyze_fastapi_file(
        """
from fastapi import APIRouter, Depends, FastAPI
app = FastAPI()
router = APIRouter()

@app.get("/users/{user_id}", response_model=UserResponse, tags=["users"])
async def get_user(user_id: int, service: Service = Depends(get_service)):
    audit()
    return await service.fetch_user(user_id)

@router.post("/users", status_code=201, dependencies=[Depends(require_admin)])
def create_user(payload: UserCreate = Body(...)):
    return create(payload)
""",
        "app/main.py",
    )
    assert [(route["http_methods"], route["handler_name"], route["handler_async"]) for route in routes] == [
        (["GET"], "get_user", True),
        (["POST"], "create_user", False),
    ]
    get_route, post_route = routes
    assert get_route["path_parameters"] == ["user_id"]
    assert get_route["response_model"] == "UserResponse"
    assert get_route["tags"] == ["users"]
    assert get_route["parameters"] == [
        {"name": "user_id", "annotation": "int", "default_type": "required"},
        {"name": "service", "annotation": "Service", "default_type": "call"},
    ]
    assert get_route["dependencies"] == ["Depends(get_service)"]
    assert get_route["called_functions"] == ["audit", "service.fetch_user"]
    assert get_route["awaited_calls"] == ["service.fetch_user"]
    assert get_route["unawaited_calls"] == ["audit"]
    assert post_route["status_code"] == 201
    assert post_route["dependencies"] == ["Depends(require_admin)"]
    assert post_route["unawaited_calls"] == ["create"]


def test_all_http_decorators_and_api_route_methods() -> None:
    routes = analyze_fastapi_file(
        """
import fastapi as fa
api = fa.FastAPI()

@api.put("/put")
@api.patch("/patch")
@api.delete("/delete")
@api.options("/options")
@api.head("/head")
def many(): pass

@api.api_route("/multi", methods=["GET", "POST", "GET"])
def multi(): pass
""",
        "routes.py",
    )
    assert [route["http_methods"] for route in routes] == [
        ["PUT"],
        ["PATCH"],
        ["DELETE"],
        ["OPTIONS"],
        ["HEAD"],
        ["GET", "POST"],
    ]
    assert routes[-1]["path"] == "/multi"


def test_dynamic_expressions_are_preserved_without_guessing() -> None:
    [route] = analyze_fastapi_file(
        """
from fastapi import FastAPI
app = FastAPI()
@app.get(PATH, response_model=list[User], status_code=status.HTTP_200_OK, tags=TAGS)
def handler(): pass
""",
        "app.py",
    )
    assert route["path"] == "PATH"
    assert route["path_parameters"] == []
    assert route["response_model"] == "list[User]"
    assert route["status_code"] == "status.HTTP_200_OK"
    assert route["tags"] == ["TAGS"]

    [empty_tags] = analyze_fastapi_file(
        """
from fastapi import FastAPI
app = FastAPI()
@app.get("/", tags=[])
def handler(): pass
""",
        "empty_tags.py",
    )
    assert empty_tags["tags"] == []


def test_ordinary_get_objects_and_decorators_are_not_routes() -> None:
    assert (
        analyze_fastapi_file(
            """
class Client:
    def get(self, path): return decorate
client = Client()

@client.get("/not-a-route")
@ordinary
def handler(): pass
""",
            "client.py",
        )
        == []
    )


def test_target_module_is_parsed_but_never_executed() -> None:
    routes = analyze_fastapi_file(
        """
raise RuntimeError("must not execute")
from fastapi import FastAPI
app = FastAPI()
@app.get("/")
def root(): return explode()
""",
        "unsafe.py",
    )
    assert routes[0]["handler_name"] == "root"
    assert routes[0]["unawaited_calls"] == ["explode"]

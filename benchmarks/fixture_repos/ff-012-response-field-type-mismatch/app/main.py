from fastapi import FastAPI

from app.schemas import UserResponse

app = FastAPI()


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/users")
def list_users() -> list[dict[str, int | str]]:
    return [{"id": 1, "name": "Ada"}]


@app.get("/users/{user_id}", response_model=UserResponse)
def get_user(user_id: int) -> dict[str, int | str]:
    return {"id": user_id, "name": "Ada"}

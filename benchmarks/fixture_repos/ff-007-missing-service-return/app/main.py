from fastapi import FastAPI

from app.schemas import UserResponse
from app.service import get_user, list_users

app = FastAPI()


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/users", response_model=list[UserResponse])
async def list_users_endpoint() -> list[dict[str, object]]:
    return await list_users()


@app.get("/users/{user_id}", response_model=UserResponse)
async def get_user_endpoint(user_id: int) -> dict[str, object] | None:
    return await get_user(user_id)

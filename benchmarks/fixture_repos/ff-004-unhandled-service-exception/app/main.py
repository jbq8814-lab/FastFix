from fastapi import FastAPI

from app.schemas import UserResponse
from app.service import fetch_user

app = FastAPI()


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int) -> dict[str, object]:
    return await fetch_user(user_id)

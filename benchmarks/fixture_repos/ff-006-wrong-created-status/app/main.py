from fastapi import FastAPI

from app.schemas import UserCreate, UserResponse
from app.service import create_user

app = FastAPI()


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/users", response_model=UserResponse)
async def create_user_endpoint(payload: UserCreate) -> dict[str, object]:
    return await create_user(payload.name)

from fastapi import FastAPI

from app.service import UserService

app = FastAPI()
user_service = UserService()


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/users")
def list_users() -> list[dict[str, int | str]]:
    return user_service.list_users()


@app.get("/users/{user_id}")
async def get_user(user_id: int) -> dict[str, int | str]:
    return await user_service.get_user(user_id)

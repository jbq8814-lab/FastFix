import asyncio

USERS: dict[int, dict[str, object]] = {
    1: {"id": 1, "name": "Ada Lovelace"},
}


async def list_users() -> list[dict[str, object]]:
    await asyncio.sleep(0)
    return list(USERS.values())


async def get_user(user_id: int) -> dict[str, object] | None:
    await asyncio.sleep(0)
    user = USERS.get(user_id)
    if user is None:
        return None

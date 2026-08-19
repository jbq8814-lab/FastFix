import asyncio


class UserNotFoundError(Exception):
    pass


async def fetch_user(user_id: int) -> dict[str, object]:
    await asyncio.sleep(0)
    if user_id == 404:
        raise UserNotFoundError("User not found")
    return {"id": user_id, "name": "Ada Lovelace"}

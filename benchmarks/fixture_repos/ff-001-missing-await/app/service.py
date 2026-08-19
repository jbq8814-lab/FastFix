import asyncio


async def fetch_user(user_id: int) -> dict[str, object]:
    await asyncio.sleep(0)
    return {"id": user_id, "name": "Ada Lovelace"}

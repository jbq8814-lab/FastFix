import asyncio


async def create_user(name: str) -> dict[str, object]:
    await asyncio.sleep(0)
    return {"id": 1, "name": name}

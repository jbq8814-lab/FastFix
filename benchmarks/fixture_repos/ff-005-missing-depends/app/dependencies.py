from fastapi import Header, HTTPException


async def require_api_key(
    x_api_key: str | None = Header(default=None),
) -> str:
    if x_api_key != "secret-key":
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key

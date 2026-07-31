from fastapi import FastAPI

from app.dependencies import require_api_key

app = FastAPI()


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/protected")
async def read_protected(api_key: str) -> dict[str, str]:
    return {"message": "Access granted", "api_key": api_key}

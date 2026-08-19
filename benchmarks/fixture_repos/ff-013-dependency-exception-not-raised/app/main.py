from typing import Annotated

from fastapi import Depends, FastAPI

from app.dependencies import require_api_key

app = FastAPI()


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/public")
def public_endpoint() -> dict[str, str]:
    return {"access": "public"}


@app.get("/admin/stats")
def admin_stats(_api_key: Annotated[str, Depends(require_api_key)]) -> dict[str, bool]:
    return {"authenticated": True}

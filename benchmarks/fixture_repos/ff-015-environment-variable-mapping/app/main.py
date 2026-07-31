from fastapi import FastAPI

from app.config import get_app_name, get_app_version

app = FastAPI()


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/config/name")
def app_name() -> dict[str, str]:
    return {"name": get_app_name()}


@app.get("/config/version")
def app_version() -> dict[str, str]:
    return {"version": get_app_version()}

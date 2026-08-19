from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine)

    def override_get_db() -> Generator[Session, None, None]:
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_health_check(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_users_starts_empty(client: TestClient):
    response = client.get("/users")
    assert response.status_code == 200
    assert response.json() == []


def test_created_user_can_be_fetched(client: TestClient):
    created = client.post("/users", json={"name": "Ada Lovelace"})
    assert created.status_code == 201
    assert created.json() == {"id": 1, "name": "Ada Lovelace"}

    fetched = client.get(f"/users/{created.json()['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created.json()

from collections.abc import Generator
from inspect import isgeneratorfunction

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
        with test_session() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
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


def test_get_db_must_be_generator():
    """get_db must be a generator function that yields Session.

    A proper implementation uses 'yield' to create a generator that
    guarantees session cleanup via try/finally. The buggy implementation
    directly returns Session without yield, breaking the generator contract
    and causing session leaks.
    """
    # get_db MUST be a generator function (using yield)
    assert isgeneratorfunction(get_db), (
        "get_db must use 'yield' to be a proper generator function. "
        "Without yield, the session cannot be guaranteed to be closed."
    )


def test_session_management_via_http_requests(client: TestClient):
    """Via HTTP: create a user then fetch them, verifying session reuse works."""
    # Create a user
    created = client.post("/users", json={"name": "Alan Turing"})
    assert created.status_code == 201
    assert created.json() == {"id": 1, "name": "Alan Turing"}

    # Fetch all users
    response = client.get("/users")
    assert response.status_code == 200
    assert len(response.json()) == 1

    # Fetch specific user
    fetched = client.get("/users/1")
    assert fetched.status_code == 200
    assert fetched.json() == {"id": 1, "name": "Alan Turing"}


def test_created_user_can_be_fetched(client: TestClient):
    created = client.post("/users", json={"name": "Ada Lovelace"})
    assert created.status_code == 201
    assert created.json() == {"id": 1, "name": "Ada Lovelace"}

    fetched = client.get(f"/users/{created.json()['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
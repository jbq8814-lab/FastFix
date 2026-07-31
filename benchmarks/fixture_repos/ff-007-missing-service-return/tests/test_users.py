from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_users():
    response = client.get("/users")
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "Ada Lovelace"}]


def test_get_existing_user():
    response = client.get("/users/1")
    assert response.status_code == 200
    assert response.json() == {"id": 1, "name": "Ada Lovelace"}

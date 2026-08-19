from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_existing_user():
    response = client.get("/users/7")
    assert response.status_code == 200
    assert response.json() == {"id": 7, "name": "Ada Lovelace"}


def test_missing_user_returns_404():
    response = client.get("/users/404")
    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}

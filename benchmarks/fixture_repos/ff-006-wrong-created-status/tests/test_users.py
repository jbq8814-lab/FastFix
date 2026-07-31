from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_user_returns_created_response():
    response = client.post("/users", json={"name": "Ada Lovelace"})
    assert response.status_code == 201
    assert response.json() == {"id": 1, "name": "Ada Lovelace"}

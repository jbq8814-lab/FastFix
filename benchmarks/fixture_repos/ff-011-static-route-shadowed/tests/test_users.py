from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_numeric_user_id():
    response = client.get("/users/1")
    assert response.status_code == 200
    assert response.json() == {"id": 1}


def test_current_user():
    response = client.get("/users/me")
    assert response.status_code == 200
    assert response.json() == {"username": "current-user"}

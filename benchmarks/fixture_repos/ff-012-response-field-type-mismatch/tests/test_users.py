from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_user_list():
    response = client.get("/users")
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "Ada"}]


def test_user_detail():
    response = client.get("/users/1")
    assert response.status_code == 200
    assert response.json() == {"id": 1, "name": "Ada"}

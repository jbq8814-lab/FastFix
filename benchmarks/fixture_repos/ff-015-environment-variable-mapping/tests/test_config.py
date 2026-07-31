from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_version():
    response = client.get("/config/version")
    assert response.status_code == 200
    assert response.json() == {"version": "2.0.0"}


def test_app_name():
    response = client.get("/config/name")
    assert response.status_code == 200
    assert response.json() == {"name": "FastFix Test"}

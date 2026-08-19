from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_public_endpoint():
    response = client.get("/public")
    assert response.status_code == 200
    assert response.json() == {"access": "public"}


def test_invalid_api_key_is_rejected():
    response = client.get("/admin/stats", headers={"X-API-Key": "invalid"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid API key"}

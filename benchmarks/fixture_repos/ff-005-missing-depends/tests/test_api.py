from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_protected_without_api_key_returns_401():
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid API key"}


def test_protected_with_valid_api_key_returns_200():
    response = client.get("/protected", headers={"X-API-Key": "secret-key"})
    assert response.status_code == 200
    assert response.json() == {
        "message": "Access granted",
        "api_key": "secret-key",
    }

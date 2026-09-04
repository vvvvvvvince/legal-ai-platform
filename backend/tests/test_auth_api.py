from fastapi.testclient import TestClient

from app.main import app
from app.services.auth_store import AuthStore


def test_login_sets_cookie_and_session_returns_identity(monkeypatch, tmp_path):
    db = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("AUTH_DB", str(db))
    AuthStore(db).create_user("alice", "Alice", "correct-password", "13800000001")

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "alice", "password": "correct-password"})
        assert login.status_code == 200
        assert "legal_ai_session=" in login.headers["set-cookie"]
        assert client.get("/api/auth/session").json()["workspace_id"] == "shared"


def test_business_route_rejects_missing_session_and_spoofed_tenant(monkeypatch, tmp_path):
    db = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("AUTH_DB", str(db))
    AuthStore(db).create_user("alice", "Alice", "correct-password", "13800000001")

    with TestClient(app) as client:
        response = client.get("/api/system-status", headers={"X-Tenant-ID": "other"})
        assert response.status_code == 401


def test_login_is_rate_limited_after_repeated_failures(monkeypatch, tmp_path):
    db = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("AUTH_DB", str(db))
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    AuthStore(db).create_user("alice", "Alice", "correct-password", "13800000001")

    with TestClient(app) as client:
        for _ in range(3):
            assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
        blocked = client.post("/api/auth/login", json={"username": "alice", "password": "correct-password"})

    assert blocked.status_code == 429

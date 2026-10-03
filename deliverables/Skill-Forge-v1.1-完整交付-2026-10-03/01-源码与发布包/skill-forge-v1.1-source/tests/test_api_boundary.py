"""健康探活、鉴权与 Origin 边界测试（主§13 本地边界）。"""
from fastapi.testclient import TestClient


def test_health_ok_without_token(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_protected_route_requires_token(client: TestClient) -> None:
    resp = client.get("/api/me")
    assert resp.status_code == 401


def test_protected_route_accepts_valid_token(client: TestClient) -> None:
    resp = client.get("/api/me", headers={"x-session-token": "test-token"})
    assert resp.status_code == 200
    assert resp.json() == {"auth": "ok"}


def test_protected_route_rejects_wrong_token(client: TestClient) -> None:
    resp = client.get("/api/me", headers={"x-session-token": "wrong"})
    assert resp.status_code == 401


def test_foreign_origin_rejected(client: TestClient) -> None:
    resp = client.get(
        "/api/health",
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_local_origin_allowed(client: TestClient) -> None:
    resp = client.get(
        "/api/me",
        headers={"Origin": "http://localhost:5173", "x-session-token": "test-token"},
    )
    assert resp.status_code == 200

"""S2 项目 CRUD 测试。"""
from fastapi.testclient import TestClient

AUTH = {"x-session-token": "test-token"}


def test_create_project(client: TestClient) -> None:
    resp = client.post("/api/projects", json={"name": "P1"}, headers=AUTH)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "P1"
    assert body["target_platform"] == "catpaw"
    assert body["id"]


def test_create_project_rejects_unknown_platform(client: TestClient) -> None:
    resp = client.post(
        "/api/projects", json={"name": "P2", "target_platform": "nope"}, headers=AUTH
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "validation_failed"


def test_create_project_rejects_empty_name(client: TestClient) -> None:
    resp = client.post("/api/projects", json={"name": ""}, headers=AUTH)
    assert resp.status_code == 422  # Pydantic min_length


def test_list_and_get_project(client: TestClient, project: dict) -> None:
    resp = client.get("/api/projects", headers=AUTH)
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()]
    assert project["id"] in ids

    resp = client.get(f"/api/projects/{project['id']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["name"] == "测试项目"


def test_get_missing_project_404(client: TestClient) -> None:
    resp = client.get("/api/projects/NOPE", headers=AUTH)
    assert resp.status_code == 404


def test_delete_project(client: TestClient, project: dict) -> None:
    pid = project["id"]
    resp = client.delete(f"/api/projects/{pid}", headers=AUTH)
    assert resp.status_code == 204
    resp = client.get(f"/api/projects/{pid}", headers=AUTH)
    assert resp.status_code == 404


def test_projects_require_token(client: TestClient) -> None:
    resp = client.get("/api/projects")
    assert resp.status_code == 401

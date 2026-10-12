"""S2 brief 导入、三层分列、确认与修订历史测试（含附件指令隔离）。"""
import copy

from fastapi.testclient import TestClient

AUTH = {"x-session-token": "test-token"}
from tests.conftest import VALID_BRIEF  # noqa: E402


def _brief_with(overrides: dict) -> dict:
    brief = copy.deepcopy(VALID_BRIEF)
    brief.update(overrides)
    return brief


def test_import_valid_brief(client: TestClient, project: dict) -> None:
    pid = project["id"]
    resp = client.post(
        f"/api/projects/{pid}/briefs",
        json={"brief": VALID_BRIEF},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["revision_id"]
    # 三层分列：user_message → explicit；system_inferred → inferred
    assert "purpose" in body["explicit"]
    assert any(p.startswith("non_trigger_conditions") for p in body["inferred"])
    # 业务校验通过：无缺失字段
    assert body["issues"] == []


def test_import_brief_missing_fields_reports_issues(client: TestClient, project: dict) -> None:
    brief = _brief_with({"purpose": None, "outputs": []})
    resp = client.post(
        f"/api/projects/{project['id']}/briefs", json={"brief": brief}, headers=AUTH
    )
    assert resp.status_code == 201  # 校验失败不阻断落库，驱动追问
    issues = resp.json()["issues"]
    assert any("purpose" in i for i in issues)
    assert any("outputs" in i for i in issues)


def test_import_brief_trigger_overlap_detected(client: TestClient, project: dict) -> None:
    brief = _brief_with(
        {
            "non_trigger_conditions": [
                {"value": "帮我整理会议纪要", "source": {"type": "system_inferred", "ref": ""}}
            ]
        }
    )
    resp = client.post(
        f"/api/projects/{project['id']}/briefs", json={"brief": brief}, headers=AUTH
    )
    assert resp.status_code == 201
    assert any("重叠" in i for i in resp.json()["issues"])


def test_import_brief_unregistered_platform(client: TestClient, project: dict) -> None:
    brief = _brief_with({"target_platform": "nope"})
    resp = client.post(
        f"/api/projects/{project['id']}/briefs", json={"brief": brief}, headers=AUTH
    )
    assert resp.status_code == 201
    assert any("未注册" in i for i in resp.json()["issues"])


def test_attachment_only_fields_go_to_confirm(client: TestClient, project: dict) -> None:
    """附件来源字段必须进待确认列（主§6.1 附件指令隔离）。"""
    brief = _brief_with(
        {
            "constraints": [
                {"value": "来自附件的指令", "source": {"type": "attachment_text", "ref": "doc.md:12"}}
            ]
        }
    )
    resp = client.post(
        f"/api/projects/{project['id']}/briefs", json={"brief": brief}, headers=AUTH
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "constraints[0]" in body["to_confirm"]
    assert "constraints[0]" not in body["explicit"]


def test_confirm_brief_marks_confirmed_and_creates_revision(client: TestClient, project: dict) -> None:
    pid = project["id"]
    resp = client.post(f"/api/projects/{pid}/briefs", json={"brief": VALID_BRIEF}, headers=AUTH)
    draft_rev = resp.json()["revision_id"]

    resp = client.patch(
        f"/api/projects/{pid}/briefs/{draft_rev}",
        json={"edits": {"purpose": "把会议纪要整理成带负责人的行动项"}},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["revision_id"] != draft_rev  # 确认产生新修订（不可变历史）

    # 编辑生效
    resp = client.get(f"/api/projects/{pid}/briefs/{body['revision_id']}", headers=AUTH)
    assert resp.json()["brief"]["purpose"]["value"] == "把会议纪要整理成带负责人的行动项"

    # 历史保留：draft 与 confirmed 都在列表
    resp = client.get(f"/api/projects/{pid}/briefs", headers=AUTH)
    statuses = [r["status"] for r in resp.json()]
    assert "draft" in statuses and "confirmed" in statuses


def test_confirm_brief_rejects_unknown_path(client: TestClient, project: dict) -> None:
    pid = project["id"]
    resp = client.post(f"/api/projects/{pid}/briefs", json={"brief": VALID_BRIEF}, headers=AUTH)
    draft_rev = resp.json()["revision_id"]
    resp = client.patch(
        f"/api/projects/{pid}/briefs/{draft_rev}",
        json={"edits": {"no_such_field": "x"}},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_confirm_brief_keeps_attachment_unconfirmed(client: TestClient, project: dict) -> None:
    """附件字段未经显式编辑确认：confirm 后仍留在待确认列。"""
    pid = project["id"]
    brief = _brief_with(
        {
            "constraints": [
                {"value": "来自附件的指令", "source": {"type": "attachment_text", "ref": "doc.md:12"}}
            ]
        }
    )
    resp = client.post(f"/api/projects/{pid}/briefs", json={"brief": brief}, headers=AUTH)
    draft_rev = resp.json()["revision_id"]

    resp = client.patch(
        f"/api/projects/{pid}/briefs/{draft_rev}", json={"edits": {}}, headers=AUTH
    )
    body = resp.json()
    # constraints[0] 未被显式编辑 → 保持待确认
    assert "constraints[0]" in body["to_confirm"]

    # 显式编辑确认后转正
    resp = client.patch(
        f"/api/projects/{pid}/briefs/{draft_rev}",
        json={"edits": {"constraints[0]": "来自附件的指令"}},
        headers=AUTH,
    )
    body = resp.json()
    assert "constraints[0]" not in body["to_confirm"]
    assert "constraints[0]" in body["explicit"]


def test_get_brief_404(client: TestClient, project: dict) -> None:
    resp = client.get(f"/api/projects/{project['id']}/briefs/NOPE", headers=AUTH)
    assert resp.status_code == 404

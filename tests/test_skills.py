"""S3 测试：草稿 CRUD、安全校验、不可变版本、冲突、篡改检测。

S4 起版本创建改为异步任务（202 + job_id）；本文件测试统一经
_run_version 辅助函数入队并由本地 Worker 执行后取回结果，
同步语义（201/422/409 等价物）从 job.result/job.error 读取。
"""
import hashlib

from fastapi.testclient import TestClient

from app.workers.worker import Worker

AUTH = {"x-session-token": "test-token"}

VALID_SKILL_MD = """---
name: meeting-notes-organizer
description: 把会议纪要整理成结构化行动项列表，输出含负责人与截止日的 Markdown。
---

# 会议纪要整理

读取用户提供的纪要文本，提取行动项。

参考脚本：[extract.py](scripts/extract.py)
"""


def _write(client: TestClient, pid: str, path: str, content: str) -> dict:
    resp = client.put(
        f"/api/projects/{pid}/skills/draft/files/{path}",
        json={"content": content},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run_version(client: TestClient, pid: str, label: str | None = None) -> dict:
    """入队 version_create 并由 Worker 执行，返回 job 视图（含 result/error）。"""
    resp = client.post(
        f"/api/projects/{pid}/skills/versions",
        json={"label": label} if label else {},
        headers=AUTH,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    Worker().run_pending()
    resp = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert resp.status_code == 200
    return resp.json()


def test_draft_write_and_read(client: TestClient, project: dict) -> None:
    pid = project["id"]
    _write(client, pid, "SKILL.md", VALID_SKILL_MD)

    resp = client.get(f"/api/projects/{pid}/skills/draft", headers=AUTH)
    assert resp.status_code == 200
    paths = [f["path"] for f in resp.json()["files"]]
    assert paths == ["SKILL.md"]

    resp = client.get(f"/api/projects/{pid}/skills/draft/files/SKILL.md", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["content"] == VALID_SKILL_MD


def test_path_traversal_rejected(client: TestClient, project: dict) -> None:
    """路径穿越 payload 被拒（S3 完成定义）。

    说明：`..` 段与绝对路径会被 ASGI 服务器在路由层规范化拦截
    （同样无法到达处理器），此处 API 层测反斜杠等可达路径，
    穿越全集由 test_normalize_relpath 单元测试覆盖。
    """
    pid = project["id"]
    for bad in ("a\\b.md", "C:/evil.md"):
        resp = client.put(
            f"/api/projects/{pid}/skills/draft/files/{bad}",
            json={"content": "x"},
            headers=AUTH,
        )
        assert resp.status_code in (404, 405, 422), f"{bad} 意外通过: {resp.text}"


def test_normalize_relpath() -> None:
    """normalize_relpath 穿越与非法路径全集单元测试。"""
    from app.services.skill_files import FileValidationError, normalize_relpath

    for bad in (
        "../evil.md",
        "a/../../evil.md",
        "/etc/passwd",
        "C:/evil.md",
        "C:\\evil.md",
        "a\\b.md",
        "..",
        "",
        "   ",
        "a\x00b.md",
        "evil.exe",
    ):
        try:
            normalize_relpath(bad)
        except FileValidationError:
            continue
        raise AssertionError(f"normalize_relpath 未拒绝: {bad!r}")

    # 合法路径应通过
    for good in ("SKILL.md", "scripts/extract.py", "references/deep/nested.md", "a-b.v1.md"):
        normalize_relpath(good)


def test_disallowed_extension_rejected(client: TestClient, project: dict) -> None:
    pid = project["id"]
    resp = client.put(
        f"/api/projects/{pid}/skills/draft/files/evil.exe",
        json={"content": "x"},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_oversize_file_rejected(client: TestClient, project: dict) -> None:
    pid = project["id"]
    resp = client.put(
        f"/api/projects/{pid}/skills/draft/files/big.md",
        json={"content": "x" * (1_000_001)},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_create_version_success_and_immutability(client: TestClient, project: dict) -> None:
    """版本创建成功；改草稿后版本内容与哈希不变（S3 完成定义）。"""
    pid = project["id"]
    _write(client, pid, "SKILL.md", VALID_SKILL_MD)
    _write(client, pid, "scripts/extract.py", "print('extract')\n")

    job = _run_version(client, pid, "v1")
    assert job["status"] == "succeeded", job
    v1 = job["result"]
    assert v1["label"] == "v1"
    assert v1["status"] == "validated"
    assert v1["warnings"] == []
    assert {f["path"] for f in v1["files"]} == {"SKILL.md", "scripts/extract.py"}
    original_hash = v1["content_hash"]

    # 修改草稿 → 版本不受影响
    _write(client, pid, "SKILL.md", VALID_SKILL_MD + "\n新增内容\n")
    resp = client.get(f"/api/projects/{pid}/skills/versions/{v1['id']}", headers=AUTH)
    assert resp.json()["content_hash"] == original_hash

    # 版本内文件内容原样
    resp = client.get(
        f"/api/projects/{pid}/skills/versions/{v1['id']}/files/SKILL.md", headers=AUTH
    )
    assert resp.json()["content"] == VALID_SKILL_MD


def test_create_version_conflict_on_same_content(client: TestClient, project: dict) -> None:
    """相同内容哈希 → job.error.code=conflict（S3 完成定义，异步等价物）。"""
    pid = project["id"]
    _write(client, pid, "SKILL.md", VALID_SKILL_MD)
    _write(client, pid, "scripts/extract.py", "print('x')\n")
    job = _run_version(client, pid, "a")
    assert job["status"] == "succeeded"
    job = _run_version(client, pid, "b")
    assert job["status"] == "failed"
    assert job["error"]["code"] == "conflict"
    assert job["error"]["existing_version_id"]

    # 内容变化后可再创建（SKILL.md 引用 scripts/extract.py，一并保留）
    _write(client, pid, "SKILL.md", VALID_SKILL_MD + "\nchanged\n")
    job = _run_version(client, pid, "c")
    assert job["status"] == "succeeded"


def test_create_version_empty_draft_rejected(client: TestClient, project: dict) -> None:
    pid = project["id"]
    job = _run_version(client, pid)
    assert job["status"] == "failed"
    assert any("空技能" in e for e in job["error"]["errors"])


def test_version_validation_errors(client: TestClient, project: dict) -> None:
    """缺 frontmatter 字段 / 引用不存在文件 → 422。"""
    pid = project["id"]

    # 引用了不存在的 scripts/extract.py
    _write(client, pid, "SKILL.md", VALID_SKILL_MD)
    job = _run_version(client, pid)
    assert job["status"] == "failed"
    assert any("不存在" in e for e in job["error"]["errors"])

    # 补上引用文件后通过；再测缺 name
    _write(client, pid, "scripts/extract.py", "print('x')\n")
    job = _run_version(client, pid)
    assert job["status"] == "succeeded"

    _write(
        client,
        pid,
        "SKILL.md",
        "---\nname: Meeting Notes\ndescription: x\n---\n\n正文\n",
    )
    _write(client, pid, "scripts/extract.py", "print('x')\n")
    job = _run_version(client, pid)
    # name 非 kebab-case 是 warning 不是 error
    assert job["status"] == "succeeded"
    assert any("kebab-case" in w for w in job["result"]["warnings"])


def test_version_file_tamper_detected(client: TestClient, project: dict, tmp_path) -> None:
    """版本目录被外部篡改 → 读取时哈希校验失败。"""
    from app.core.config import load_config

    pid = project["id"]
    _write(client, pid, "SKILL.md", VALID_SKILL_MD)
    _write(client, pid, "scripts/extract.py", "print('x')\n")
    job = _run_version(client, pid)
    vid = job["result"]["id"]

    # 直接改版本目录中的文件（模拟外部篡改）
    cfg = load_config()
    target = cfg.data_dir / "projects" / pid / "skills" / "versions" / vid / "SKILL.md"
    assert target.is_file()
    target.write_text("被篡改的内容", encoding="utf-8")

    resp = client.get(f"/api/projects/{pid}/skills/versions/{vid}/files/SKILL.md", headers=AUTH)
    assert resp.status_code == 422
    assert any("篡改" in e for e in resp.json()["detail"]["errors"])


def test_version_binds_latest_confirmed_brief(client: TestClient, project: dict) -> None:
    """版本创建时绑定最新已确认需求修订（主§7 字段）。"""
    from tests.conftest import VALID_BRIEF

    pid = project["id"]
    resp = client.post(f"/api/projects/{pid}/briefs", json={"brief": VALID_BRIEF}, headers=AUTH)
    draft_rev = resp.json()["revision_id"]
    resp = client.patch(
        f"/api/projects/{pid}/briefs/{draft_rev}", json={"edits": {}}, headers=AUTH
    )
    confirmed_rev = resp.json()["revision_id"]

    _write(client, pid, "SKILL.md", VALID_SKILL_MD)
    _write(client, pid, "scripts/extract.py", "print('x')\n")
    job = _run_version(client, pid)
    assert job["result"]["brief_revision_id"] == confirmed_rev


def test_delete_draft_file(client: TestClient, project: dict) -> None:
    pid = project["id"]
    _write(client, pid, "SKILL.md", VALID_SKILL_MD)
    _write(client, pid, "scripts/extract.py", "print('x')\n")
    resp = client.delete(f"/api/projects/{pid}/skills/draft/files/scripts/extract.py", headers=AUTH)
    assert resp.status_code == 204

    resp = client.get(f"/api/projects/{pid}/skills/draft", headers=AUTH)
    paths = [f["path"] for f in resp.json()["files"]]
    assert paths == ["SKILL.md"]

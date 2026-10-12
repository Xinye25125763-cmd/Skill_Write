"""F04 回归：带全部关联数据的项目删除。

缺陷背景：删除逻辑只删 brief_revisions 与项目行；SkillVersion 的
brief_revision_id 外键无级联 → SQLite FK 违约 → 500。
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.core.ulid import new_ulid
from app.models import (
    AgentRun,
    BriefRevision,
    EvalCaseResult,
    EvalRun,
    Job,
    Project,
    SkillVersion,
    Suite,
)
from app.services import queue
from app.services.projects import ProjectService

AUTH = {"x-session-token": "test-token"}


def _full_project(client: TestClient, cfg) -> tuple[str, str, str, str]:
    """造一个含全套关联数据的项目：brief + 版本（外键绑定）+ 运行 + 套件 + 评测。"""
    r = client.post("/api/projects", json={"name": "f04 项目"}, headers=AUTH)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    brief = {
        "purpose": {"value": "整理", "source": {"type": "user_message", "ref": "m1"}},
        "trigger_conditions": [{"value": "整理", "source": {"type": "user_message", "ref": "m1"}}],
        "non_trigger_conditions": [],
        "inputs": [],
        "outputs": [],
        "tools": [],
        "constraints": [],
        "success_criteria": [],
        "target_platform": "catpaw",
        "evaluation_preference": {"objective_tests": True, "subjective_scale": False},
    }
    r = client.post(f"/api/projects/{pid}/briefs", json={"brief": brief}, headers=AUTH)
    assert r.status_code == 201, r.text
    brief_id = r.json()["revision_id"]

    with Session(create_db_engine(db_path(cfg))) as s:
        version = SkillVersion(
            id=new_ulid(), project_id=pid, label="v1", content_hash="ch",
            files_json=json.dumps([{"path": "SKILL.md", "hash": "h", "size": 1, "kind": "text"}]),
            brief_revision_id=brief_id,  # F04 核心：无级联外键绑定
        )
        s.add(version)
        s.commit()
        run = AgentRun(
            id=new_ulid(), project_id=pid, version_id=version.id, prompt="p",
            status="finished", exit_reason="completed", created_at=0.0,
            steps_json="[]", prompt_tokens=0, completion_tokens=0,
            sandbox_files_json="{}", sandbox_dir="",
        )
        s.add(run)
        suite = Suite(
            id=new_ulid(), project_id=pid, name="s", cases_json="[]", created_at=0.0,
        )
        s.add(suite)
        s.commit()
        ev = EvalRun(
            id=new_ulid(), project_id=pid, suite_id=suite.id, version_id=version.id,
            status="finished", created_at=0.0, finished_at=1.0,
        )
        s.add(ev)
        s.commit()
        s.add(EvalCaseResult(
            id=new_ulid(), eval_run_id=ev.id, case_id="c1", group_name="candidate",
            agent_run_id=run.id, exec_status="executed", score_status="scored",
            created_at=0.0, assertions_json="[]", passed_count=1, total_count=1,
        ))
        s.commit()
        vid, rid, sid, evid = version.id, run.id, suite.id, ev.id

    # 磁盘数据目录（草稿 + 版本快照 + 运行沙箱）
    for rel in ("skills/draft", f"skills/versions/{vid}", "runs/x/sandbox"):
        d = cfg.data_dir / "projects" / pid / rel
        d.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "projects" / pid / "skills" / "draft" / "SKILL.md").write_text(
        "---\nname: x\n---\n", encoding="utf-8"
    )

    return pid, vid, evid


def test_delete_project_with_all_relations(migrated_db: Path, client: TestClient) -> None:
    """报告复现场景 + 全套关联：删除返回 204，所有子表清空，数据目录消失。"""
    cfg = load_config()
    pid, vid, evid = _full_project(client, cfg)

    r = client.delete(f"/api/projects/{pid}", headers=AUTH)
    assert r.status_code == 204, r.text

    with Session(create_db_engine(db_path(cfg))) as s:
        assert s.get(Project, pid) is None
        assert s.query(BriefRevision).filter_by(project_id=pid).count() == 0
        assert s.query(SkillVersion).filter_by(project_id=pid).count() == 0
        assert s.query(AgentRun).filter_by(project_id=pid).count() == 0
        assert s.query(Suite).filter_by(project_id=pid).count() == 0
        assert s.query(EvalRun).filter_by(project_id=pid).count() == 0
        assert s.query(EvalCaseResult).filter_by(eval_run_id=evid).count() == 0
    assert not (cfg.data_dir / "projects" / pid).exists()

    # 列表不再包含
    r = client.get("/api/projects", headers=AUTH)
    assert all(p["id"] != pid for p in r.json())
    # 再删一次 → 404
    r = client.delete(f"/api/projects/{pid}", headers=AUTH)
    assert r.status_code == 404


def test_delete_project_cancels_queued_jobs(migrated_db: Path, client: TestClient) -> None:
    """关联的排队任务被标 cancelled（带 project_deleted 原因）。"""
    cfg = load_config()
    r = client.post("/api/projects", json={"name": "f04b"}, headers=AUTH)
    pid = r.json()["id"]

    with Session(create_db_engine(db_path(cfg))) as s:
        j1 = queue.enqueue(s, "sleep", {"seconds": 0.1, "project_id": pid})
        j2 = queue.enqueue(s, "sleep", {"seconds": 30, "project_id": pid})
        j3 = queue.enqueue(s, "sleep", {"seconds": 0.1, "project_id": "别的项目"})
        # j1 领取并落终态（succeeded），应保留
        queue.claim(s, "w", 60)
        queue.complete(s, j1.id, {"ok": True})
        ids = (j1.id, j2.id, j3.id)

    r = client.delete(f"/api/projects/{pid}", headers=AUTH)
    assert r.status_code == 204, r.text

    with Session(create_db_engine(db_path(cfg))) as s:
        after1, after2, after3 = (s.get(Job, i) for i in ids)
        assert after2.status == "cancelled"
        assert json.loads(after2.error_json)["code"] == "project_deleted"
        assert after1.status == "succeeded"  # 终态不动
        assert after3.status == "queued"  # 他项目不动


def test_delete_project_rollback_on_failure(migrated_db: Path, client: TestClient, monkeypatch) -> None:
    """事务失败整体回滚：项目与关联数据保留（不出现半删除）。"""
    cfg = load_config()
    r = client.post("/api/projects", json={"name": "f04c"}, headers=AUTH)
    pid = r.json()["id"]

    # 注入真实 session.delete；真实服务走完整子表删除后失败，检验回滚。
    import pytest
    with Session(create_db_engine(db_path(cfg))) as session:
        def reject_delete(obj):
            raise RuntimeError("injected real delete failure")
        monkeypatch.setattr(session, "delete", reject_delete)
        with pytest.raises(RuntimeError):
            ProjectService(session).delete(pid)
    assert client.get(f"/api/projects/{pid}", headers=AUTH).status_code == 200

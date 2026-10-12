"""T03：持久化流程、阶段检查点和增量迁移。

本项验收（工程计划 §5 T03）：
1. 旧库升级/往返（0007 → 0008 后所有功能正常）；
2. 陈旧 revision 修改 → 409（CAS）；
3. 用户旧草稿不变；
4. 同阶段重复请求只产生一个有效阶段任务（唯一阶段键 + 幂等）。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Project


# ---------- 1：旧库升级 / 往返 ----------


def test_old_db_upgrades_and_flow_roundtrip(migrated_db: Path) -> None:
    """0008 迁移后：创建 flow → 快照往返（合同/规则/检查点/预算无损）。"""
    from app.generation.flow import create_flow, get_flow
    from app.generation.contracts import FlowState

    with Session(create_engine(f"sqlite:///{migrated_db}")) as session:
        pid = "01AAAAAAAAAAAAAAAAAAAAAAAA"
        session.add(Project(id=pid, name="p", target_platform="codex"))
        session.commit()

        request = {
            "request_text": "帮我做一个周报助手",
            "target_platform": "codex",
            "resource_ids": [],
            "profile_revision": 1,
            "budget": {"total_tokens": 150000},
        }
        snap = create_flow(session, project_id=pid, request=request)
        assert snap.state == FlowState.QUEUED
        assert snap.revision == 1
        assert snap.budget.total_tokens == 150000
        assert snap.checkpoint == {}

        restored = get_flow(session, snap.flow_id)
        assert restored.state == FlowState.QUEUED
        assert restored.revision == 1
        assert restored.budget.total_tokens == 150000
        assert restored.flow_id == snap.flow_id


def test_migration_0008_creates_tables(migrated_db: Path) -> None:
    """迁移后两张新表存在且结构正确。"""
    import sqlite3

    con = sqlite3.connect(migrated_db)
    try:
        tables = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "generation_flows" in tables
        assert "generation_stage_runs" in tables

        cols = {r[1] for r in con.execute("PRAGMA table_info(generation_flows)")}
        for expected in (
            "id", "project_id", "request_json", "state", "revision",
            "snapshot_json", "created_at", "updated_at",
        ):
            assert expected in cols, f"generation_flows 缺列 {expected}"

        cols2 = {r[1] for r in con.execute("PRAGMA table_info(generation_stage_runs)")}
        for expected in ("id", "flow_id", "stage", "job_id", "stage_key", "created_at"):
            assert expected in cols2, f"generation_stage_runs 缺列 {expected}"
    finally:
        con.close()


# ---------- 2：陈旧 revision 修改 → 409 ----------


def test_stale_revision_transition_rejected(migrated_db: Path) -> None:
    from app.generation.flow import StaleRevision, create_flow, transition_flow
    from app.generation.contracts import FlowState

    with Session(create_engine(f"sqlite:///{migrated_db}")) as session:
        pid = "01AAAAAAAAAAAAAAAAAAAAAAAA"
        session.add(Project(id=pid, name="p"))
        session.commit()
        snap = create_flow(session, project_id=pid, request={"request_text": "x"})

        # 正确 revision：成功，revision+1
        snap2 = transition_flow(
            session, flow_id=snap.flow_id, expected_revision=1,
            state=FlowState.EXTRACTING, checkpoint={"phase": "requirements"},
        )
        assert snap2.revision == 2
        assert snap2.state == FlowState.EXTRACTING
        assert snap2.checkpoint == {"phase": "requirements"}

        # 陈旧 revision（还用 1）：409 语义
        with pytest.raises(StaleRevision) as excinfo:
            transition_flow(
                session, flow_id=snap.flow_id, expected_revision=1,
                state=FlowState.PLANNING, checkpoint={},
            )
        assert "409" in str(excinfo.value) and "revision" in str(excinfo.value)
        assert excinfo.value.actual == 2 and excinfo.value.expected == 1

        # 数据未被陈旧写入污染
        snap3 = transition_flow(
            session, flow_id=snap.flow_id, expected_revision=2,
            state=FlowState.PLANNING, checkpoint={"phase": "plan"},
        )
        assert snap3.revision == 3
        assert snap3.state == FlowState.PLANNING


# ---------- 3：用户旧草稿不变 ----------


def test_user_draft_untouched_by_flow_operations(
    migrated_db: Path, client: TestClient
) -> None:
    """创建/推进 flow 不触碰既有 brief 草稿与技能草稿目录。"""
    headers = {"x-session-token": "test-token"}

    r = client.post("/api/projects", json={"name": "p"}, headers=headers)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    # 写用户草稿
    r = client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": "# 我的草稿\n用户手写内容"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # 创建 flow（走 API 层触发存储路径）
    r = client.post(
        f"/api/projects/{pid}/generation-flows",
        json={"request_text": "做个周报助手"},
        headers=headers,
    )
    assert r.status_code in (201, 202), r.text
    flow_id = r.json()["flow_id"]

    # 旧草稿原样
    r = client.get(f"/api/projects/{pid}/skills/draft/files/SKILL.md", headers=headers)
    assert r.status_code == 200
    assert "用户手写内容" in r.json()["content"]

    # flow 可查
    r = client.get(f"/api/generation-flows/{flow_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["state"] == "queued"


# ---------- 4：同阶段重复请求只产生一个有效阶段任务 ----------


def test_duplicate_stage_request_single_job(migrated_db: Path) -> None:
    from app.generation.flow import create_flow, run_stage_once
    from app.services.queue import enqueue
    from app.models import Job

    with Session(create_engine(f"sqlite:///{migrated_db}")) as session:
        pid = "01AAAAAAAAAAAAAAAAAAAAAAAA"
        session.add(Project(id=pid, name="p"))
        session.commit()
        snap = create_flow(session, project_id=pid, request={"request_text": "x"})

        # 同一阶段第一次排队
        job1 = run_stage_once(session, flow_id=snap.flow_id, stage="extract")
        assert job1 is not None

        # 同阶段重复请求（唯一阶段键 + 幂等键）：返回同一任务，不新建
        job2 = run_stage_once(session, flow_id=snap.flow_id, stage="extract")
        assert job2.id == job1.id

        # 数据库里该阶段只有一行 stage_run / 一个 job
        stage_rows = session.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM generation_stage_runs WHERE flow_id = :f AND stage = 'extract'"
            ),
            {"f": snap.flow_id},
        ).scalar()
        assert stage_rows == 1

        jobs = session.query(Job).filter_by(type="generation_stage").all()
        assert len(jobs) == 1

        # 不同阶段是新任务
        job3 = run_stage_once(session, flow_id=snap.flow_id, stage="plan")
        assert job3.id != job1.id


# ---------- 状态机约束 ----------


def test_unknown_state_transition_rejected(migrated_db: Path) -> None:
    from app.generation.flow import InvalidTransition, create_flow, transition_flow

    with Session(create_engine(f"sqlite:///{migrated_db}")) as session:
        pid = "01AAAAAAAAAAAAAAAAAAAAAAAA"
        session.add(Project(id=pid, name="p"))
        session.commit()
        snap = create_flow(session, project_id=pid, request={"request_text": "x"})

        # queued → delivering 非法（不存在该状态，模型层直接拒绝）
        with pytest.raises(InvalidTransition):
            transition_flow(
                session, flow_id=snap.flow_id, expected_revision=1,
                state="delivering", checkpoint={},
            )

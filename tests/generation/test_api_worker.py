"""T08：将阶段生成接入现有队列与异步 API。

本项验收（工程计划 §5 T08）：
1. 提交 202（异步受理，非同步完成）；
2. 必要问题暂停时不占 Worker（AWAITING_INPUT 后无 queued/running 任务）；
3. 重复 answers 不重复生成（幂等键 + 状态闸）；
4. 调用中取消不发布候选（取消传播到当前任务，无 candidate 目录）；
5. 迟到 Worker 不覆盖检查点（CAS 围栏 → superseded）。

模型一律用可记录调用的假实现；宿主真实测试另行记录。
"""
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_config
from app.llm import Capabilities, ModelResponse, Usage

AUTH = {"x-session-token": "test-token"}

# ---------- 假模型：剧本回放 + 调用记录 + 可选闸门 ----------


class ScriptedProvider:
    """按剧本顺序返回 JSON；gate_at 指定的调用会阻塞到闸门放行。"""

    capabilities = Capabilities(
        tools=False, streaming=False, json_mode=True, max_context_tokens=100_000
    )

    def __init__(self, script: list[Any], gate_at: int | None = None,
                 gate: threading.Event | None = None) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []
        self.gate_at = gate_at
        self.gate = gate

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN202
        idx = len(self.calls)
        self.calls.append(list(messages))
        if self.gate_at is not None and idx == self.gate_at and self.gate is not None:
            assert self.gate.wait(timeout=10), "闸门超时：主线程未放行"
        if not self.script:
            return ModelResponse(content="{}", usage=Usage(10, 5), finish_reason="stop")
        item = self.script.pop(0)
        return ModelResponse(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
            usage=Usage(100, 200), finish_reason="stop",
        )


def _contract_json(pending_extra: bool) -> dict:
    """提取阶段响应：REQ-001 确认；pending_extra 时加 REQ-002 待确认。"""
    reqs = [{
        "requirement_id": "REQ-001",
        "content": "读取素材并按模板输出周报 Markdown",
        "necessary": True,
        "source_type": "user_message", "source_ref": "msg-1",
        "confirmation": "confirmed",
    }]
    questions: list[str] = []
    if pending_extra:
        reqs.append({
            "requirement_id": "REQ-002",
            "content": "把周报渲染成 PNG",
            "necessary": True,
            "source_type": "system_inferred", "source_ref": "",
            "confirmation": "pending",
            "note": "需要图像渲染工具，当前环境缺失",
        })
        questions = ["REQ-002 需要图像渲染工具，是否必需？"]
    return {"requirements": reqs, "conflicts": [], "open_questions": questions}


def _plan_json() -> dict:
    return {
        "plan_id": "plan-1",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "周报主技能",
             "requirement_ids": ["REQ-001"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "SKILL.md 头信息完整", "requirement_ids": ["REQ-001"]},
        ],
        "tool_notes": [], "blocking_gaps": [],
    }


def _file_json() -> dict:
    return {"path": "SKILL.md", "kind": "text", "complete": True,
            "content": "---\nname: weekly-report\n---\n# 周报\n按模板输出。"}


def _use_provider(monkeypatch: pytest.MonkeyPatch, provider: ScriptedProvider) -> None:
    import app.workers.handlers.generation_stage as generation_stage
    monkeypatch.setattr(generation_stage, "provider_factory", lambda: provider)


def _create_flow(client: TestClient, pid: str, provider: ScriptedProvider,
                 monkeypatch: pytest.MonkeyPatch, pending: bool = False) -> str:
    _use_provider(monkeypatch, provider)
    r = client.post(
        f"/api/projects/{pid}/generation-flows",
        json={"request_text": "帮我做一个周报助手，读素材输出 Markdown"},
        headers=AUTH,
    )
    # 验收 1：202 异步受理 + flow_id + job_id（一次创建即完备）
    assert r.status_code == 202, r.text
    assert r.json()["flow_id"] and r.json()["job_id"]
    return r.json()["flow_id"]


def _flow(client: TestClient, flow_id: str) -> dict:
    r = client.get(f"/api/generation-flows/{flow_id}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 验收 1+2：202 受理 + 暂停不占 Worker ----------


def test_async_flow_pause_resume_cancel_and_owner_fence(
    client: TestClient, project: dict, migrated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers.worker import Worker

    pid = project["id"]
    provider = ScriptedProvider([
        _contract_json(pending_extra=True),   # extract：REQ-002 待确认
        _plan_json(),                          # plan
        _file_json(),                          # generate
    ])
    flow_id = _create_flow(client, pid, provider, monkeypatch, pending=True)

    worker = Worker()
    worker.run_pending()

    # 验收 2：必要问题暂停 → awaiting_input，无 queued/running 任务占 Worker
    view = _flow(client, flow_id)
    assert view["state"] == "awaiting_input"
    assert view["pending_questions"], "必要缺口问题必须呈现给用户"
    assert "REQ-002" in " ".join(view["pending_questions"])

    from app.models import Job
    from app.core.db import create_db_engine, db_path
    with __import__("sqlalchemy.orm", fromlist=["Session"]).Session(
        create_db_engine(db_path(load_config()))
    ) as s:
        active = [j.id for j in s.query(Job).filter(Job.status.in_(("queued", "running")))]
        assert active == [], f"暂停后仍有活跃任务: {active}"

    # 验收 3：回答必要缺口 → 202 → 继续推进；重复 answers 不重复生成
    r = client.post(
        f"/api/generation-flows/{flow_id}/answers",
        json={
            "revision": view["revision"],
            "idempotency_key": "ans-1",
            "answers": {"REQ-002": {"necessary": False}},
        },
        headers=AUTH,
    )
    assert r.status_code == 202, r.text

    worker.run_pending()
    view = _flow(client, flow_id)
    # T10 接线后：验证→发布→冻结→delivered（交付收据入检查点）
    assert view["state"] == "delivered", view
    assert view["checkpoint"]["receipt"]["version_id"], "收据必须带版本 ID"
    calls_after_resume = len(provider.calls)

    # 重复提交同一 answers（同幂等键）→ 不再触发任何生成
    r = client.post(
        f"/api/generation-flows/{flow_id}/answers",
        json={
            "revision": view["revision"],
            "idempotency_key": "ans-1",
            "answers": {"REQ-002": {"necessary": False}},
        },
        headers=AUTH,
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "not_awaiting_input"
    assert len(provider.calls) == calls_after_resume, "重复 answers 不得重复生成"

    # 产物已发布为候选并冻结为版本（T09 验证通过 → T10 发布冻结）
    cfg = load_config()
    candidate = cfg.data_dir / "generation" / pid / "candidate" / flow_id
    assert (candidate / "SKILL.md").is_file()
    assert "周报" in (candidate / "SKILL.md").read_text(encoding="utf-8")
    # 冻结的版本目录存在且可读（T10）
    version_id = view["checkpoint"]["receipt"]["version_id"]
    version_dir = cfg.data_dir / "projects" / pid / "skills" / "versions" / version_id
    assert (version_dir / "SKILL.md").is_file()

    # 验收 5：迟到 Worker 不覆盖检查点（CAS 围栏 → superseded）
    from app.workers.handlers import get_handler
    from app.workers.handlers import HandlerContext
    from app.models import GenerationFlow as FlowRow
    from app.services import queue as queue_svc
    from app.core.db import create_db_engine, db_path as _dbp

    engine = create_db_engine(_dbp(load_config()))
    with __import__("sqlalchemy.orm", fromlist=["Session"]).Session(engine) as s:
        # 复刻迟到场景：为旧阶段（extract）再造一个任务并由"迟到 Worker"执行
        late_job = queue_svc.enqueue(
            s, "generation_stage", {"flow_id": flow_id, "stage": "extract"},
            idempotency_key=f"late:{flow_id}:extract",
        )
        before = s.get(FlowRow, flow_id)
        rev_before = before.revision
        snap_before = json.loads(before.snapshot_json)
        handler = get_handler("generation_stage")
        ctx = HandlerContext(s, late_job.id, lease_seconds=60)
        outcome = handler(s, load_config(), late_job, ctx)
        # 流程已终结：迟到结果作废（skipped_terminal），同样不覆盖检查点
        assert outcome["outcome"] in ("superseded", "skipped_terminal"), outcome
        after = s.get(FlowRow, flow_id)
        assert after.revision == rev_before, "迟到 Worker 不得推进 revision"
        assert json.loads(after.snapshot_json) == snap_before, "迟到 Worker 不得覆盖检查点"

    # ---------- 验收 4：调用中取消不发布候选 ----------

    gate = threading.Event()
    provider2 = ScriptedProvider([
        _contract_json(pending_extra=False),  # extract：完整需求
        _plan_json(),                          # plan
        _file_json(),                          # generate（闸门卡在这里）
    ], gate_at=2, gate=gate)
    flow2 = _create_flow(client, pid, provider2, monkeypatch)

    worker2 = Worker()
    t = threading.Thread(target=worker2.run_pending, daemon=True)
    t.start()

    # 等待进入 generating（worker 阻塞在生成调用上）
    deadline = time.monotonic() + 15
    rev = None
    while time.monotonic() < deadline:
        view2 = _flow(client, flow2)
        if view2["state"] == "generating":
            rev = view2["revision"]
            break
        time.sleep(0.02)
    assert rev is not None, f"未进入 generating: {view2['state']}"

    # 取消：传播到当前任务 + 流程转 cancelled
    r = client.post(
        f"/api/generation-flows/{flow2}/cancel",
        json={"revision": rev, "idempotency_key": "cancel-1"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text

    gate.set()  # 放行被卡的生成调用
    t.join(timeout=15)
    assert not t.is_alive(), "worker 线程未退出"

    view2 = _flow(client, flow2)
    assert view2["state"] == "cancelled"
    # 生成调用被取消围栏拦下：无暂存、无候选
    gen_root = cfg.data_dir / "generation" / pid
    assert not (gen_root / "candidate" / flow2).exists(), "取消后不得发布候选"
    assert not (gen_root / "staging" / flow2).exists(), "取消后不得暂存半包"


# ---------- 恢复端点：INTERRUPTED → 重排阶段 ----------


def test_resume_interrupted_flow(
    client: TestClient, project: dict, migrated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation import flow as flow_store
    from app.generation.contracts import FlowState
    from app.workers.worker import Worker
    from app.core.db import create_db_engine, db_path
    from sqlalchemy.orm import Session

    pid = project["id"]
    provider = ScriptedProvider([
        _contract_json(pending_extra=False),  # extract
        _plan_json(),                          # plan
        _file_json(),                          # generate
    ])
    flow_id = _create_flow(client, pid, provider, monkeypatch)

    worker = Worker()
    worker.run_once()  # extract
    worker.run_once()  # plan → generating，generate 任务排队

    assert _flow(client, flow_id)["state"] == "generating"

    # 模拟宕机：流程转 interrupted
    with Session(create_db_engine(db_path(load_config()))) as s:
        snap = flow_store.get_flow(s, flow_id)
        flow_store.transition_flow(
            s, flow_id, snap.revision, FlowState.INTERRUPTED
        )

    # 恢复：202 + 重排 generate 阶段
    view = _flow(client, flow_id)
    r = client.post(
        f"/api/generation-flows/{flow_id}/resume",
        json={"revision": view["revision"], "idempotency_key": "resume-1"},
        headers=AUTH,
    )
    assert r.status_code == 202, r.text
    assert r.json()["job_id"]

    worker.run_pending()
    assert _flow(client, flow_id)["state"] == "delivered"

"""S4 测试：队列、Worker、取消、真实进程中断恢复、SSE 补取。"""
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.models import Job
from app.services import queue
from app.workers.worker import Worker

AUTH = {"x-session-token": "test-token"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def worker(migrated_db: Path, monkeypatch: pytest.MonkeyPatch) -> Worker:
    """复用 client fixture 已建库；Worker 直接用 run_pending 批量执行。"""
    return Worker()


def _enqueue(session: Session, job_type: str, payload: dict, idempotency_key: str | None = None) -> Job:
    return queue.enqueue(session, job_type, payload, idempotency_key=idempotency_key)


def _session() -> Session:
    cfg = load_config()
    engine = create_db_engine(db_path(cfg))
    return Session(engine)


def test_enqueue_dedup_by_idempotency(client: TestClient) -> None:
    """幂等键去重：同 (type, key) 返回同一任务。"""
    with _session() as s:
        j1 = _enqueue(s, "sleep", {"seconds": 1}, idempotency_key="k1")
        j2 = _enqueue(s, "sleep", {"seconds": 1}, idempotency_key="k1")
        j3 = _enqueue(s, "sleep", {"seconds": 1}, idempotency_key="k2")
        assert j1.id == j2.id
        assert j1.id != j3.id


def test_claim_and_complete(worker: Worker) -> None:
    """领取 → 执行 → 终态 + 事件。"""
    with _session() as s:
        job = _enqueue(s, "sleep", {"seconds": 0.3})
        job_id = job.id
    assert worker.run_once() is True
    with _session() as s:
        done = s.get(Job, job_id)
        assert done.status == "succeeded"
        assert done.attempt == 1
        assert __import__("json").loads(done.result_json)["slept"] == 0.3


def test_unknown_handler_failed(client: TestClient) -> None:
    """未注册类型 → 结构化 failed。"""
    with _session() as s:
        job = _enqueue(s, "no_such_type", {})
        job_id = job.id
    w = Worker()
    w.run_once()
    with _session() as s:
        done = s.get(Job, job_id)
        assert done.status == "failed"
        err = __import__("json").loads(done.error_json)
        assert err["code"] == "unknown_handler"


def test_cancel_cooperative(worker: Worker) -> None:
    """协作取消：标记后 Worker 在边界停止，终态 cancelled。"""
    with _session() as s:
        job = _enqueue(s, "sleep", {"seconds": 5})
        job_id = job.id
    # 立刻置取消（不等长任务跑完）
    import threading

    def cancel_soon() -> None:
        time.sleep(0.5)
        with _session() as s2:
            queue.request_cancel(s2, job.id)

    threading.Thread(target=cancel_soon).start()
    start = time.monotonic()
    worker.run_once()
    elapsed = time.monotonic() - start
    with _session() as s:
        done = s.get(Job, job_id)
        assert done.status == "cancelled"
    assert elapsed < 3, f"取消未及时生效: {elapsed:.1f}s"


def test_version_create_via_worker(client: TestClient, project: dict, worker: Worker) -> None:
    """版本创建走异步任务（S4 改造）：202 → Worker → job.result 带版本视图。"""
    pid = project["id"]
    from tests.test_skills import VALID_SKILL_MD

    r = client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": VALID_SKILL_MD},
        headers=AUTH,
    )
    assert r.status_code == 200
    client.put(
        f"/api/projects/{pid}/skills/draft/files/scripts/extract.py",
        json={"content": "print(1)\n"},
        headers=AUTH,
    )
    r = client.post(f"/api/projects/{pid}/skills/versions", json={"label": "v-async"}, headers=AUTH)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    worker.run_pending()

    r = client.get(f"/api/jobs/{job_id}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "succeeded", body
    assert body["result"]["label"] == "v-async"
    assert {f["path"] for f in body["result"]["files"]} == {"SKILL.md", "scripts/extract.py"}


def test_version_create_conflict_via_worker(client: TestClient, project: dict, worker: Worker) -> None:
    """冲突在 job.error 结构化（异步路径的 409 等价物）。"""
    pid = project["id"]
    from tests.test_skills import VALID_SKILL_MD

    client.put(
        f"/api/projects/{pid}/skills/draft/files/SKILL.md",
        json={"content": VALID_SKILL_MD},
        headers=AUTH,
    )
    client.put(
        f"/api/projects/{pid}/skills/draft/files/scripts/extract.py",
        json={"content": "print(1)\n"},
        headers=AUTH,
    )
    r = client.post(f"/api/projects/{pid}/skills/versions", json={"label": "a"}, headers=AUTH)
    worker.run_pending()
    assert client.get(f"/api/jobs/{r.json()['job_id']}", headers=AUTH).json()["status"] == "succeeded"

    r = client.post(f"/api/projects/{pid}/skills/versions", json={"label": "b"}, headers=AUTH)
    worker.run_pending()
    body = client.get(f"/api/jobs/{r.json()['job_id']}", headers=AUTH).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "conflict"
    assert body["error"]["existing_version_id"]


def test_real_process_kill_recovery(migrated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实杀 Worker 进程 → 重启 → 任务标记 interrupted 且可重试（主§14）。

    必须真实中断进程，不允许只改状态字段。
    """
    with _session() as s:
        job = _enqueue(s, "sleep", {"seconds": 8})
        job_id = job.id

    # 起 Worker 子进程（短租约便于快速过期，但足够领取后保持 running）
    env = {**dict(__import__("os").environ), "SKILLFORGE_LEASE_SECONDS": "5"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.workers.worker"],
        cwd=ROOT,
        env=env,
    )
    # 轮询等它真正领取（S5/S6 后 Worker 导入链变重，固定 sleep 不可靠）
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("Worker 子进程提前退出")
        with _session() as s:
            if s.get(Job, job_id).status == "running":
                break
        time.sleep(0.1)
    else:
        proc.kill()
        raise AssertionError("Worker 15 秒内未领取任务")
    proc.kill()  # 真实杀死（SIGKILL 等价），任务应正处 running
    proc.wait()

    with _session() as s:
        mid = s.get(Job, job_id)
        assert mid.status == "running"  # 数据库仍认为在跑

    # 等租约过期后启动新 Worker：恢复检查应标记 interrupted
    time.sleep(5.5)
    new_worker = Worker()
    interrupted = new_worker.recover()
    assert job_id in interrupted
    with _session() as s:
        done = s.get(Job, job_id)
        assert done.status == "interrupted"
        assert __import__("json").loads(done.error_json)["code"] == "lease_expired"


def test_events_replay(client: TestClient, worker: Worker) -> None:
    """事件补取：按 event_id 单调递增，不丢不重。"""
    with _session() as s:
        _enqueue(s, "sleep", {"seconds": 0.2})
        _enqueue(s, "sleep", {"seconds": 0.2})
    worker.run_pending()

    r = client.get("/api/events?after_id=0", headers=AUTH)
    assert r.status_code == 200
    events = r.json()
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)  # 单调
    types = {e["type"] for e in events}
    assert "job_created" in types
    assert "job_succeeded" in types

    # 增量补取
    mid = ids[len(ids) // 2]
    r = client.get(f"/api/events?after_id={mid}", headers=AUTH)
    assert all(e["id"] > mid for e in r.json())


def test_sse_stream_requires_token(client: TestClient) -> None:
    """SSE 端点鉴权：无令牌 401，查询参数令牌放行。

    流本体（generate 协程）不在此消费——TestClient 的 stream
    消费会阻塞在 keepalive 循环；流逻辑由 events_after 补取测试覆盖，
    真实浏览器消费在 E2E 验证。
    """
    r = client.get("/api/events/stream")
    assert r.status_code == 401

    # 测试内设 1 秒最大存活：流在超时后自我终结，避免 TestClient 假死挂起
    import os

    os.environ["SKILLFORGE_SSE_MAX_SECONDS"] = "1"
    try:
        with client.stream("GET", "/api/events/stream?token=test-token") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            # 消费至流终结（含 keepalive ping）
            for _ in resp.iter_lines():
                pass
    finally:
        os.environ.pop("SKILLFORGE_SSE_MAX_SECONDS", None)


def test_jobs_api_requires_token(client: TestClient) -> None:
    r = client.get("/api/jobs/whatever")
    assert r.status_code == 401

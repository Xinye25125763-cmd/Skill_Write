"""F02 回归：租约内重启不误伤 + 过期任务被周期扫描接管。

缺陷背景：run_forever 只在启动时 recover 一次；Worker 被杀后如果在
租约到期前重启，旧任务当时不会被恢复，之后也没有任何扫描——任务
永久停在 running。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.models import Job
from app.services import queue
from app.workers.worker import Worker

ROOT = Path(__file__).resolve().parents[1]


def _session() -> Session:
    cfg = load_config()
    return Session(create_db_engine(db_path(cfg)))


def test_recover_within_lease_is_noop(migrated_db: Path) -> None:
    """租约未过期时 recover 不动任务（不误伤活跃 Worker）。"""
    with _session() as s:
        job = queue.enqueue(s, "sleep", {"seconds": 30})
        job_id = job.id
    w = Worker()
    w.lease = 60  # 长租约
    with _session() as s:
        claimed = queue.claim(s, "worker-a", w.lease)
        assert claimed is not None and claimed.id == job_id
    # 租约远未过期 → 恢复应为空
    assert w.recover() == []
    with _session() as s:
        assert s.get(Job, job_id).status == "running"


def test_recover_after_expiry_interrupts(migrated_db: Path) -> None:
    """租约过期后 recover 标记 interrupted（既有行为，作为对照）。"""
    with _session() as s:
        job = queue.enqueue(s, "sleep", {"seconds": 30})
        job_id = job.id
        queue.claim(s, "worker-a", 0.2)  # 极短租约
    time.sleep(0.3)
    w = Worker()
    found = w.recover()
    assert job_id in found
    with _session() as s:
        j = s.get(Job, job_id)
        assert j.status == "interrupted"
        assert json.loads(j.error_json)["code"] == "lease_expired"


def test_worker_restart_within_lease_then_periodic_sweep_takes_over(migrated_db: Path) -> None:
    """F02 核心场景（真实子进程）：租约内重启 → 到期后被常驻扫描接管。

    旧行为：重启时 recover 一次（租约未过期，无事发生），
    之后循环不再扫描 → 任务永久 running。
    新行为：常驻循环周期扫描（SKILLFORGE_RECOVER_SECONDS），到期即标记 interrupted。
    """
    env = {
        **dict(os.environ),
        "SKILLFORGE_LEASE_SECONDS": "6",       # 租约 6 秒
        "SKILLFORGE_RECOVER_SECONDS": "1",     # 每秒扫描（验证周期性，不等启动恢复）
        "SKILLFORGE_POLL_SECONDS": "0.2",
    }
    with _session() as s:
        job = queue.enqueue(s, "sleep", {"seconds": 30})
        job_id = job.id

    def spawn() -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "app.workers.worker"],
            cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    w1 = spawn()
    try:
        # 等 worker1 领取（导入开销 1-2 秒 + 轮询）
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with _session() as s:
                if s.get(Job, job_id).status == "running":
                    break
            if w1.poll() is not None:
                pytest.fail("worker1 提前退出")
            time.sleep(0.2)
        else:
            pytest.fail("worker1 未领取任务")

        w1.kill()  # 真实杀死；心跳停止，租约从最后一次 touch 起算 ≤6 秒
        w1.wait()

        # 租约内（约还有 5 秒余量）立即重启 worker2
        w2 = spawn()
        try:
            # 租约未过期窗口内：不应被误标
            time.sleep(1.5)
            with _session() as s:
                assert s.get(Job, job_id).status == "running", "租约内不应被误标 interrupted"

            # 等租约到期 + 周期扫描（≤ recover_interval 1s + 余量）
            deadline = time.monotonic() + 12
            status = "running"
            while time.monotonic() < deadline:
                with _session() as s:
                    status = s.get(Job, job_id).status
                if status == "interrupted":
                    break
                time.sleep(0.3)
            assert status == "interrupted", (
                f"F02 旧行为复现：任务在租约到期后仍停在 {status}（无周期扫描）"
            )
            with _session() as s:
                j = s.get(Job, job_id)
                assert json.loads(j.error_json)["code"] == "lease_expired"
        finally:
            w2.kill()
            w2.wait()
    finally:
        if w1.poll() is None:
            w1.kill()
            w1.wait()

"""Worker 进程：领取 → 执行 → 提交；启动恢复；取消协作。

环境变量（测试用）：
    SKILLFORGE_LEASE_SECONDS      租约时长（默认 60）
    SKILLFORGE_POLL_SECONDS       空闲轮询间隔（默认 0.5）

心跳与租约（细化方案 §4.3）：处理器通过 HandlerContext.touch() 周期续租；
租约过期的 running → interrupted。恢复不只在启动时跑一次（F02）：
常驻循环周期性扫描过期任务——活跃任务有心跳续租不会被误伤，
而被杀 Worker 遗留的任务即使在租约未过期时重启也能在到期后被接管。
"""
import os
import sys
import time
import uuid
import threading
from contextlib import contextmanager

from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.llm import ProviderNotConfigured
from app.models import Job
from app.services import queue
from app.services.skill_files import FileValidationError, VersionConflictError
from app.workers.handlers import JobCancelled, HandlerContext, get_handler
from app.workers.handlers.agent_run import VersionNotFound
from app.workers.handlers.eval_run import SuiteNotFound


class Worker:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.engine = create_db_engine(db_path(self.cfg))
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self.lease = float(os.environ.get("SKILLFORGE_LEASE_SECONDS", "60"))
        self.poll = float(os.environ.get("SKILLFORGE_POLL_SECONDS", "0.5"))
        # F02：周期性扫描过期任务的间隔（取租约时长，即至多迟到一个租约周期接管）
        self.recover_interval = float(os.environ.get("SKILLFORGE_RECOVER_SECONDS", str(self.lease)))

    # ---------- 恢复 ----------

    def recover(self) -> list[str]:
        """启动恢复：租约过期的 running → interrupted。"""
        with Session(self.engine) as session:
            return queue.recover_stale(session)

    # ---------- 执行 ----------

    def run_once(self) -> bool:
        """领取并执行一个任务；无任务返回 False。"""
        with Session(self.engine) as session:
            job = queue.claim(session, self.worker_id, self.lease)
        if job is None:
            return False
        self._execute(job.id, job.type)
        return True

    def run_pending(self) -> int:
        """执行所有排队任务（测试与批处理入口）。"""
        count = 0
        while self.run_once():
            count += 1
        return count

    def run_forever(self) -> None:
        """常驻循环：启动恢复 → 循环领取 + 周期性过期扫描（F02）。"""
        interrupted = self.recover()
        if interrupted:
            print(f"[worker] 恢复检查：{len(interrupted)} 个中断任务", file=sys.stderr)
        last_recover = time.monotonic()
        while True:
            try:
                if not self.run_once():
                    time.sleep(self.poll)
                # F02：无论忙闲都周期性扫描过期任务；
                # 活跃任务靠心跳续租保持 lease_expires_at 在未来，不会被误标
                if time.monotonic() - last_recover >= self.recover_interval:
                    last_recover = time.monotonic()
                    found = self.recover()
                    if found:
                        print(f"[worker] 过期扫描：{len(found)} 个任务标记中断", file=sys.stderr)
            except KeyboardInterrupt:
                print("[worker] 退出", file=sys.stderr)
                return
            except Exception as exc:  # noqa: BLE001 单任务异常不拖垮循环
                print(f"[worker] 任务执行异常: {exc}", file=sys.stderr)
                time.sleep(self.poll)

    @contextmanager
    def _heartbeat(self, job_id: str):
        stop = threading.Event()
        def pulse():
            while not stop.wait(max(0.01, self.lease / 3)):
                try:
                    with Session(self.engine) as heartbeat_session:
                        queue.touch(heartbeat_session, job_id, self.lease, self.worker_id)
                except Exception as exc:
                    print(f"[worker] 心跳失败: {exc}", file=sys.stderr)
        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=6)

    def _execute(self, job_id: str, job_type: str) -> None:
        with self._heartbeat(job_id), Session(self.engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            handler = get_handler(job_type)
            if handler is None:
                queue.fail(session, job_id, {"code": "unknown_handler", "message": f"未注册的任务类型: {job_type}"}, self.worker_id)
                return
            ctx = HandlerContext(session, job_id, self.lease, self.worker_id)
            def fail(error):
                session.rollback()
                queue.fail(session, job_id, error, self.worker_id)
            try:
                result = handler(session, self.cfg, job, ctx)
                queue.complete(session, job_id, result, self.worker_id)
            except JobCancelled:
                session.rollback()
                queue.mark_cancelled(session, job_id, worker_id=self.worker_id)
            except VersionConflictError as exc:
                fail({
                    "code": "conflict",
                    "message": "相同内容版本已存在",
                    "existing_version_id": exc.existing_id,
                })
            except ProviderNotConfigured as exc:
                fail({
                    "code": "provider_not_configured",
                    "message": str(exc),
                })
            except VersionNotFound as exc:
                fail({
                    "code": "not_found",
                    "message": str(exc),
                })
            except SuiteNotFound as exc:
                fail({
                    "code": "not_found",
                    "message": str(exc),
                })
            except FileValidationError as exc:
                error = {"code": "validation_failed", "errors": exc.errors}
                if exc.warnings:
                    error["warnings"] = exc.warnings
                fail(error)
            except Exception as exc:  # noqa: BLE001 未知异常也结构化落库
                fail({
                    "code": type(exc).__name__,
                    "message": str(exc),
                })


def main() -> int:
    worker = Worker()
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

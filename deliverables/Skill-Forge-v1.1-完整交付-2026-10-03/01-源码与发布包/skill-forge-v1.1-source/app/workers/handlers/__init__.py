"""处理器注册表与执行上下文（细化方案 §4.2）。"""
import json

from sqlalchemy.orm import Session

from app.services import queue


class JobCancelled(Exception):
    """处理器主动响应取消。"""


class HandlerContext:
    """处理器与队列设施的交互面：续租、发事件、查取消。"""

    def __init__(self, session: Session, job_id: str, lease_seconds: float, worker_id: str | None = None) -> None:
        self.worker_id = worker_id
        self.session = session
        self.job_id = job_id
        self.lease_seconds = lease_seconds

    def touch(self) -> None:
        """心跳续租：长活处理器应周期调用。"""
        queue.touch(self.session, self.job_id, self.lease_seconds, self.worker_id)

    def event(self, stage: str, message: str = "", progress: dict | None = None) -> None:
        """发进度事件。"""
        queue.append_event(
            self.session, job_id=self.job_id, type_="progress",
            stage=stage, message=message, progress=progress,
        )

    def is_cancelled(self) -> bool:
        return queue.is_cancel_requested(self.session, self.job_id, self.worker_id)


_REGISTRY: dict = {}


def register(job_type: str):
    """注册装饰器。"""
    def decorator(func):  # noqa: ANN202
        _REGISTRY[job_type] = func
        return func
    return decorator


def get_handler(job_type: str):
    return _REGISTRY.get(job_type)


def payload_of(job) -> dict:  # noqa: ANN001
    return json.loads(job.payload_json)


# 导入各处理器完成注册
from app.workers.handlers import version_create as _version_create  # noqa: E402,F401
from app.workers.handlers import sleep as _sleep  # noqa: E402,F401
from app.workers.handlers import agent_run as _agent_run  # noqa: E402,F401
from app.workers.handlers import eval_run as _eval_run  # noqa: E402,F401

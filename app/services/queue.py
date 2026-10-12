"""持久化任务队列：领取、租约、心跳、取消、恢复与事件追加（细化方案 §4）。

纪律（主§9）：
    - 领取用短事务 + 条件更新，即使单写者也保证语义正确；
    - 任务"可能重复执行"，结果提交以先到者为准；
    - cancel 是协作式标记：Worker 在子任务边界检查；
    - 恢复：租约过期的 running 任务标记 interrupted，由用户决定重试。
"""
import json
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.ulid import new_ulid
from app.models import Job, WorkflowEvent

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")


def now() -> float:
    return time.time()


def enqueue(
    session: Session,
    job_type: str,
    payload: dict,
    idempotency_key: str | None = None,
) -> Job:
    """入队；同 (type, idempotency_key) 已存在则返回既有任务（去重不重跑）。"""
    existing = None
    if idempotency_key is not None:
        existing = (
            session.query(Job)
            .filter_by(type=job_type, idempotency_key=idempotency_key)
            .one_or_none()
        )
    if existing is not None:
        return existing

    job = Job(
        id=new_ulid(),
        type=job_type,
        payload_json=json.dumps(payload, ensure_ascii=False),
        status="queued",
        idempotency_key=idempotency_key,
        created_at=now(),
        updated_at=now(),
    )
    session.add(job)
    session.commit()
    append_event(session, job_id=job.id, type_="job_created", stage="queue", message=f"任务入队: {job_type}")
    return job


def claim(session: Session, worker_id: str, lease_seconds: float) -> Job | None:
    """领取一个 queued 任务：条件更新原子置 running（细化方案 §4.3 SQL）。"""
    result = session.execute(
        text(
            """
            UPDATE jobs SET
                status = 'running',
                worker_id = :worker,
                lease_expires_at = :expires,
                heartbeat_at = :now,
                attempt = attempt + 1,
                updated_at = :now
            WHERE id = (
                SELECT id FROM jobs WHERE status = 'queued'
                ORDER BY created_at LIMIT 1
            )
            RETURNING id
            """
        ),
        {"worker": worker_id, "expires": now() + lease_seconds, "now": now()},
    ).scalar_one_or_none()
    if result is None:
        return None
    session.commit()
    return session.get(Job, result)


def touch(session: Session, job_id: str, lease_seconds: float, worker_id: str | None = None) -> None:
    """心跳续租。"""
    session.execute(
        text(
            "UPDATE jobs SET heartbeat_at = :now, lease_expires_at = :now2 + :lease, updated_at = :now3 "
            "WHERE id = :jid AND status = 'running' AND (:owner IS NULL OR worker_id = :owner)"
        ),
        {"now": now(), "now2": now(), "lease": lease_seconds, "now3": now(), "jid": job_id, "owner": worker_id},
    )
    session.commit()


def _finish(session: Session, job_id: str, status: str, result=None, error=None,
            worker_id: str | None = None) -> bool:
    # 条件更新防止 ORM 缓存把 interrupted 或其它终态覆盖成 succeeded。
    changed = session.execute(text("""
        UPDATE jobs SET status=:status, result_json=:result, error_json=:error, updated_at=:now
        WHERE id=:jid AND status IN ('queued', 'running')
        AND (:owner IS NULL OR (status='running' AND worker_id=:owner))
    """), {"status": status, "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
            "error": json.dumps(error, ensure_ascii=False) if error is not None else None,
            "now": now(), "jid": job_id, "owner": worker_id}).rowcount
    if changed:
        session.add(WorkflowEvent(job_id=job_id, type="job_" + status, stage="done",
                                 message="任务" + status, ts=now()))
    session.commit()
    session.expire_all()
    return bool(changed)


def complete(session: Session, job_id: str, result: dict | None = None,
             worker_id: str | None = None) -> None:
    _finish(session, job_id, "succeeded", result=result, worker_id=worker_id)


def fail(session: Session, job_id: str, error: dict, worker_id: str | None = None) -> None:
    _finish(session, job_id, "failed", error=error, worker_id=worker_id)


def mark_cancelled(session: Session, job_id: str, *, error: dict | None = None,
                   worker_id: str | None = None) -> None:
    _finish(session, job_id, "cancelled", error=error, worker_id=worker_id)


def request_cancel(session: Session, job_id: str) -> bool:
    """置取消标记（协作式）：返回任务是否存在。"""
    job = session.get(Job, job_id)
    if job is None:
        return False
    job.cancel_requested = 1
    job.updated_at = now()
    session.commit()
    return True


def is_cancel_requested(session: Session, job_id: str, worker_id: str | None = None) -> bool:
    row = session.execute(text("SELECT status, cancel_requested, worker_id FROM jobs WHERE id=:jid"),
                          {"jid": job_id}).first()
    return row is None or bool(row.cancel_requested) or row.status in TERMINAL_STATUSES or (
        worker_id is not None and (row.status != "running" or row.worker_id != worker_id))


def recover_stale(session: Session) -> list[str]:
    """恢复检查：租约过期的 running 任务标记 interrupted（主§14 恢复语义）。"""
    rows = session.execute(
        text(
            "UPDATE jobs SET status = 'interrupted', updated_at = :now, error_json = :err "
            "WHERE status = 'running' AND lease_expires_at < :now RETURNING id"
        ),
        {"now": now(), "err": json.dumps({"code": "lease_expired", "message": "Worker 心跳丢失，任务中断"})},
    ).scalars().all()
    if rows:
        session.commit()
        for jid in rows:
            append_event(session, job_id=jid, type_="job_failed", stage="recovery", message="租约过期，标记中断")
    return list(rows)


def append_event(
    session: Session,
    job_id: str | None,
    type_: str,
    stage: str,
    message: str = "",
    progress: dict | None = None,
) -> int:
    """追加事件，返回单调 event_id。"""
    event = WorkflowEvent(
        job_id=job_id,
        type=type_,
        stage=stage,
        message=message,
        progress_json=json.dumps(progress, ensure_ascii=False) if progress else None,
        ts=now(),
    )
    session.add(event)
    session.commit()
    return event.event_id


def events_after(session: Session, after_id: int, limit: int = 100) -> list[dict]:
    """断线补取：event_id 单调递增（细化方案 §3.3）。"""
    rows = (
        session.query(WorkflowEvent)
        .filter(WorkflowEvent.event_id > after_id)
        .order_by(WorkflowEvent.event_id)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.event_id,
            "job_id": r.job_id,
            "type": r.type,
            "stage": r.stage,
            "message": r.message,
            "progress": json.loads(r.progress_json) if r.progress_json else None,
            "ts": r.ts,
        }
        for r in rows
    ]


def job_view(job: Job) -> dict:
    """Job → API 视图。"""
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "cancel_requested": bool(job.cancel_requested),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "result": json.loads(job.result_json) if job.result_json else None,
        "error": json.loads(job.error_json) if job.error_json else None,
    }

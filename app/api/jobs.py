"""任务与事件路由：任务查询/取消、事件补取、SSE 流（细化方案 §3.2 / §3.3）。"""
import asyncio
import json
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.projects import get_session
from app.models import Job
from app.services import queue

router = APIRouter(prefix="/api")


@router.get("/jobs/{job_id}")
def get_job(job_id: str, session: Session = Depends(get_session)) -> dict:
    job = session.get(Job, job_id)
    if job is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return queue.job_view(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, session: Session = Depends(get_session)) -> dict:
    """置取消标记（协作式）：已终态任务无效果。

    上游已产生的模型调用费用不撤回（主§9），由前端提示。
    """
    if not queue.request_cancel(session, job_id):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail={"code": "not_found"})
    job = session.get(Job, job_id)
    return queue.job_view(job)


@router.get("/events")
def list_events(after_id: int = 0, limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    """断线补取：按 event_id 单调递增返回（细化方案 §3.3 兜底通道）。"""
    return queue.events_after(session, after_id, min(limit, 1000))


@router.get("/events/stream")
async def event_stream(request: Request, session: Session = Depends(get_session)) -> StreamingResponse:
    """SSE 事件流：Last-Event-ID 接续 + keepalive。

    EventSource 无法自定义请求头，令牌经查询参数传递（中间件对
    本端点放行 query token）；Origin 校验仍然生效。
    """
    media_type = "text/event-stream"
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    async def generate():  # noqa: ANN202
        import time as _time

        max_lifetime = float(os.environ.get("SKILLFORGE_SSE_MAX_SECONDS", "3600"))
        deadline = _time.monotonic() + max_lifetime
        last_id = int(request.headers.get("last-event-id", "0"))
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            if _time.monotonic() > deadline:
                return  # 最大存活保护：客户端假死时服务端自我终结
            events = queue.events_after(session, last_id, limit=100)
            for ev in events:
                data = {
                    "job_id": ev["job_id"],
                    "type": ev["type"],
                    "stage": ev["stage"],
                    "message": ev["message"],
                    "progress": ev["progress"],
                    "ts": ev["ts"],
                }
                yield f"event: {ev['type']}\nid: {ev['id']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                last_id = ev["id"]
            if not events:
                yield ": ping\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(generate(), media_type=media_type, headers=headers)

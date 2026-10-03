"""agent 运行路由（细化方案 §3.2 的 S5 子集）。

- POST /api/projects/{id}/runs：入队 agent_run（202 + job_id）
- GET  /api/projects/{id}/runs：运行记录列表
- GET  /api/runs/{run_id}：单条运行记录（含轨迹与沙箱文件）
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.projects import get_session
from app.models import AgentRun, Project
from app.services import queue

router = APIRouter(prefix="/api")


class RunCreate(BaseModel):
    """创建运行请求。version_id 省略 = 无技能基线。"""

    prompt: str = Field(min_length=1, max_length=100_000)
    version_id: str | None = None
    max_tokens_total: int | None = Field(default=None, ge=100, le=10_000_000)
    max_rounds: int | None = Field(default=None, ge=1, le=200)


def _run_view(run: AgentRun) -> dict:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "version_id": run.version_id,
        "prompt": run.prompt,
        "status": run.status,
        "exit_reason": run.exit_reason,
        "final_message": run.final_message,
        "steps": json.loads(run.steps_json),
        "usage": {
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
        },
        "error_detail": run.error_detail,
        "sandbox_files": json.loads(run.sandbox_files_json),
        "budget": json.loads(run.budget_json),
        "sandbox_dir": run.sandbox_dir,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


@router.post("/projects/{project_id}/runs", status_code=202)
def create_run(project_id: str, body: RunCreate, session: Session = Depends(get_session)) -> dict:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "项目不存在"})
    payload = {"project_id": project_id, "prompt": body.prompt}
    if body.version_id is not None:
        payload["version_id"] = body.version_id
    if body.max_tokens_total is not None:
        payload["max_tokens_total"] = body.max_tokens_total
    if body.max_rounds is not None:
        payload["max_rounds"] = body.max_rounds
    job = queue.enqueue(session, "agent_run", payload)
    return {"job_id": job.id, "status": job.status}


@router.get("/projects/{project_id}/runs")
def list_runs(project_id: str, session: Session = Depends(get_session)) -> list[dict]:
    runs = (
        session.query(AgentRun)
        .filter_by(project_id=project_id)
        .order_by(AgentRun.created_at.desc())
        .limit(100)
        .all()
    )
    return [_run_view(r) for r in runs]


@router.get("/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(get_session)) -> dict:
    run = session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "运行不存在"})
    return _run_view(run)

"""评测路由（细化方案 §3.2 的 S6 子集）：测试集 + 对照运行。

- POST /api/projects/{id}/suites：创建测试集（同步，校验用例与断言结构）
- GET  /api/projects/{id}/suites：测试集列表
- POST /api/projects/{id}/eval-runs：入队对照评测（202 + job_id）
- GET  /api/eval-runs/{id}：状态、逐例结果、汇总
- GET  /api/projects/{id}/eval-runs：运行列表
"""
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.projects import get_session
from app.core.ulid import new_ulid
from app.models import EvalRun, Project, Suite
from app.services import queue

router = APIRouter(prefix="/api")

ASSERTION_TYPES = {"file_exists", "file_contains", "text_contains", "exit_reason_is"}


class AssertionSpec(BaseModel):
    """程序断言（最小版四种类型）。"""

    type: str
    path: str | None = None
    contains: str | None = None
    value: str | None = None


class CaseSpec(BaseModel):
    """测试用例。"""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str | None = Field(default=None, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    assertions: list[AssertionSpec] = Field(default_factory=list)
    repeats: int = Field(default=1, ge=1, le=10)  # v1.4 R2：方差分析重复次数（1=现行为）
    fixtures: dict[str, Any] | None = None  # 生成链内建套件携带的共享 fixtures
    budget: dict[str, Any] | None = None  # 生成链内建套件携带的预算限制


class SuiteCreate(BaseModel):
    """创建测试集请求。"""

    name: str = Field(min_length=1, max_length=200)
    cases: list[CaseSpec] = Field(min_length=1, max_length=100)


class EvalRunCreate(BaseModel):
    """创建对照评测请求。"""

    suite_id: str
    version_id: str
    # Separate one candidate/baseline run ceiling from the aggregate suite cap.
    max_tokens_per_run: int | None = Field(default=None, ge=100, le=10_000_000)
    max_tokens_total: int | None = Field(default=None, ge=100, le=100_000_000)
    # Compatibility alias used by early v1.5 implementation snapshots.
    suite_max_tokens_total: int | None = Field(default=None, ge=100, le=100_000_000)
    max_rounds: int | None = Field(default=None, ge=1, le=200)


def _suite_view(s: Suite, with_cases: bool = True) -> dict:
    view = {
        "id": s.id,
        "project_id": s.project_id,
        "name": s.name,
        "created_at": s.created_at,
    }
    if with_cases:
        view["cases"] = json.loads(s.cases_json)
    return view


@router.post("/projects/{project_id}/suites", status_code=201)
def create_suite(project_id: str, body: SuiteCreate, session: Session = Depends(get_session)) -> dict:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "项目不存在"})

    errors: list[str] = []
    seen_ids: set[str] = set()
    for i, case in enumerate(body.cases):
        if case.id in seen_ids:
            errors.append(f"用例 ID 重复: {case.id}（第 {i + 1} 个）")
        seen_ids.add(case.id)
        for j, a in enumerate(case.assertions):
            if a.type not in ASSERTION_TYPES:
                errors.append(f"用例 {case.id} 第 {j + 1} 条断言类型未知: {a.type}")
            elif a.type == "file_exists" and not a.path:
                errors.append(f"用例 {case.id} 断言 file_exists 缺 path")
            elif a.type == "file_contains" and (not a.path or a.contains is None):
                errors.append(f"用例 {case.id} 断言 file_contains 缺 path/contains")
            elif a.type == "text_contains" and a.contains is None:
                errors.append(f"用例 {case.id} 断言 text_contains 缺 contains")
            elif a.type == "exit_reason_is" and not a.value:
                errors.append(f"用例 {case.id} 断言 exit_reason_is 缺 value")
    if errors:
        raise HTTPException(status_code=422, detail={"code": "validation_failed", "errors": errors})

    suite = Suite(
        id=new_ulid(),
        project_id=project_id,
        name=body.name,
        cases_json=json.dumps(
            [c.model_dump() for c in body.cases], ensure_ascii=False
        ),
        created_at=time.time(),
    )
    session.add(suite)
    session.commit()
    return _suite_view(suite)


@router.get("/projects/{project_id}/suites")
def list_suites(project_id: str, session: Session = Depends(get_session)) -> list[dict]:
    suites = (
        session.query(Suite)
        .filter_by(project_id=project_id)
        .order_by(Suite.created_at.desc())
        .limit(100)
        .all()
    )
    return [_suite_view(s, with_cases=False) for s in suites]


@router.get("/suites/{suite_id}")
def get_suite(suite_id: str, session: Session = Depends(get_session)) -> dict:
    suite = session.get(Suite, suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "测试集不存在"})
    return _suite_view(suite)


@router.post("/projects/{project_id}/eval-runs", status_code=202)
def create_eval_run(project_id: str, body: EvalRunCreate, session: Session = Depends(get_session)) -> dict:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "项目不存在"})
    suite = session.get(Suite, body.suite_id)
    if suite is None or suite.project_id != project_id:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "测试集不存在"})
    payload = {
        "project_id": project_id,
        "suite_id": body.suite_id,
        "version_id": body.version_id,
    }
    if body.max_tokens_per_run is not None:
        payload["max_tokens_per_run"] = body.max_tokens_per_run
    total_cap = (body.max_tokens_total if body.max_tokens_total is not None
                 else body.suite_max_tokens_total)
    if total_cap is not None:
        payload["max_tokens_total"] = total_cap
    if body.max_rounds is not None:
        payload["max_rounds"] = body.max_rounds
    job = queue.enqueue(session, "eval_run", payload)
    return {"job_id": job.id, "status": job.status}


@router.get("/eval-runs/{eval_run_id}")
def get_eval_run(eval_run_id: str, session: Session = Depends(get_session)) -> dict:
    from app.workers.handlers.eval_run import eval_run_view

    ev = session.get(EvalRun, eval_run_id)
    if ev is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "评测运行不存在"})
    return eval_run_view(session, ev)


@router.get("/projects/{project_id}/eval-runs")
def list_eval_runs(project_id: str, session: Session = Depends(get_session)) -> list[dict]:
    evs = (
        session.query(EvalRun)
        .filter_by(project_id=project_id)
        .order_by(EvalRun.created_at.desc())
        .limit(50)
        .all()
    )
    from app.workers.handlers.eval_run import eval_run_view

    return [eval_run_view(session, ev) for ev in evs]

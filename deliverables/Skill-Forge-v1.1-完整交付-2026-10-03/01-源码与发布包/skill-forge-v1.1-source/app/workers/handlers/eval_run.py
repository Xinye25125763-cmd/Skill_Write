"""eval_run 处理器（S6）：候选版本 vs 无技能基线，逐用例执行 + 程序断言评分。

任务语义：
- 基础设施失败（套件/版本不存在、未配置提供商）→ job failed；
- 单用例执行失败（exit_reason != completed）→ exec_status=exec_failed，
  该例不评分（score_status=not_scored）——与评分失败严格分离；
- 评分步骤自身失败（断言结构非法）→ score_status=score_failed，不影响执行状态；
- 协作取消：未执行的用例记 skipped，已执行的保留结果。
"""
import json
import time

from app.core.ulid import new_ulid
from app.llm import ProviderNotConfigured
from app.models import EvalCaseResult, EvalRun, Suite
from app.workers.handlers import HandlerContext, payload_of, register
from app.workers.handlers.agent_run import execute_prompt

GROUPS = ("candidate", "baseline")

# 基础设施类异常：job 级失败（不属单例兑底范围）
INFRA_ERRORS = (ProviderNotConfigured,)
try:  # VersionNotFound 在 agent_run 模块内定义
    from app.workers.handlers.agent_run import VersionNotFound

    INFRA_ERRORS = (ProviderNotConfigured, VersionNotFound)
except ImportError:  # pragma: no cover
    pass


class SuiteNotFound(Exception):
    """payload 指定的套件不存在。"""


def _fail_eval_run(session, ev: EvalRun) -> None:  # noqa: ANN001
    """job 级失败前给 EvalRun 行收尾（不留 running，F08）。"""
    ev.status = "failed"
    ev.finished_at = time.time()
    session.commit()


def eval_run_view(session, ev: EvalRun) -> dict:  # noqa: ANN001
    """EvalRun 行 → API 视图（含逐例结果与汇总）。"""
    cases = (
        session.query(EvalCaseResult)
        .filter_by(eval_run_id=ev.id)
        .order_by(EvalCaseResult.created_at)
        .all()
    )
    case_results = [
        {
            "id": c.id,
            "case_id": c.case_id,
            "group": c.group_name,
            "agent_run_id": c.agent_run_id,
            "exec_status": c.exec_status,
            "exit_reason": c.exit_reason,
            "score_status": c.score_status,
            "assertions": json.loads(c.assertions_json),
            "passed_count": c.passed_count,
            "total_count": c.total_count,
            "score_error": c.score_error,
        }
        for c in cases
    ]
    summary: dict[str, dict] = {}
    for group in GROUPS:
        group_cases = [c for c in case_results if c["group"] == group]
        scored = [c for c in group_cases if c["score_status"] == "scored"]
        summary[group] = {
            "cases": len(group_cases),
            "executed": sum(1 for c in group_cases if c["exec_status"] == "executed"),
            "exec_failed": sum(1 for c in group_cases if c["exec_status"] == "exec_failed"),
            "skipped": sum(1 for c in group_cases if c["exec_status"] == "skipped"),
            "scored": len(scored),
            "score_failed": sum(1 for c in group_cases if c["score_status"] == "score_failed"),
            "passed_total": sum(c["passed_count"] for c in scored),
            "assertions_total": sum(c["total_count"] for c in scored),
        }
    return {
        "id": ev.id,
        "project_id": ev.project_id,
        "suite_id": ev.suite_id,
        "version_id": ev.version_id,
        "status": ev.status,
        "error": json.loads(ev.error_json) if ev.error_json else None,
        "summary": summary,
        "case_results": case_results,
        "created_at": ev.created_at,
        "finished_at": ev.finished_at,
    }


@register("eval_run")
def handle(session, cfg, job, ctx: HandlerContext) -> dict:  # noqa: ANN001
    from app.services.scoring import ScoringError, score_assertions

    payload = payload_of(job)
    project_id = payload["project_id"]
    suite_id = payload["suite_id"]
    version_id = payload.get("version_id")
    max_tokens_total = payload.get("max_tokens_total")
    max_rounds = payload.get("max_rounds")

    suite = session.get(Suite, suite_id)
    if suite is None or suite.project_id != project_id:
        raise SuiteNotFound(f"套件不存在: {suite_id}")
    cases = json.loads(suite.cases_json)

    ev = EvalRun(
        id=new_ulid(),
        project_id=project_id,
        suite_id=suite_id,
        version_id=version_id,
        status="running",
        created_at=time.time(),
    )
    session.add(ev)
    session.commit()

    ctx.event("start", f"对照评测：{len(cases)} 用例 × {len(GROUPS)} 组")

    total = len(cases) * len(GROUPS)
    done = 0
    for case in cases:
        case_id = str(case.get("id") or "")
        prompt = str(case.get("prompt") or "")
        assertions = case.get("assertions", [])
        for group in GROUPS:
            done += 1
            if ctx.is_cancelled():
                session.add(EvalCaseResult(
                    id=new_ulid(),
                    eval_run_id=ev.id,
                    case_id=case_id,
                    group_name=group,
                    exec_status="skipped",
                    score_status="not_scored",
                    created_at=time.time(),
                ))
                session.commit()
                continue

            ctx.touch()
            ctx.event(
                "case",
                f"用例 {case_id} · {group}（{done}/{total}）",
                progress={"case_id": case_id, "group": group, "done": done, "total": total},
            )

            row = EvalCaseResult(
                id=new_ulid(),
                eval_run_id=ev.id,
                case_id=case_id,
                group_name=group,
                exec_status="executed",
                score_status="not_scored",
                created_at=time.time(),
            )

            # 执行（candidate=候选版本；baseline=无技能）。
            # F08④：单例兑底——基础设施类异常（版本不存在/未配置 provider）
            # 上抛为 job 级失败（带 EvalRun 收尾）；其余意外异常记为该例
            # exec_failed，不拖垮余下用例，评测仍正常完成
            try:
                run = execute_prompt(
                    session,
                    cfg,
                    ctx,
                    project_id=project_id,
                    version_id=version_id if group == "candidate" else None,
                    prompt=prompt,
                    max_tokens_total=max_tokens_total,
                    max_rounds=max_rounds,
                )
            except INFRA_ERRORS:
                _fail_eval_run(session, ev)
                raise
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                row.exec_status = "exec_failed"
                row.exit_reason = "model_failed"
                row.score_error = f"用例执行异常: {type(exc).__name__}: {exc}"
                session.add(row)
                session.commit()
                continue
            row.agent_run_id = run.id
            row.exit_reason = run.exit_reason

            if run.exit_reason != "completed":
                # 执行失败：不评分（与评分失败分离）
                row.exec_status = "exec_failed"
            else:
                sandbox_files = json.loads(run.sandbox_files_json)
                try:
                    results = score_assertions(
                        assertions,
                        sandbox_files=sandbox_files,
                        final_message=run.final_message,
                        exit_reason=run.exit_reason,
                    )
                    row.score_status = "scored"
                    row.assertions_json = json.dumps(results, ensure_ascii=False)
                    row.passed_count = sum(1 for r in results if r["passed"])
                    row.total_count = len(results)
                except ScoringError as exc:
                    row.score_status = "score_failed"
                    row.score_error = str(exc)
                except Exception as exc:  # noqa: BLE001 F08④：评分意外异常也只影响该例
                    session.rollback()
                    row.score_status = "score_failed"
                    row.score_error = f"评分意外异常: {type(exc).__name__}: {exc}"

            session.add(row)
            session.commit()

    ev.status = "finished"
    ev.finished_at = time.time()
    session.commit()

    ctx.event("finished", "评测完成")
    session.refresh(ev)
    return eval_run_view(session, ev)

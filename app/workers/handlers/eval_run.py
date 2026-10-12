"""eval_run 处理器（S6）：候选版本 vs 无技能基线，逐用例执行 + 程序断言评分。

任务语义：
- 基础设施失败（套件/版本不存在、未配置提供商）→ job failed；
- 单用例执行失败（exit_reason != completed）→ exec_status=exec_failed，
  该例不评分（score_status=not_scored）——与评分失败严格分离；
- 评分步骤自身失败（断言结构非法）→ score_status=score_failed，不影响执行状态；
- 协作取消：未执行的用例记 skipped，已执行的保留结果；
- v1.4 R2：用例支持 repeats（默认 1=现行为），逐 attempt 执行并落 attempt 序号；
  汇总含方差统计（多数决/flaky/组级 pass_rate±stdev，纯计算层 variance）；
- v1.4 R3：双侧输出齐全的用例追加盲评对照（A/B 随机匿名化 + 盲态评审
  + 归因引用抽查），偏好分布入 meta_json 与结果视图，导出折算 eval:blind。
"""
import json
import random
import time

from app.core.ulid import new_ulid
from app.generation import blind as blind_mod
from app.generation import budget as budget_svc
from app.generation import variance as variance_mod
from app.llm import ProviderNotConfigured, judge_provider_from_env, provider_from_env
from app.models import EvalCaseResult, EvalRun, Suite
from app.workers.handlers import HandlerContext, payload_of, register
from app.workers.handlers.agent_run import execute_prompt

GROUPS = ("candidate", "baseline")

# v1.4 R3 起：盲评评审者工厂（测试可替换）。v1.5 M5：默认优先读
# SKILLFORGE_JUDGE_LLM_*（未配置回落主模型）——裁判调用可配分级模型
judge_provider_factory = judge_provider_from_env


class _UsageTrackingProvider:
    """Track every judge call, including successful calls with malformed replies."""

    def __init__(self, provider) -> None:  # noqa: ANN001
        self.provider = provider
        self.capabilities = provider.capabilities
        self.calls = 0
        self.usage_calls = 0
        self.usage_missing_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        try:
            response = self.provider.complete(messages, **kwargs)
        except Exception:
            self.usage_missing_calls += 1
            raise
        usage = getattr(response, "usage", None)
        if usage is None:
            self.usage_missing_calls += 1
        else:
            self.usage_calls += 1
            self.prompt_tokens += int(usage.prompt_tokens or 0)
            self.completion_tokens += int(usage.completion_tokens or 0)
        return response

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
            "attempt": c.attempt or 1,
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

    # v1.4 R2：方差统计（多数决/flaky/组级 pass_rate±stdev/delta）
    var_rows = [
        {"group_name": c["group"], "case_id": c["case_id"],
         "exec_status": c["exec_status"], "exit_reason": c["exit_reason"],
         "score_status": c["score_status"],
         "passed_count": c["passed_count"], "total_count": c["total_count"]}
        for c in case_results
    ]
    stats = variance_mod.group_case_stats(var_rows)
    var_summary = variance_mod.group_summary(stats)
    variance = {
        "groups": var_summary,
        "delta_pass_rate": variance_mod.delta(var_summary),
        "flaky_cases": {
            g: sorted(cid for cid, c in stats.get(g, {}).items() if c["flaky"])
            for g in GROUPS
        },
        "non_discriminating": variance_mod.non_discriminating(stats),
    }

    # v1.5 M4：成本可见性（执行侧真实账单：关联 agent_runs 累计）
    from app.models import AgentRun
    run_ids = [c["agent_run_id"] for c in case_results if c["agent_run_id"]]
    cost = {"total_tokens": 0, "candidate_tokens": 0,
            "baseline_tokens": 0, "judge_tokens": 0}
    if run_ids:
        runs = session.query(AgentRun).filter(AgentRun.id.in_(run_ids)).all()
        cost["candidate_tokens"] = sum(
            (r.prompt_tokens or 0) + (r.completion_tokens or 0)
            for r in runs if r.version_id is not None)
        cost["baseline_tokens"] = sum(
            (r.prompt_tokens or 0) + (r.completion_tokens or 0)
            for r in runs if r.version_id is None)
    judge_meta = (_blind_of(ev) or {}).get("usage") or {}
    cost["judge_tokens"] = int(judge_meta.get("prompt_tokens") or 0) + int(
        judge_meta.get("completion_tokens") or 0)
    cost["total_tokens"] = (
        cost["candidate_tokens"] + cost["baseline_tokens"] + cost["judge_tokens"])

    return {
        "id": ev.id,
        "project_id": ev.project_id,
        "suite_id": ev.suite_id,
        "version_id": ev.version_id,
        "status": ev.status,
        "error": json.loads(ev.error_json) if ev.error_json else None,
        "summary": summary,
        "variance": variance,
        "cost": cost,
        "budget": _budget_of(ev),
        "blind": _blind_of(ev),
        "case_results": case_results,
        "created_at": ev.created_at,
        "finished_at": ev.finished_at,
    }


def _resolve_budget_limits(cases: list[dict], payload: dict) -> tuple[int | None, int | None, list[int | None]]:
    """Resolve per-run and suite ceilings with explicit > case > suite precedence."""
    suite_budget = next(
        (c.get("budget") or {} for c in cases if isinstance(c.get("budget"), dict)),
        {},
    )
    explicit_per_run = payload.get("max_tokens_per_run")
    # max_tokens_total 是路线图规定的整套评测累计上限；
    # suite_max_tokens_total 保留为早期实现版本的兼容别名。
    suite_cap = payload.get("max_tokens_total")
    if suite_cap is None:
        suite_cap = payload.get("suite_max_tokens_total")
    if suite_cap is None:
        declared = suite_budget.get("max_tokens_total")
        suite_cap = int(declared) if declared is not None else None

    limits: list[int | None] = []
    for case in cases:
        case_budget = case.get("budget") if isinstance(case.get("budget"), dict) else {}
        case_value = case_budget.get("max_tokens_per_case")
        suite_value = suite_budget.get("max_tokens_per_case")
        value = explicit_per_run
        if value is None:
            value = case_value if case_value is not None else suite_value
        limits.append(int(value) if value is not None else None)
    return (int(explicit_per_run) if explicit_per_run is not None else None,
            int(suite_cap) if suite_cap is not None else None, limits)


def _budget_of(ev: EvalRun) -> dict | None:
    if not ev.meta_json:
        return None
    try:
        meta = json.loads(ev.meta_json)
    except (json.JSONDecodeError, TypeError):
        return None
    budget = meta.get("budget")
    return budget if isinstance(budget, dict) else None


def _blind_of(ev: EvalRun) -> dict | None:
    """meta_json 里的盲评段（无则 None；trigger 类运行无此段）。"""
    if not ev.meta_json:
        return None
    try:
        meta = json.loads(ev.meta_json)
    except (json.JSONDecodeError, TypeError):
        return None
    blind = meta.get("blind")
    return blind if isinstance(blind, dict) else None


@register("eval_run")
def handle(session, cfg, job, ctx: HandlerContext) -> dict:  # noqa: ANN001
    from app.services.scoring import ScoringError, score_assertions

    payload = payload_of(job)
    project_id = payload["project_id"]
    suite_id = payload["suite_id"]
    version_id = payload.get("version_id")
    max_rounds = payload.get("max_rounds")

    suite = session.get(Suite, suite_id)
    if suite is None or suite.project_id != project_id:
        raise SuiteNotFound(f"套件不存在: {suite_id}")
    cases = json.loads(suite.cases_json)

    # 预算优先级：API 显式值 > 用例声明 > 套件声明 > 执行器默认。
    # judge 在候选/基线执行循环之后运行，单独计量，不消耗 suite cap。
    max_tokens_per_run, suite_total_cap, case_limits = _resolve_budget_limits(cases, payload)

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

    # v1.4 R3：收集双侧完成的最终输出（盲评输入；映射只入库不进提示）
    outputs: dict[str, dict[str, str]] = {}
    prompts: dict[str, str] = {}

    # v1.4 R2：用例级 repeats（套件内声明；缺省 1=现行为）
    repeats_of = {str(c.get("id") or ""): max(1, int(c.get("repeats") or 1))
                  for c in cases}
    total = sum(repeats_of.values()) * len(GROUPS)
    done = 0
    used_total_tokens = 0  # suite cap 计账：实测 tokens + 未知 usage 的保守预留
    known_total_tokens = 0
    unknown_usage_estimated_tokens = 0
    usage_missing_calls = 0
    suite_cap_hit = False
    for case_index, case in enumerate(cases):
        case_id = str(case.get("id") or "")
        prompt = str(case.get("prompt") or "")
        assertions = case.get("assertions", [])
        # v1.5 M1：逐例预算——API 显式 > 用例/套件声明 > 执行器默认。
        # 未显式传且套件声明了 per-case 限制 → 采用之。
        case_limit = case_limits[case_index]
        for group in GROUPS:
            for attempt_no in range(1, repeats_of.get(case_id, 1) + 1):
                done += 1
                # v1.5 M1：套件整体封顶——累计消耗达到声明上限后，
                # 余下用例不再执行（如实标 skipped，不粉饰也不浪费）
                if suite_total_cap is not None and used_total_tokens >= suite_total_cap:
                    suite_cap_hit = True
                    session.add(EvalCaseResult(
                        id=new_ulid(),
                        eval_run_id=ev.id,
                        case_id=case_id,
                        group_name=group,
                        attempt=attempt_no,
                        exec_status="skipped",
                        score_status="not_scored",
                        score_error=(
                            f"套件整体 token 封顶已达（{used_total_tokens}"
                            f"/{suite_total_cap}），本例未执行"),
                        created_at=time.time(),
                    ))
                    session.commit()
                    continue
                if ctx.is_cancelled():
                    session.add(EvalCaseResult(
                        id=new_ulid(),
                        eval_run_id=ev.id,
                        case_id=case_id,
                        group_name=group,
                        attempt=attempt_no,
                        exec_status="skipped",
                        score_status="not_scored",
                        created_at=time.time(),
                    ))
                    session.commit()
                    continue

                ctx.touch()
                ctx.event(
                    "case",
                    f"用例 {case_id} · {group} · 第{attempt_no}次（{done}/{total}）",
                    progress={"case_id": case_id, "group": group,
                              "attempt": attempt_no, "done": done, "total": total},
                )

                row = EvalCaseResult(
                    id=new_ulid(),
                    eval_run_id=ev.id,
                    case_id=case_id,
                    group_name=group,
                    attempt=attempt_no,
                    exec_status="executed",
                    score_status="not_scored",
                    created_at=time.time(),
                )

                # 执行（candidate=候选版本；baseline=无技能）。
                # F08④：单例兑底——基础设施类异常（版本不存在/未配置 provider）
                # 上抛为 job 级失败（带 EvalRun 收尾）；其余意外异常记为该例
                # exec_failed，不拖垮余下用例，评测仍正常完成
                # The aggregate cap also constrains the next individual run to
                # its remaining allowance, so a suite cap is not merely a
                # post-hoc skip threshold. A single in-flight provider response
                # can still report prompt-token overrun; it is recorded below.
                effective_case_limit = case_limit
                if suite_total_cap is not None:
                    remaining_suite_tokens = max(1, suite_total_cap - used_total_tokens)
                    effective_case_limit = (remaining_suite_tokens if case_limit is None
                                            else min(case_limit, remaining_suite_tokens))
                try:
                    run = execute_prompt(
                        session,
                        cfg,
                        ctx,
                        project_id=project_id,
                        version_id=version_id if group == "candidate" else None,
                        prompt=prompt,
                        max_tokens_total=effective_case_limit,
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
                # v1.5 M1：累计实际消耗（预算封顶用；失败也算已花的钱）
                try:
                    run_budget = json.loads(run.budget_json or "{}")
                except (json.JSONDecodeError, TypeError, ValueError):
                    run_budget = {}
                known_tokens = int(run_budget.get("used_tokens") or
                                   ((run.prompt_tokens or 0) + (run.completion_tokens or 0)))
                estimated_tokens = int(run_budget.get("unknown_usage_estimated_tokens") or 0)
                known_total_tokens += known_tokens
                unknown_usage_estimated_tokens += estimated_tokens
                usage_missing_calls += int(run_budget.get("usage_missing_calls") or 0)
                used_total_tokens += known_tokens + estimated_tokens
                if run.exit_reason == "completed" and run.final_message:
                    outputs.setdefault(case_id, {})[group] = run.final_message
                    prompts[case_id] = prompt

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

    # v1.4 R3：盲评对照（双侧输出齐全的用例；每例 +1 次评审调用）
    # 单例失败（回复不合 JSON/归因引用不实/评审异常）→ 该例记 invalid，
    # 不拖垮整组（与程序评分失败同语义）；评审者未配置 → 整段如实跳过。
    # 预算：评审调用全进台账（stage_id=eval-blind，预留+实际双写；
    # 生成链内建评测才有关联 flow，对话入口发起的评测无 flow 不记）
    blind_verdicts: list[dict] = []
    blind_skipped: str | None = None
    judge_meta_total: dict[str, Any] = {
        "model_calls": 0,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "usage_observed": {"prompt_tokens": 0, "completion_tokens": 0},
        "usage_calls": 0,
        "usage_missing_calls": 0,
    }
    try:
        judge = _UsageTrackingProvider(judge_provider_factory())
    except ProviderNotConfigured as exc:
        judge = None
        blind_skipped = f"评审者未配置（{type(exc).__name__}）"
    if judge is not None:
        # 生成链内建评测 → 盲评调用入预算台账（版本行携带生成流程 ID；
        # 对话入口发起的评测无关联 flow → 不记，调用数仍入 meta 可见）
        flow_id = None
        if version_id:
            from app.models import SkillVersion
            vrow = session.get(SkillVersion, version_id)
            flow_id = vrow.generation_flow_id if vrow is not None else None
        rng = random.Random()
        for case in cases:
            case_id = str(case.get("id") or "")
            pair = outputs.get(case_id) or {}
            cand_msg = pair.get("candidate")
            base_msg = pair.get("baseline")
            if not (cand_msg and base_msg):
                continue  # 双侧输出不齐全 → 不盲评（不粉饰）
            text_a, text_b, cand_is_a = blind_mod.blind_pair(rng, cand_msg, base_msg)
            verdict: dict = {
                "case_id": case_id, "candidate_is_a": cand_is_a,
            }
            try:
                v, jmeta = blind_mod.judge_blind(
                    judge, prompts.get(case_id, ""), text_a, text_b,
                )
                verdict.update(v)
                verdict["attribution_ok"] = blind_mod.verify_attribution(
                    v["attribution"], text_a, text_b, v["preference"],
                )
            except Exception as exc:  # noqa: BLE001 单例兑底
                verdict.update({"preference": None, "attribution": "",
                                "attribution_ok": False,
                                "error": f"{type(exc).__name__}: {exc}"})
            blind_verdicts.append(verdict)
    if judge is not None:
        judge_meta_total.update({
            "model_calls": judge.calls,
            "usage": ({"prompt_tokens": judge.prompt_tokens,
                       "completion_tokens": judge.completion_tokens}
                      if judge.usage_calls and not judge.usage_missing_calls else None),
            "usage_observed": {"prompt_tokens": judge.prompt_tokens,
                               "completion_tokens": judge.completion_tokens},
            "unknown_usage_estimated_tokens": 0,
            "usage_calls": judge.usage_calls,
            "usage_missing_calls": judge.usage_missing_calls,
        })
    blind_meta = {
        "verdicts": blind_verdicts,
        "summary": blind_mod.blind_summary(blind_verdicts),
        "model_calls": judge_meta_total["model_calls"],
        "usage": (judge_meta_total["usage"]
                  if judge_meta_total["usage_calls"] else None),
        "usage_observed": ({
            "prompt_tokens": judge.prompt_tokens,
            "completion_tokens": judge.completion_tokens,
        } if judge is not None else {"prompt_tokens": 0, "completion_tokens": 0}),
        "usage_calls": judge_meta_total["usage_calls"],
        "usage_missing_calls": judge_meta_total["usage_missing_calls"],
    }
    if blind_skipped:
        blind_meta["skipped"] = blind_skipped
    judge_attempt_id = f"{job.id}:{int(job.attempt or 1)}"
    if judge_meta_total["model_calls"] > 0 and flow_id:
        blind_meta["budget_ledger_key"] = f"eval-blind@{judge_attempt_id}"
    # v1.5 M1：预算消费记录（可见性：套件声明、实际累计、封顶是否触发）
    budget_meta = {
        "per_run_limit_override": max_tokens_per_run,
        "per_run_limits_by_case": {
            str(c.get("id") or ""): case_limits[i] for i, c in enumerate(cases)
        },
        "suite_total_cap": suite_total_cap,
        "used_tokens": known_total_tokens,
        "cap_accounted_tokens": used_total_tokens,
        "unknown_usage_estimated_tokens": unknown_usage_estimated_tokens,
        "usage_missing_calls": usage_missing_calls,
        "cap_hit": suite_cap_hit,
        "suite_cap_scope": "candidate_and_baseline_only; judge is reported separately",
        "judge_tokens_excluded": int(judge_meta_total.get("usage", {}).get("prompt_tokens") or 0)
            + int(judge_meta_total.get("usage", {}).get("completion_tokens") or 0)
            if isinstance(judge_meta_total.get("usage"), dict) else None,
    }
    ev.meta_json = json.dumps({"blind": blind_meta, "budget": budget_meta},
                              ensure_ascii=False)

    # 预算：评审调用全进台账（stage_id=eval-blind，预留+实际双写；
    # 同 stage_id 不重记；台账失败不拖垮已完成的评测）
    if judge_meta_total["model_calls"] > 0 and flow_id:
        try:
            budget_svc.reserve_stage_budget(
                session, flow_id, "eval-blind", attempt_id=judge_attempt_id,
            )
            budget_svc.record_stage_usage(
                session, flow_id, "eval-blind",
                budget_svc.usage_from_meta(judge_meta_total), attempt_id=judge_attempt_id,
            )
        except Exception as exc:  # noqa: BLE001: evaluation evidence records ledger failure
            session.rollback()
            ev = session.get(EvalRun, ev.id)
            blind_meta["budget_ledger_error"] = f"{type(exc).__name__}: {exc}"
            ev.meta_json = json.dumps({"blind": blind_meta, "budget": budget_meta},
                                      ensure_ascii=False)
            session.commit()

    ev.status = "finished"
    ev.finished_at = time.time()
    session.commit()

    ctx.event("finished", "评测完成")
    session.refresh(ev)
    return eval_run_view(session, ev)

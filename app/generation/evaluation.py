"""自动建立测试集并做公平对照（工程计划 §5 T14，样本 L）。

build_eval_suite：合同 + 方案 → Suite 行（不可变快照）：
- 每个必要需求一个用例（prompt 从需求内容生成，fixtures 两组共享
  ——公平对照的唯一差异是候选额外带 skill 资源，由执行层保证）；
- 每例预算 + 整体预算写入用例数据（评测预算限制显式可查）；
- 独立保留用例不用于修复（修复回路 T15 不得改套件刷分）。

evaluate_candidate：按最近一次已完成 EvalRun 出 CheckReport：
- 失败/跳过/评分失败计入分母（不只看已评分——防"分数看似通过"）；
- 每例 eval:case:{case_id}，汇总 eval:summary 带对照证据；
- v1.4 R2：用例多次尝试（repeats）按多数决聚合（平票算 failed，保守），
  flaky（结果翻转）与 non-discriminating（有无技能都过）显式入证据。

样本不足不伪造统计显著性：单次运行只报告计数，不做显著性声明。
"""
from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.ulid import new_ulid
from app.generation import blind as blind_mod
from app.generation import variance as variance_mod
from app.generation.contracts import (
    CheckReport,
    CheckResult,
    CheckStatus,
    RequirementContract,
    SkillPlan,
)
from app.models import EvalCaseResult, EvalRun, Suite

# 评测预算（默认值；进套件数据显式可查）。
# v1.5 M1：从 4k/60k 校准到实测值——completed 运行平均 62k token
# （candidate 组 101k，system 提示含完整 SKILL.md），旧值 4k 连
# system 提示都装不下（套件预算字段此前从未被消费，属死字段）。
# 新默认：单例 80k（覆盖成功路径中位数）× 整体 200k（多例封顶）。
MAX_TOKENS_PER_CASE = 80_000
MAX_TOKENS_TOTAL = 200_000

GROUPS = ("candidate", "baseline")


def _shared_fixtures(contract: RequirementContract) -> dict[str, Any]:
    """两组共享的任务输入 fixtures（公平对照的根基）。"""
    return {
        "request_text": "按以下需求处理素材并输出结果",
        "requirements": [
            {"requirement_id": r.requirement_id, "content": r.content}
            for r in contract.requirements if r.necessary
        ],
        # fixtures 本体按需求内容生成共享素材；当前最小版为需求快照
        "shared": True,
    }


def build_eval_suite(
    session: Session, contract: RequirementContract, plan: SkillPlan,
    *, project_id: str | None = None,
) -> str:
    """从合同 + 方案自动建立测试集（Suite 不可变快照）→ suite_id。

    用例 = 必要需求逐条（prompt 引需求内容与验收描述）；
    每例带 budget（每例 + 整体）与共享 fixtures。
    project_id：调用方（生成链）已知归属时显式传入，避免
    request_id 非法映射时挂错项目。
    """
    fixtures = _shared_fixtures(contract)
    acceptances = {a.acceptance_id: a for a in plan.acceptances}

    cases: list[dict[str, Any]] = []
    for req in contract.requirements:
        if not req.necessary:
            continue  # 非必要不进测试集（覆盖报告另行标注）
        accs = [a for a in plan.acceptances if req.requirement_id in a.requirement_ids]
        cases.append({
            "id": f"case:{req.requirement_id}",
            "name": f"对照用例 {req.requirement_id}",
            "prompt": (
                f"{req.content}。"
                + (f"验收：{accs[0].description}。" if accs else "")
                + "请完成并输出结果。"
            ),
            "assertions": [
                {"type": "exit_reason_is", "value": "completed"},
                {"type": "text_contains", "contains": req.requirement_id},
            ],
            "requirement_ids": [req.requirement_id],
            "acceptance_ids": [a.acceptance_id for a in accs],
            "fixtures": fixtures,
            "budget": {
                "max_tokens_per_case": MAX_TOKENS_PER_CASE,
                "max_tokens_total": MAX_TOKENS_TOTAL,
            },
        })

    suite = Suite(
        id=new_ulid(),
        project_id=project_id or _project_of(session, contract, plan),
        name=f"需求对照测试集（合同 {contract.request_id}）",
        cases_json=json.dumps(cases, ensure_ascii=False),
        created_at=time.time(),
    )
    session.add(suite)
    session.commit()
    return suite.id


def _project_of(session: Session, contract: RequirementContract, plan: SkillPlan) -> str:
    """套件挂到项目：合同 request_id 即 flow_id，查流程归属项目。"""
    from app.models import GenerationFlow

    row = session.get(GenerationFlow, contract.request_id)
    if row is not None:
        return row.project_id
    # 无流程行（单元测试直调）：挂到第一个项目或建哨兵项目
    from app.models import Project

    first = session.query(Project).first()
    if first is not None:
        return first.id
    pid = new_ulid()
    session.add(Project(id=pid, name="eval-suite- orphan"))
    session.commit()
    return pid


def _latest_eval_run(session: Session, suite_id: str, version_id: str) -> EvalRun | None:
    """最近一次已完成（finished/failed）的对照评测。"""
    rows = (
        session.query(EvalRun)
        .filter(EvalRun.suite_id == suite_id, EvalRun.version_id == version_id)
        .order_by(EvalRun.created_at.desc())
        .all()
    )
    for r in rows:
        if r.status in ("finished", "failed"):
            return r
    return None


def evaluate_candidate(
    session: Session, flow_id: str, version_id: str, suite_id: str
) -> CheckReport:
    """把最近一次对照评测转成 CheckReport。

    分母纪律：exec_failed / skipped / score_failed 全部计入验收
    （对应 FAILED / NOT_RUN），绝不只看已评分用例——分数看似通过
    但执行失败的候选不能合格。
    """
    ev = _latest_eval_run(session, suite_id, version_id)
    if ev is None:
        return CheckReport(checks=[CheckResult(
            check_id="eval:summary", status=CheckStatus.NOT_RUN,
            evidence="尚无已完成的对照评测（未运行的检查不能 passed）",
        )])

    cases = (
        session.query(EvalCaseResult)
        .filter(EvalCaseResult.eval_run_id == ev.id)
        .order_by(EvalCaseResult.created_at)
        .all()
    )

    # v1.4 R2：逐尝试行 → (group, case_id) 聚合 → 多数决结论 + flaky 标注
    rows = [
        {"group_name": c.group_name, "case_id": c.case_id,
         "exec_status": c.exec_status, "exit_reason": c.exit_reason,
         "score_status": c.score_status,
         "passed_count": c.passed_count, "total_count": c.total_count,
         "score_error": c.score_error}
        for c in cases
    ]
    stats = variance_mod.group_case_stats(rows)

    _OUTCOME_STATUS = {
        "passed": CheckStatus.PASSED,
        "failed": CheckStatus.FAILED,
        "not_run": CheckStatus.NOT_RUN,
    }

    results: list[CheckResult] = []
    by_group: dict[str, dict[str, int]] = {
        g: {"cases": 0, "executed": 0, "failed": 0, "not_run": 0, "passed_asserts": 0,
            "total_asserts": 0}
        for g in GROUPS
    }

    for group in GROUPS:
        for case_id, cstat in stats.get(group, {}).items():
            g = by_group.setdefault(group, {
                "cases": 0, "executed": 0, "failed": 0, "not_run": 0,
                "passed_asserts": 0, "total_asserts": 0,
            })
            g["cases"] += 1
            outcome = cstat["outcome"]
            if outcome == "passed":
                g["executed"] += 1
            elif outcome == "failed":
                g["failed"] += 1
            else:
                g["not_run"] += 1
            # 断言计数取该组该用例的尝试聚合（已评分尝试的合计，审计可查）
            group_rows = [r for r in rows
                          if r["group_name"] == group and r["case_id"] == case_id
                          and r["score_status"] == "scored"]
            g["passed_asserts"] += sum(int(r["passed_count"] or 0) for r in group_rows)
            g["total_asserts"] += sum(int(r["total_count"] or 0) for r in group_rows)

            detail = (
                f"多数决：{cstat['passed_votes']}/{cstat['attempts']} 次通过"
                + ("；⚠ flaky（结果翻转，通过含金量不明）" if cstat["flaky"] else "")
            )
            results.append(CheckResult(
                check_id=f"eval:case:{group}:{case_id}",
                status=_OUTCOME_STATUS[outcome], evidence=detail,
            ))

    # 汇总：分母 = 全部用例（含失败/跳过），对照证据分组可读
    cand = by_group.get("candidate", {})
    base = by_group.get("baseline", {})
    total_cases = sum(g["cases"] for g in by_group.values())
    scored_cases = sum(g["executed"] for g in by_group.values())
    ev_passed = all(
        r.status == CheckStatus.PASSED
        for r in results if r.check_id.startswith("eval:case:")
    ) and results and any(r.check_id.startswith("eval:case:") for r in results)
    summary_status = (
        CheckStatus.PASSED if ev_passed
        else (CheckStatus.FAILED if any(
            r.status == CheckStatus.FAILED for r in results
        ) else CheckStatus.NOT_RUN)
    )

    # v1.4 R2：组级方差统计（多数决 pass_rate 均值±stdev、delta、信号计数）
    var_summary = variance_mod.group_summary(stats)
    flaky_cand = sorted(cid for cid, c in stats.get("candidate", {}).items()
                        if c["flaky"])
    flaky_base = sorted(cid for cid, c in stats.get("baseline", {}).items()
                        if c["flaky"])
    non_disc = variance_mod.non_discriminating(stats)
    delta_pr = variance_mod.delta(var_summary)
    var_note = ""
    if any(c["attempts"] > 1 for g in stats.values() for c in g.values()):
        var_note = (
            f"；方差分析：candidate pass_rate {var_summary.get('candidate', {}).get('pass_rate', 0)}"
            f"±{var_summary.get('candidate', {}).get('pass_rate_stdev', 0)}，"
            f"baseline {var_summary.get('baseline', {}).get('pass_rate', 0)}"
            f"±{var_summary.get('baseline', {}).get('pass_rate_stdev', 0)}，"
            f"delta {delta_pr if delta_pr is not None else '—'}"
        )
    signal_note = ""
    signals = []
    if flaky_cand or flaky_base:
        signals.append(f"flaky 用例 {sorted(set(flaky_cand + flaky_base))}")
    if non_disc:
        signals.append(f"non-discriminating 用例 {non_disc}")
    if signals:
        signal_note = f"；信号：{'；'.join(signals)}"

    # v1.5 M4：成本可见性——聚合本次评测关联 agent_runs 的 token 消耗
    # （参考 skill-creator 把 tokens 列进 benchmark 表的做法）。断言
    # 通过与否不取决于成本，但“贵不贵”应与“好不好”同屏可见。
    cost = _eval_cost(session, cases, ev)
    cost_note = (
        f"；成本：本次评测消耗 {cost['total_tokens']:,} tokens"
        f"（candidate {cost['candidate_tokens']:,}、baseline {cost['baseline_tokens']:,}、"
        f"judge {cost['judge_tokens']:,}）"
        if cost["total_tokens"] > 0 else ""
    )

    evidence = (
        f"分母 {cand.get('cases', 0)}×2 组（含失败/跳过，非仅已评分 {scored_cases}/{total_cases}）；"
        f"candidate: 断言 {cand.get('passed_asserts', 0)}/{cand.get('total_asserts', 0)}"
        f"（执行 {cand.get('executed', 0)}、失败 {cand.get('failed', 0)}、"
        f"未运行 {cand.get('not_run', 0)}）；"
        f"baseline: 断言 {base.get('passed_asserts', 0)}/{base.get('total_asserts', 0)}"
        f"（执行 {base.get('executed', 0)}、失败 {base.get('failed', 0)}、"
        f"未运行 {base.get('not_run', 0)}）；"
        f"两组共享相同 fixtures，候选额外 skill 资源；"
        f"样本不足不声明统计显著性"
        + var_note + signal_note + cost_note
    )
    results.append(CheckResult(
        check_id="eval:summary", status=summary_status, evidence=evidence,
    ))

    # v1.4 R3：盲评偏好分布折算（建议性维度；平手/无效 → NOT_RUN 不粉饰）
    blind_check = _blind_check(ev)
    if blind_check is not None:
        results.append(blind_check)
    return CheckReport(checks=results)


def _eval_cost(session: Session, case_rows: list[EvalCaseResult],
               ev: EvalRun | None = None) -> dict[str, int]:
    """聚合本次评测的 token 消耗（v1.5 M4）。

    经 case_rows 的 agent_run_id 关联 AgentRun，按组累加
    prompt+completion；无关联（未执行）不计。返回三分量。若
    meta_json.budget 有累计值（M1 写入），与之互不覆盖——本函数算的是
    执行侧真实账单（AgentRun 落库的 usage），供证据展示。
    """
    from app.models import AgentRun

    run_ids = [r.agent_run_id for r in case_rows if r.agent_run_id]
    runs = session.query(AgentRun).filter(AgentRun.id.in_(run_ids)).all() if run_ids else []
    cand = sum((r.prompt_tokens or 0) + (r.completion_tokens or 0)
               for r in runs if r.version_id is not None)
    base = sum((r.prompt_tokens or 0) + (r.completion_tokens or 0)
               for r in runs if r.version_id is None)
    if ev is None and case_rows:
        ev = session.get(EvalRun, case_rows[0].eval_run_id)
    try:
        blind = json.loads(ev.meta_json or "{}").get("blind", {}) if ev else {}
    except (json.JSONDecodeError, TypeError):
        blind = {}
    judge_usage = blind.get("usage_observed") or blind.get("usage") or {}
    judge = int(judge_usage.get("prompt_tokens") or 0) + int(
        judge_usage.get("completion_tokens") or 0)
    return {"total_tokens": cand + base + judge,
            "candidate_tokens": cand, "baseline_tokens": base,
            "judge_tokens": judge}


def _blind_check(ev: EvalRun) -> CheckResult | None:
    """EvalRun.meta_json 的盲评段 → eval:blind 检查（无段则 None）。"""
    if not ev.meta_json:
        return None
    try:
        meta = json.loads(ev.meta_json)
    except (json.JSONDecodeError, TypeError):
        return None
    blind = meta.get("blind")
    if not isinstance(blind, dict):
        return None

    summary = blind.get("summary") or {}
    status = blind_mod.blind_check_status(summary)
    skipped = blind.get("skipped")
    evidence = (
        f"盲评偏好分布：candidate 胜 {summary.get('candidate_wins', 0)}、"
        f"baseline 胜 {summary.get('baseline_wins', 0)}、"
        f"平手 {summary.get('ties', 0)}"
        f"（有效裁决 {summary.get('valid', 0)}/{summary.get('verdicts', 0)}"
        f"，无效 {summary.get('invalid', 0)}）；"
        f"结论 {summary.get('conclusion', 'none')}"
        + (f"；未运行：{skipped}" if skipped else "")
    )
    return CheckResult(
        check_id="eval:blind", status=status, evidence=evidence,
    )

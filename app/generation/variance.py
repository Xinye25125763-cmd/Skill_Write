"""v1.4 R2：评测方差分析聚合层（纯函数，无 IO）。

输入逐例尝试（EvalCaseResult 的逻辑视图），输出统计结论：
- attempt_outcome：单次尝试结论归一（exec/score 状态 → passed/failed/not_run）；
- majority_outcome：多数决（passed 票严格过半才 passed；平票算 failed，保守）；
- flaky：尝试>1 且结论不一致（重跑会翻转 = 通过含金量不明）；
- non-discriminating：candidate 与 baseline 同用例结论相同且都过
  （有无技能都过 = 不区分技能好坏，虚增通过率）；
- 组级统计：pass_rate 均值 ± 标准差（跨用例）与 delta。

设计纪律：
- 全部纯函数，输入是可 JSON 化的字典列表，便于独立单测与重放；
- repeats=1 时多数决退化为单次结论（现行为逐字节一致）；
- 样本不足不伪造显著性：不做任何假设检验声明。
"""
from __future__ import annotations

import math
from typing import Any

OUTCOMES = ("passed", "failed", "not_run")


def attempt_outcome(row: dict[str, Any]) -> str:
    """单次尝试结论归一（与 evaluate_candidate 现行判定同语义）。

    - exec_status=skipped → not_run；
    - exec_status != executed 或 exit_reason != completed → failed；
    - score_status=scored 且全部断言通过（total>0）→ passed；
    - 其余（score_failed / 未评分 / total=0）→ not_run。
    """
    if row.get("exec_status") == "skipped":
        return "not_run"
    if row.get("exec_status") != "executed" or row.get("exit_reason") != "completed":
        return "failed"
    if row.get("score_status") == "scored":
        total = int(row.get("total_count") or 0)
        passed = int(row.get("passed_count") or 0)
        if total > 0 and passed == total:
            return "passed"
        return "failed"
    return "not_run"


def majority_outcome(attempts: list[dict[str, Any]]) -> str:
    """多数决：passed 票严格过半才 passed；平票算 failed（保守）。

    not_run 票不算 passed 也不算 failed（未运行不冒充结论），
    多数决只在 passed/failed 票之间比较；两者皆零 → not_run。
    """
    outcomes = [attempt_outcome(a) for a in attempts]
    if not outcomes:
        return "not_run"
    passed = outcomes.count("passed")
    failed = outcomes.count("failed")
    if passed == 0 and failed == 0:
        return "not_run"
    if passed > failed and passed * 2 > len(outcomes):
        return "passed"
    return "failed"


def is_flaky(attempts: list[dict[str, Any]]) -> bool:
    """flaky：尝试>1 且结论不一致（含 not_run 翻转——跳过也是翻转）。"""
    if len(attempts) <= 1:
        return False
    outcomes = {attempt_outcome(a) for a in attempts}
    return len(outcomes) > 1


def group_case_stats(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """按 (group, case_id) 聚合尝试 → 逐例统计。

    返回 {group: {case_id: {outcome, attempts, passed_votes, flaky}}}。
    """
    by: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in rows:
        by.setdefault(str(r.get("group_name") or ""), {}).setdefault(
            str(r.get("case_id") or ""), []
        ).append(r)

    out: dict[str, dict[str, Any]] = {}
    for group, cases in by.items():
        out[group] = {}
        for case_id, attempts in cases.items():
            outcomes = [attempt_outcome(a) for a in attempts]
            out[group][case_id] = {
                "outcome": majority_outcome(attempts),
                "attempts": len(attempts),
                "passed_votes": outcomes.count("passed"),
                "flaky": is_flaky(attempts),
            }
    return out


def non_discriminating(
    stats: dict[str, dict[str, dict[str, Any]]],
) -> list[str]:
    """不区分性用例：candidate 与 baseline 同用例结论相同且都 passed。

    有无技能都过 → 该用例不区分技能好坏（虚增通过率的信号）。
    缺任一组的用例无从比较，不判定。
    """
    cand = stats.get("candidate", {})
    base = stats.get("baseline", {})
    return sorted(
        cid for cid in cand
        if cid in base
        and cand[cid]["outcome"] == "passed"
        and base[cid]["outcome"] == "passed"
    )


def _mean_stdev(values: list[float]) -> tuple[float, float]:
    """均值与（总体）标准差；样本为空 → (0.0, 0.0)。"""
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(var)


def group_summary(
    stats: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """组级统计：用例级 pass_rate（多数决通过的比例）的均值 ± 标准差。

    pass_rate 逐用例取值：passed=1.0 / not_run=0.0 且不计入有效样本 /
    failed=0.0。not_run 用例（证据不足）不进均值分母——但单独计数显式可见。
    """
    out: dict[str, dict[str, Any]] = {}
    for group, cases in stats.items():
        rates = [
            1.0 if c["outcome"] == "passed" else 0.0
            for c in cases.values() if c["outcome"] != "not_run"
        ]
        mean, stdev = _mean_stdev(rates)
        out[group] = {
            "cases": len(cases),
            "scored_cases": len(rates),
            "not_run_cases": sum(1 for c in cases.values()
                                 if c["outcome"] == "not_run"),
            "pass_rate": round(mean, 4),
            "pass_rate_stdev": round(stdev, 4),
            "flaky_cases": sum(1 for c in cases.values() if c["flaky"]),
        }
    return out


def delta(
    summaries: dict[str, dict[str, Any]],
) -> float | None:
    """candidate − baseline 的 pass_rate 差；缺任一组 → None。"""
    cand = summaries.get("candidate")
    base = summaries.get("baseline")
    if cand is None or base is None:
        return None
    return round(float(cand["pass_rate"]) - float(base["pass_rate"]), 4)

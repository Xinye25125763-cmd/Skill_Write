"""功能完整性与需求覆盖证明（工程计划 §5 T12）。

按 requirement_id 给出覆盖裁决：
- missing   ：需求未映射到任何产物文件；
- blocked   ：方案声明无法满足（blocking_gaps）；
- implemented：有实现位置，但验收证据未运行或为人工项（至多 NOT_RUN，
              绝不能 passed——只写映射或文件注释不能证明实现）；
- verified  ：有实现位置 + 全部验收项检查已运行且 passed。

映射到 CheckStatus（必要项）：
    missing/blocked → FAILED（阻塞交付）
    implemented（验收未运行/人工待验收）→ NOT_RUN
    verified → PASSED
非必要项缺失 → UNSUPPORTED（如实标注，不阻塞）。

证据纪律：实现位置与 hash 来自 manifest（系统计算，不信模型自报）；
验收证据来自已运行的 CheckReport（按 acceptance_id 匹配）；人工验收
永远保留待验收，模型语义判断不能代替缺失的运行证据。
"""
from __future__ import annotations

from app.generation.contracts import (
    ArtifactManifest,
    CheckReport,
    CheckResult,
    CheckStatus,
    RequirementContract,
    SkillPlan,
)

_MANUAL_KINDS = {"manual"}


def evaluate_coverage(
    contract: RequirementContract,
    plan: SkillPlan,
    manifest: ArtifactManifest,
    checks: CheckReport,
) -> CheckReport:
    """逐需求给出覆盖检查结果（check_id = coverage:{requirement_id}）。"""
    ran = {c.check_id: c for c in checks.checks}
    blocking = " ".join(plan.blocking_gaps or [])
    results: list[CheckResult] = []

    for req in contract.requirements:
        results.append(_requirement_result(req, plan, manifest, ran, blocking))

    return CheckReport(checks=results)


def _requirement_result(req, plan, manifest, ran, blocking) -> CheckResult:  # noqa: ANN001
    rid = req.requirement_id
    locations = [a for a in manifest.artifacts if rid in a.requirement_ids]
    acceptances = [a for a in plan.acceptances if rid in a.requirement_ids]

    # blocked：方案明确声明无法满足
    if rid in blocking:
        status = CheckStatus.FAILED if req.necessary else CheckStatus.UNSUPPORTED
        gap = next(g for g in plan.blocking_gaps if rid in g)
        return CheckResult(
            check_id=f"coverage:{rid}",
            status=status,
            evidence=f"blocked（必要={req.necessary}）: {gap}",
        )

    # missing：无实现位置
    if not locations:
        if req.necessary:
            return CheckResult(
                check_id=f"coverage:{rid}", status=CheckStatus.FAILED,
                evidence="missing：必要功能未映射到任何产物文件（不能交付）",
            )
        return CheckResult(
            check_id=f"coverage:{rid}", status=CheckStatus.UNSUPPORTED,
            evidence="missing（非必要，不阻塞交付）：未映射到产物文件",
        )

    loc_text = ", ".join(f"{a.path}@{a.sha256[:12]}" for a in locations)

    # implemented：有位置，看验收证据
    manual_pending = [a for a in acceptances if a.kind in _MANUAL_KINDS]
    runnable = [a for a in acceptances if a.kind not in _MANUAL_KINDS]

    if not acceptances:
        return CheckResult(
            check_id=f"coverage:{rid}", status=CheckStatus.NOT_RUN,
            evidence=f"implemented（无验收项，证据未运行）: {loc_text}",
            artifact_hash=locations[0].sha256,
        )

    if manual_pending:
        ids = ", ".join(a.acceptance_id for a in manual_pending)
        return CheckResult(
            check_id=f"coverage:{rid}", status=CheckStatus.NOT_RUN,
            evidence=f"implemented（manual 待验收: {ids}，人工判断不自动通过）: {loc_text}",
            artifact_hash=locations[0].sha256,
        )

    unrun, failed, passed = [], [], []
    for acc in runnable:
        c = ran.get(acc.acceptance_id)
        if c is None or c.status in (CheckStatus.NOT_RUN, CheckStatus.UNSUPPORTED):
            unrun.append(acc.acceptance_id)
        elif c.status == CheckStatus.FAILED:
            failed.append(acc.acceptance_id)
        else:
            passed.append((acc.acceptance_id, c))

    if failed:
        status = CheckStatus.FAILED if req.necessary else CheckStatus.UNSUPPORTED
        return CheckResult(
            check_id=f"coverage:{rid}", status=status,
            evidence=f"implemented（验收失败: {', '.join(failed)}）: {loc_text}",
            artifact_hash=locations[0].sha256,
        )

    if unrun:
        return CheckResult(
            check_id=f"coverage:{rid}", status=CheckStatus.NOT_RUN,
            evidence=(
                f"implemented（验收未运行: {', '.join(unrun)}——"
                f"未运行的必需检查不能 passed）: {loc_text}"
            ),
            artifact_hash=locations[0].sha256,
        )

    # verified：位置 + 全部验收证据 + 版本
    evidence = "; ".join(
        f"{aid}={c.status.value}" for aid, c in passed
    )
    return CheckResult(
        check_id=f"coverage:{rid}", status=CheckStatus.PASSED,
        evidence=f"verified: 位置 {loc_text}; 验收 {evidence}",
        artifact_hash=locations[0].sha256,
    )


def summarize_coverage(report: CheckReport) -> CheckStatus:
    """整体覆盖状态：必要项失败 → FAILED；有未运行 → NOT_RUN；否则 PASSED。

    UNSUPPORTED 表示非必要缺失（如实标注、不阻塞），不参与降级。
    """
    if any(c.status == CheckStatus.FAILED for c in report.checks):
        return CheckStatus.FAILED
    if any(c.status == CheckStatus.NOT_RUN for c in report.checks):
        return CheckStatus.NOT_RUN
    return CheckStatus.PASSED

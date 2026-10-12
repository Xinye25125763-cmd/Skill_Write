"""T12：功能完整性与需求覆盖证明。

本项验收（工程计划 §5 T12，样本 D + 防误判纪律）：
1. 缺一必要功能不能合格（missing → FAILED，阻塞交付）；
2. 只写映射或文件注释不能证明实现（有文件无运行证据 → 至多
   implemented/NOT_RUN，绝不能 passed）；
3. 未运行必需检查不能 passed（验收项检查 not_run → 该需求不得 verified）；
4. verified 必须有实现位置 + 验收证据 + 证据版本（hash）；
5. 修改 D 保留旧功能与用户新要求（旧 REQ 已验证 + 新 REQ 已验证 → 都 passed）；
6. 人工验收保留待验收（manual 永不自动 passed）；
7. blocked（方案声明无法满足）→ FAILED；
8. 非必要缺失如实标注但不阻塞交付。

模型语义判断不在本层——运行证据缺失时本层只能给出 implemented，
不允许任何"看起来实现了"升级为 verified。
"""
from app.generation.contracts import (
    AcceptanceItem,
    ArtifactFile,
    ArtifactKind,
    ArtifactManifest,
    CheckReport,
    CheckResult,
    CheckStatus,
    PlannedFile,
    Requirement,
    RequirementContract,
    SkillPlan,
)

SHA = "a" * 64


def _req(rid: str, necessary: bool = True) -> Requirement:
    return Requirement.model_validate({
        "requirement_id": rid,
        "content": f"功能 {rid}",
        "necessary": necessary,
        "source_type": "user_message",
        "source_ref": "m",
        "confirmation": "confirmed",
    })


def _contract(*reqs: Requirement) -> RequirementContract:
    return RequirementContract.model_validate({
        "request_id": "flow-1",
        "target_platform": "codex",
        "requirements": [r.model_dump() for r in reqs],
    })


def _plan_file(path: str, req_ids: list[str]) -> PlannedFile:
    return PlannedFile.model_validate({
        "path": path, "kind": "text", "purpose": "p", "requirement_ids": req_ids,
    })


def _acc(aid: str, req_ids: list[str], kind: str = "static") -> AcceptanceItem:
    return AcceptanceItem.model_validate({
        "acceptance_id": aid, "kind": kind, "description": "验收", "requirement_ids": req_ids,
    })


def _artifact(path: str, req_ids: list[str]) -> ArtifactFile:
    return ArtifactFile.model_validate({
        "path": path, "kind": ArtifactKind.TEXT, "sha256": SHA,
        "requirement_ids": req_ids, "content": "内容",
    })


def _coverage(contract, plan, manifest, checks=None):
    from app.generation.coverage import evaluate_coverage
    return evaluate_coverage(
        contract, plan, manifest, checks or CheckReport(checks=[])
    )


def _by_id(report: CheckReport, rid: str) -> CheckResult:
    got = [c for c in report.checks if c.check_id == f"coverage:{rid}"]
    assert len(got) == 1, f"缺 coverage:{rid}: {[c.check_id for c in report.checks]}"
    return got[0]


def test_missing_required_feature_and_unrun_check_block_delivery() -> None:
    from app.generation.coverage import summarize_coverage

    # ---------- 验收 1：缺一必要功能不能合格 ----------
    contract = _contract(_req("REQ-001"), _req("REQ-002"))
    plan = SkillPlan.model_validate({
        "plan_id": "plan-1",
        "files": [_plan_file("SKILL.md", ["REQ-001"])],
        "acceptances": [_acc("ACC-001", ["REQ-001"])],
    })
    manifest = ArtifactManifest(artifacts=[_artifact("SKILL.md", ["REQ-001"])])
    report = _coverage(contract, plan, manifest)
    r2 = _by_id(report, "REQ-002")
    assert r2.status == CheckStatus.FAILED, "必要功能缺失必须 FAILED"
    assert "missing" in r2.evidence
    assert report.has_failed, "必要功能缺失必须使报告不合格"
    assert summarize_coverage(report) == CheckStatus.FAILED

    # ---------- 验收 2：只写映射/文件不能证明实现 ----------
    checks = CheckReport(checks=[
        CheckResult(check_id="ACC-001", status=CheckStatus.NOT_RUN, evidence="预算不足未运行"),
    ])
    report = _coverage(contract, plan, manifest, checks)
    r1 = _by_id(report, "REQ-001")
    assert r1.status == CheckStatus.NOT_RUN, "有实现无运行证据至多 implemented"
    assert "implemented" in r1.evidence
    assert r1.status != CheckStatus.PASSED

    # ---------- 验收 3：未运行必需检查不能 passed（验收项 not_run） ----------
    contract_only1 = _contract(_req("REQ-001"))
    report_only1 = _coverage(contract_only1, plan, manifest, checks)
    assert summarize_coverage(report_only1) == CheckStatus.NOT_RUN

    # ---------- 验收 4：verified 需要位置 + 证据 + 版本 ----------
    checks = CheckReport(checks=[
        CheckResult(check_id="ACC-001", status=CheckStatus.PASSED,
                    evidence="静态检查通过", artifact_hash=SHA),
    ])
    contract1 = _contract(_req("REQ-001"))
    report = _coverage(contract1, plan, manifest, checks)
    r1 = _by_id(report, "REQ-001")
    assert r1.status == CheckStatus.PASSED
    assert "SKILL.md" in r1.evidence, "证据须含实现位置"
    assert SHA in r1.evidence or r1.artifact_hash == SHA, "证据须含版本（hash）"
    assert "ACC-001" in r1.evidence, "证据须含验收项"
    assert summarize_coverage(report) == CheckStatus.PASSED

    # ---------- 验收 5：修改 D 保留旧功能与用户新要求 ----------
    contract = _contract(_req("REQ-001"), _req("REQ-005"))  # 旧 + 新
    plan = SkillPlan.model_validate({
        "plan_id": "plan-2",
        "files": [_plan_file("SKILL.md", ["REQ-001"]),
                  _plan_file("scripts/extra.py", ["REQ-005"])],
        "acceptances": [
            _acc("ACC-001", ["REQ-001"]),
            _acc("ACC-005", ["REQ-005"]),
        ],
    })
    manifest = ArtifactManifest(artifacts=[
        _artifact("SKILL.md", ["REQ-001"]),
        _artifact("scripts/extra.py", ["REQ-005"]),
    ])
    checks = CheckReport(checks=[
        CheckResult(check_id="ACC-001", status=CheckStatus.PASSED, artifact_hash=SHA),
        CheckResult(check_id="ACC-005", status=CheckStatus.PASSED, artifact_hash=SHA),
    ])
    report = _coverage(contract, plan, manifest, checks)
    assert _by_id(report, "REQ-001").status == CheckStatus.PASSED
    assert _by_id(report, "REQ-005").status == CheckStatus.PASSED
    assert summarize_coverage(report) == CheckStatus.PASSED

    # ---------- 验收 6：人工验收保留待验收 ----------
    plan_manual = SkillPlan.model_validate({
        "plan_id": "plan-3",
        "files": [_plan_file("SKILL.md", ["REQ-001"])],
        "acceptances": [_acc("ACC-001", ["REQ-001"], kind="manual")],
    })
    report = _coverage(contract1, plan_manual, manifest)
    r1 = _by_id(report, "REQ-001")
    assert r1.status == CheckStatus.NOT_RUN, "人工验收不自动通过"
    assert "待验收" in r1.evidence or "manual" in r1.evidence

    # ---------- 验收 7：blocked（方案声明无法满足）----------
    contract = _contract(_req("REQ-003"))
    plan_blocked = SkillPlan.model_validate({
        "plan_id": "plan-4",
        "files": [_plan_file("SKILL.md", [])],
        "acceptances": [_acc("ACC-003", ["REQ-003"])],
        "blocking_gaps": ["REQ-003 需要图像渲染工具，环境缺失"],
    })
    report = _coverage(contract, plan_blocked, ArtifactManifest(artifacts=[_artifact("SKILL.md", [])]))
    r3 = _by_id(report, "REQ-003")
    assert r3.status == CheckStatus.FAILED, "blocking gap 必须 FAILED"
    assert "blocked" in r3.evidence

    # ---------- 验收 8：非必要缺失如实标注但不阻塞 ----------
    contract = _contract(_req("REQ-001"), _req("REQ-004", necessary=False))
    plan = SkillPlan.model_validate({
        "plan_id": "plan-5",
        "files": [_plan_file("SKILL.md", ["REQ-001"])],
        "acceptances": [_acc("ACC-001", ["REQ-001"])],
    })
    manifest = ArtifactManifest(artifacts=[_artifact("SKILL.md", ["REQ-001"])])
    checks = CheckReport(checks=[
        CheckResult(check_id="ACC-001", status=CheckStatus.PASSED, artifact_hash=SHA),
    ])
    report = _coverage(contract, plan, manifest, checks)
    r4 = _by_id(report, "REQ-004")
    assert r4.status != CheckStatus.FAILED, "非必要缺失不阻塞"
    assert "missing" in r4.evidence
    assert not report.has_failed
    assert summarize_coverage(report) == CheckStatus.PASSED

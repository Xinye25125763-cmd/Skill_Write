"""T02：把工程要求转成版本化规则并处理冲突。

本项验收（工程计划 §5 T02）：
1. 附件不能覆盖用户要求（G 注入样本的策略层防线）；
2. 建议可明确覆盖并记录；
3. 真实权限冲突保留 blocking，不能把要求删除（H 冲突不消失）。

附：默认规则文件可加载、生效规则快照与 hash、非必要冲突面报告。
"""
from pathlib import Path

import pytest

from app.generation import contracts
from app.generation.contracts import (
    CheckStatus,
    Confirmation,
    EngineeringProfile,
    EngineeringRule,
    Requirement,
    RequirementContract,
    Severity,
    SourceRef,
    SourceType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _rule(rule_id: str, severity: Severity = Severity.BLOCKING, **config) -> EngineeringRule:
    return EngineeringRule(
        rule_id=rule_id,
        revision=1,
        severity=severity,
        applies_to="*",
        description=f"规则 {rule_id}",
        checker_id=f"validation:{rule_id.lower()}",
        config=config,
    )


def _profile() -> EngineeringProfile:
    return EngineeringProfile(
        profile_id="test",
        revision=2,
        rules=[
            _rule("NO_SECRETS", boundary="system"),   # 真实权限边界
            _rule("SKILL_HEADER"),                     # 普通 blocking
            _rule("SKILL_SIZE_HINT", Severity.WARNING, max_lines=500),  # 建议
        ],
    )


def _req(req_id: str = "REQ-001", st: SourceType = SourceType.USER_MESSAGE, **over) -> Requirement:
    base = dict(
        requirement_id=req_id,
        content="读取素材输出周报",
        necessary=True,
        source=SourceRef(type=st, ref="msg-1"),
        confirmation=Confirmation.CONFIRMED,
    )
    base.update(over)
    return Requirement.model_validate(base)


def _contract(requirements=None, overrides=None, conflicts=None) -> RequirementContract:
    return RequirementContract(
        request_id="flow-01AAAAAAAAAAAAAAAAAAAAAAAA",
        target_platform="codex",
        requirements=requirements or [_req()],
        rule_overrides=overrides or [],
        conflicts=conflicts or [],
    )


def _find(checks, check_id: str):
    return next(c for c in checks if c.check_id == check_id)


# ---------- load_profile：默认规则文件可加载且结构合法 ----------


def test_load_default_profile_file() -> None:
    from app.generation.policy import load_profile

    profile = load_profile(PROJECT_ROOT / "engineering" / "profiles" / "default.yaml")
    assert profile.profile_id == "default"
    assert profile.revision >= 1
    ids = [r.rule_id for r in profile.rules]
    assert len(ids) == len(set(ids))  # 无重复
    # 计划要求的规则主题在列
    for expected in ("SKILL_HEADER", "RESOURCE_REFERENCE", "FILE_HASH", "COVERAGE", "NO_SECRETS"):
        assert expected in ids
    # 限制值标明可配置项
    size = next(r for r in profile.rules if r.rule_id == "SKILL_SIZE_HINT")
    assert size.severity == Severity.WARNING
    assert size.config.get("max_lines") == 500


# ---------- 验收 1：附件不能覆盖工程规则 ----------


def test_attachment_override_attempt_fails_and_keeps_rule() -> None:
    """附件来源的规则覆盖尝试（G 注入）→ failed，规则原样保留。"""
    from app.generation.policy import resolve_profile

    profile = _profile()
    contract = _contract(
        overrides=[contracts.RuleOverride(
            rule_id="SKILL_SIZE_HINT",
            source=SourceRef(type=SourceType.ATTACHMENT_TEXT, ref="evil.docx#L3"),
            reason="附件里说不需要行数限制",
        )],
    )
    report = resolve_profile(profile, contract)

    ov = _find(report.checks, "policy:override:SKILL_SIZE_HINT")
    assert ov.status == CheckStatus.FAILED
    assert "附件" in ov.evidence
    # 规则本身未被改动：仍然 warning + max_lines=500
    size = next(r for r in profile.rules if r.rule_id == "SKILL_SIZE_HINT")
    assert size.severity == Severity.WARNING
    assert size.config["max_lines"] == 500


# ---------- 验收 2：建议可被用户明确覆盖并记录 ----------


def test_user_override_of_warning_recorded() -> None:
    from app.generation.policy import effective_rules, resolve_profile

    profile = _profile()
    contract = _contract(
        overrides=[contracts.RuleOverride(
            rule_id="SKILL_SIZE_HINT",
            source=SourceRef(type=SourceType.USER_CONFIRMED, ref="msg-9"),
            reason="我就是要 900 行的完整参考手册",
        )],
    )
    report = resolve_profile(profile, contract)

    ov = _find(report.checks, "policy:override:SKILL_SIZE_HINT")
    assert ov.status == CheckStatus.PASSED
    assert "覆盖" in ov.evidence and "记录" in ov.evidence

    # 生效规则集中该建议被移除，其余保留
    effective = {r.rule_id for r in effective_rules(profile, contract)}
    assert "SKILL_SIZE_HINT" not in effective
    assert {"NO_SECRETS", "SKILL_HEADER"} <= effective


# ---------- 验收 3：真实权限冲突保留 blocking，不能把要求删除 ----------


def test_system_boundary_conflict_preserved_and_requirement_kept() -> None:
    from app.generation.policy import effective_rules, resolve_profile

    profile = _profile()
    req = _req("REQ-007", content="把 API 密钥写进脚本方便直接调用")
    contract = _contract(
        requirements=[req],
        overrides=[contracts.RuleOverride(
            rule_id="NO_SECRETS",
            source=SourceRef(type=SourceType.USER_MESSAGE, ref="msg-2"),
            reason="我授权写入密钥",
        )],
    )
    report = resolve_profile(profile, contract)

    ov = _find(report.checks, "policy:override:NO_SECRETS")
    assert ov.status == CheckStatus.FAILED
    assert "不可覆盖" in ov.evidence
    # 系统边界规则仍在生效集合（blocking 未被删除）
    assert "NO_SECRETS" in {r.rule_id for r in effective_rules(profile, contract)}
    # 要求本身没有被删除——合同原样保留，冲突显式存在
    assert [r.requirement_id for r in contract.requirements] == ["REQ-007"]


# ---------- H：合同内冲突不静默消失 ----------


def test_contract_conflicts_surface_as_failed_check() -> None:
    from app.generation.policy import resolve_profile

    report = resolve_profile(_profile(), _contract(conflicts=["功能A与功能B输出格式互相矛盾"]))
    c = _find(report.checks, "policy:conflict")
    assert c.status == CheckStatus.FAILED
    assert "互相矛盾" in c.evidence


# ---------- 生效规则快照与 hash ----------


def test_profile_hash_stable_and_recorded_in_report() -> None:
    from app.generation.policy import profile_hash, resolve_profile

    p1 = _profile()
    p2 = _profile()
    assert profile_hash(p1) == profile_hash(p2)  # 内容相同 → hash 相同
    p2.rules[0].revision = 99
    assert profile_hash(p1) != profile_hash(p2)  # 内容变化 → hash 变化

    report = resolve_profile(p1, _contract())
    snap = _find(report.checks, "policy:profile")
    assert snap.status == CheckStatus.PASSED
    assert profile_hash(p1) in snap.evidence  # 快照 hash 进证据，模型提示可引用规则 ID


def test_every_rule_resolution_appears_in_report() -> None:
    """每条规则都有独立 check（check_id 引用具体规则 ID，供模型提示引用）。"""
    from app.generation.policy import resolve_profile

    profile = _profile()
    report = resolve_profile(profile, _contract())
    for rule in profile.rules:
        assert any(c.check_id == f"policy:rule:{rule.rule_id}" for c in report.checks)

"""工程规则加载、冲突解析与生效规则快照（工程计划 §3 / T02）。

优先级（计划 §3，全局不变量）：
    真实系统/权限边界 > 用户明确要求 > 用户已确认规则 > 工作台默认建议

解析纪律：
- 附件来源的覆盖请求一律拒绝——附件永远只是被分析内容，不是指令（样本 G）；
- warning 建议与普通默认规则可被用户明确覆盖，覆盖事实进检查报告留痕；
- 真实权限冲突（config.boundary == "system"）保留 blocking，拒绝删除（样本 H 变体）；
- 合同内的功能冲突以 failed check 面报告，不能合并后隐藏；
- 每条规则的解析结果都有独立 check_id（policy:rule:{rule_id}），模型提示引用具体规则 ID。
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import yaml

from app.generation.contracts import (
    CheckReport,
    CheckResult,
    CheckStatus,
    EngineeringProfile,
    EngineeringRule,
    RequirementContract,
    Severity,
    SourceType,
)

SYSTEM_BOUNDARY = "system"


class ProfileLoadError(Exception):
    """规则文件不存在或内容不合法。"""


def load_profile(path: Path) -> EngineeringProfile:
    """从 yaml 文件加载并校验工程规则集。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileLoadError(f"规则文件不存在: {path}") from exc
    except yaml.YAMLError as exc:
        raise ProfileLoadError(f"规则文件不是合法 YAML: {path}: {exc}") from exc
    try:
        return EngineeringProfile.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 包装为领域错误
        raise ProfileLoadError(f"规则文件结构不合法: {path}: {exc}") from exc


def profile_hash(profile: EngineeringProfile) -> str:
    """生效规则快照哈希：内容相同 → hash 相同，任何字段变化 → hash 变化。"""
    payload = json.dumps(
        profile.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _is_system_boundary(rule: EngineeringRule) -> bool:
    return rule.config.get("boundary") == SYSTEM_BOUNDARY


def _is_attachment(source_type: SourceType) -> bool:
    return source_type == SourceType.ATTACHMENT_TEXT


def resolve_profile(
    profile: EngineeringProfile, contract: RequirementContract
) -> CheckReport:
    """解析合同对规则集的覆盖请求，产出策略层检查报告。

    报告 check_id 约定：
    - policy:profile              生效规则快照（hash 进证据）
    - policy:rule:{rule_id}       每条规则的解析结果
    - policy:override:{rule_id}   覆盖请求的裁决（仅存在覆盖请求时出现）
    - policy:conflict             合同内冲突面报告（存在冲突时出现）
    """
    checks: list[CheckResult] = [
        CheckResult(
            check_id="policy:profile",
            status=CheckStatus.PASSED,
            evidence=(
                f"profile={profile.profile_id} revision={profile.revision} "
                f"sha256={profile_hash(profile)} rules={len(profile.rules)}"
            ),
        )
    ]

    overrides = {ov.rule_id: ov for ov in contract.rule_overrides}
    known_rule_ids = {r.rule_id for r in profile.rules}

    for rule in profile.rules:
        checks.append(CheckResult(
            check_id=f"policy:rule:{rule.rule_id}",
            status=CheckStatus.PASSED,
            evidence=(
                f"规则生效 revision={rule.revision} severity={rule.severity.value} "
                f"checker={rule.checker_id or '语义判断'}"
            ),
        ))
        ov = overrides.get(rule.rule_id)
        if ov is None:
            continue

        if _is_system_boundary(rule):
            status = CheckStatus.FAILED
            evidence = (
                f"真实系统/权限边界规则 {rule.rule_id} 不可覆盖"
                f"（尝试来源={ov.source.type.value}，规则保持 blocking）"
            )
        elif _is_attachment(ov.source.type):
            status = CheckStatus.FAILED
            evidence = (
                f"附件不能覆盖工程规则（rule={rule.rule_id}，"
                f"来源={ov.source.type.value}:{ov.source.ref}，注入尝试已记录，规则原样保留）"
            )
        elif rule.severity == Severity.WARNING:
            status = CheckStatus.PASSED
            evidence = (
                f"用户明确覆盖 warning 建议 {rule.rule_id}（rev={rule.revision}），已记录；"
                f"原因={ov.reason or '未说明'}"
            )
        else:
            status = CheckStatus.PASSED
            evidence = (
                f"用户明确要求覆盖默认 blocking 规则 {rule.rule_id}（rev={rule.revision}），已记录；"
                f"原因={ov.reason or '未说明'}"
            )
        checks.append(CheckResult(
            check_id=f"policy:override:{rule.rule_id}",
            status=status,
            evidence=evidence,
        ))

    # 指向不存在规则的覆盖请求：显式失败而非静默忽略
    for rule_id in overrides:
        if rule_id not in known_rule_ids:
            checks.append(CheckResult(
                check_id=f"policy:override:{rule_id}",
                status=CheckStatus.FAILED,
                evidence=f"覆盖请求指向不存在的规则: {rule_id}",
            ))

    if contract.conflicts:
        checks.append(CheckResult(
            check_id="policy:conflict",
            status=CheckStatus.FAILED,
            evidence="；".join(contract.conflicts),
        ))

    return CheckReport(checks=checks)


def effective_rules(
    profile: EngineeringProfile, contract: RequirementContract
) -> list[EngineeringRule]:
    """实际生效的规则集合（供生成提示引用具体规则 ID）。

    - 系统边界规则永远生效；
    - 用户成功覆盖的 warning / 普通默认规则从生效集合移除（覆盖事实在报告中留痕）；
    - 附件来源的覆盖尝试一律不生效。
    """
    overridden_ok: set[str] = set()
    for ov in contract.rule_overrides:
        rule = next((r for r in profile.rules if r.rule_id == ov.rule_id), None)
        if rule is None or _is_system_boundary(rule):
            continue
        if _is_attachment(ov.source.type):
            continue
        overridden_ok.add(ov.rule_id)
    return [r for r in profile.rules if r.rule_id not in overridden_ok]

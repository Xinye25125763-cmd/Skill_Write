"""T01：合同和工程规则的可验证类型。

本项验收（工程计划 §5 T01）：
1. 序列化往返不丢来源和 requirement_id；
2. 未知 severity / 重复 ID / 预算负数拒绝；
3. 旧 brief（brief_schema）仍可正常解析。
"""
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.generation.contracts as contracts
from app.services.brief_schema import (
    BriefDraft,
    BriefPayload,
    FieldSource,
    SourcedField,
    SourcedList,
    SourceType,
    validate_brief,
)


def _mk_requirement(**over) -> dict:
    """构造一条合法需求功能（缺省值可覆盖）。"""
    base = dict(
        requirement_id="REQ-001",
        content="读取素材并按模板输出周报 Markdown",
        necessary=True,
        source_type="user_message",
        source_ref="msg-1",
        confirmation="confirmed",
        acceptance_ids=["ACC-001"],
    )
    base.update(over)
    return base


def _mk_contract(**over) -> dict:
    base = dict(
        schema_version="1",
        request_id="flow-01AAAAAAAAAAAAAAAAAAAAAAAA",
        target_platform="codex",
        requirements=[_mk_requirement()],
    )
    base.update(over)
    return base


def _mk_rule(**over) -> dict:
    base = dict(
        rule_id="SKILL_HEADER",
        revision=1,
        severity="blocking",
        applies_to="SKILL.md",
        description="SKILL.md 必须含 name/description 头",
        checker_id="validation:skill_header",
    )
    base.update(over)
    return base


# ---------- 验收 1：序列化往返不丢来源和 requirement_id ----------


def test_contract_roundtrip_preserves_sources_and_ids() -> None:
    """合同 → JSON → 合同：requirement_id、来源、确认状态全部无损。"""
    contract = contracts.RequirementContract.model_validate(_mk_contract(
        requirements=[
            _mk_requirement(),
            _mk_requirement(
                requirement_id="REQ-002",
                content="不虚构数据",
                source_type="attachment_text",
                source_ref="附件.docx#L12",
                confirmation="pending",
            ),
        ],
    ))
    restored = contracts.RequirementContract.model_validate_json(contract.model_dump_json())

    assert [r.requirement_id for r in restored.requirements] == ["REQ-001", "REQ-002"]
    r2 = restored.requirements[1]
    assert r2.source.type == contracts.SourceType.ATTACHMENT_TEXT
    assert r2.source.ref == "附件.docx#L12"
    assert r2.confirmation == "pending"
    # 必要性与验收 ID 同样往返无损
    assert restored.requirements[0].necessary is True
    assert restored.requirements[0].acceptance_ids == ["ACC-001"]
    assert restored.schema_version == "1"


def test_profile_roundtrip_preserves_rules() -> None:
    profile = contracts.EngineeringProfile.model_validate({
        "schema_version": "1",
        "profile_id": "default",
        "revision": 3,
        "rules": [_mk_rule(), _mk_rule(rule_id="NO_SECRETS", severity="warning")],
    })
    restored = contracts.EngineeringProfile.model_validate_json(profile.model_dump_json())
    assert [r.rule_id for r in restored.rules] == ["SKILL_HEADER", "NO_SECRETS"]
    assert restored.rules[0].severity == contracts.Severity.BLOCKING
    assert restored.revision == 3


# ---------- 验收 2：非法输入拒绝 ----------


def test_unknown_severity_rejected() -> None:
    with pytest.raises(ValidationError):
        contracts.EngineeringProfile.model_validate({
            "schema_version": "1",
            "profile_id": "default",
            "revision": 1,
            "rules": [_mk_rule(severity="fatal")],
        })


def test_duplicate_rule_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="(?i)重复|duplicate"):
        contracts.EngineeringProfile.model_validate({
            "schema_version": "1",
            "profile_id": "default",
            "revision": 1,
            "rules": [_mk_rule(), _mk_rule()],
        })


def test_duplicate_requirement_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="(?i)重复|duplicate"):
        contracts.RequirementContract.model_validate(_mk_contract(
            requirements=[_mk_requirement(), _mk_requirement()],
        ))


def test_negative_budget_rejected() -> None:
    with pytest.raises(ValidationError):
        contracts.FlowBudget.model_validate({
            "schema_version": "1",
            "total_tokens": -100,
        })


@pytest.mark.parametrize("field", ["max_tokens_total", "max_model_calls", "max_repair_rounds"])
def test_negative_flow_budget_fields_rejected(field: str) -> None:
    data = {"schema_version": "1", "total_tokens": 1000, field: -1}
    with pytest.raises(ValidationError):
        contracts.FlowBudget.model_validate(data)


# ---------- 检查报告 / 快照 / 收据的结构约束 ----------


def test_check_status_enum_and_fields() -> None:
    ok = contracts.CheckResult.model_validate({
        "check_id": "coverage:REQ-001",
        "status": "passed",
        "evidence": "assertion result",
        "artifact_hash": "sha256:abc",
    })
    assert ok.status == contracts.CheckStatus.PASSED

    with pytest.raises(ValidationError):
        contracts.CheckResult.model_validate({"check_id": "x", "status": "maybe"})


def test_flow_snapshot_states_and_unknown_transition() -> None:
    snap = contracts.FlowSnapshot.model_validate({
        "schema_version": "1",
        "flow_id": "flow-01AAAAAAAAAAAAAAAAAAAAAAAA",
        "project_id": "proj-01AAAAAAAAAAAAAAAAAAAAAA",
        "state": "queued",
        "revision": 1,
    })
    assert snap.state == contracts.FlowState.QUEUED

    with pytest.raises(ValidationError):
        contracts.FlowSnapshot.model_validate({
            "schema_version": "1",
            "flow_id": "f", "project_id": "p", "state": "exploding", "revision": 1,
        })


def test_artifact_manifest_kinds_and_hash() -> None:
    manifest = contracts.ArtifactManifest.model_validate({
        "schema_version": "1",
        "artifacts": [
            {"path": "SKILL.md", "kind": "text", "content": "# s", "sha256": "h1",
             "purpose": "正文", "requirement_ids": ["REQ-001"]},
            {"path": "assets/logo.png", "kind": "binary", "resource_id": "res-01",
             "sha256": "h2", "purpose": "周报 logo", "requirement_ids": []},
        ],
    })
    assert manifest.artifacts[1].kind == contracts.ArtifactKind.BINARY
    assert manifest.artifacts[1].resource_id == "res-01"
    # 文本与二进制互斥：文本不能带 resource_id 占位内容
    with pytest.raises(ValidationError):
        contracts.ArtifactFile.model_validate({
            "path": "a.md", "kind": "text", "content": "x", "resource_id": "res-1", "sha256": "h",
        })


def test_delivery_receipt_carries_status_dimensions() -> None:
    receipt = contracts.DeliveryReceipt.model_validate({
        "schema_version": "1",
        "flow_id": "flow-01AAAAAAAAAAAAAAAAAAAAAAAA",
        "version_id": "ver-01AAAAAAAAAAAAAAAAAAAAAAA",
        "delivery_status": "candidate",
        "static_status": "passed",
        "execution_status": "failed",
        "coverage_status": "passed",
        "host_status": "not_run",
        "zip_path": "deliveries/x.zip",
    })
    assert receipt.delivery_status == contracts.DeliveryStatus.CANDIDATE
    assert receipt.host_status == contracts.CheckStatus.NOT_RUN


# ---------- 验收 3：旧 brief 仍可正常解析 ----------


def test_legacy_brief_still_parses_and_validates() -> None:
    draft = BriefDraft(brief=BriefPayload(
        purpose=SourcedField(value="周报助手", source=FieldSource(type=SourceType.USER_MESSAGE)),
        trigger_conditions=SourcedList(items=[
            SourcedField(value="帮我写周报", source=FieldSource(type=SourceType.USER_MESSAGE)),
        ]),
        non_trigger_conditions=SourcedList(items=[
            SourcedField(value="写月报", source=FieldSource(type=SourceType.SYSTEM_INFERRED)),
        ]),
        outputs=[{"name": "weekly.md", "format": "markdown"}],
    ))
    errors = validate_brief(draft)
    assert errors == []
    assert draft.explicit == ["purpose", "trigger_conditions[0]"]
    assert draft.inferred == ["non_trigger_conditions[0]"]


# ---------- 配置扩展：默认规则路径与版本，不改旧默认 ----------


def test_config_has_profile_defaults_without_breaking_old() -> None:
    from app.core.config import AppConfig

    cfg = AppConfig()
    # 旧默认不变
    assert cfg.data_dir == Path("data")
    assert cfg.server.port == 8710
    # 新增：工程规则配置
    assert cfg.generation.profile_path == Path("engineering/profiles/default.yaml")
    assert cfg.generation.profile_id == "default"

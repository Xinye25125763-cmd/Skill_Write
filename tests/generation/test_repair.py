"""T15：证据驱动的有界自动修复。

本项验收（工程计划 §5 T15）：
1. 修复缺链接不删相关功能（修复后覆盖检查不出现新的 missing）；
2. 修复后重新验收（返回的报告基于修复后 manifest 重新评估）；
3. 不修改测试断言降低标准来刷分（套件/断言不可变——修复层根本拿不到）；
4. 连续相同失败或轮次/预算到达 → 停止并保存候选（有界）。

接口：repair_candidate(provider, contract, profile, candidate: ArtifactManifest,
report: CheckReport) -> ArtifactManifest
"""
import json
from hashlib import sha256
from typing import Any

import pytest
from app.llm import Capabilities, ChatMessage, ModelResponse, Usage
from app.llm.base import ModelProvider

from app.generation.contracts import (
    ArtifactFile,
    ArtifactKind,
    ArtifactManifest,
    CheckReport,
    CheckResult,
    CheckStatus,
    EngineeringProfile,
    Requirement,
    RequirementContract,
)

ORIG_MD = "# 周报（无 front matter，缺链接）\n"
SHA = sha256(ORIG_MD.encode()).hexdigest()
SHA2 = "c" * 64


def _profile() -> EngineeringProfile:
    return EngineeringProfile.model_validate({
        "schema_version": "1", "profile_id": "default", "revision": 1,
        "rules": [{
            "rule_id": "SKILL_HEADER", "revision": 1, "severity": "blocking",
            "applies_to": "SKILL.md", "description": "SKILL.md 头信息",
            "checker_id": "validation:skill_header",
        }],
    })


def _contract() -> RequirementContract:
    return RequirementContract.model_validate({
        "request_id": "flow-1", "target_platform": "codex",
        "requirements": [{
            "requirement_id": "REQ-001",
            "content": "SKILL.md 含使用说明与输出模板链接",
            "necessary": True,
            "source_type": "user_message", "source_ref": "m",
            "confirmation": "confirmed",
        }],
    })


def _manifest(content: str) -> ArtifactManifest:
    return ArtifactManifest(artifacts=[
        ArtifactFile.model_validate({
            "path": "SKILL.md", "kind": ArtifactKind.TEXT,
            "sha256": sha256(content.encode()).hexdigest(),
            "requirement_ids": ["REQ-001"],
            "content": content,
        }),
    ])


class ScriptedRepairProvider(ModelProvider):
    """按剧本返回修复文件 JSON；记录调用。"""

    capabilities = Capabilities(tools=False, streaming=False, json_mode=True,
                                max_context_tokens=100_000)

    def __init__(self, script: list[dict | Exception]) -> None:
        self.script = list(script)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> ModelResponse:
        self.calls.append(list(messages))
        if not self.script:
            return ModelResponse(content="{}", usage=Usage(10, 5), finish_reason="stop")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ModelResponse(content=json.dumps(item, ensure_ascii=False),
                             usage=Usage(100, 200), finish_reason="stop")


FIXED_MD = "---\nname: weekly-report\ndescription: 周报\n---\n# 周报\n模板见 [template](templates/weekly.md)\n"

_fixed_resp = {"path": "SKILL.md", "kind": "text", "complete": True,
               "content": FIXED_MD, "requirement_ids": ["REQ-001"]}


def _report(with_failed: bool = True) -> CheckReport:
    return CheckReport(checks=[
        CheckResult(check_id="validation:skill_header", status=CheckStatus.FAILED,
                    evidence="SKILL.md 头信息非法：front matter 缺失",
                    artifact_hash=SHA),
    ] if with_failed else [CheckResult(
        check_id="validation:skill_header", status=CheckStatus.PASSED,
        evidence="头信息合法", artifact_hash=SHA2,
    )])


def test_repair_preserves_features_and_stops_on_no_progress() -> None:
    from app.generation.repair import MAX_REPAIR_ROUNDS, RepairStop, repair_candidate

    # ---------- 验收 1+2：修复缺链接不删功能，修复后重新验收 ----------
    provider = ScriptedRepairProvider([_fixed_resp])
    candidate = _manifest(ORIG_MD)
    manifest, meta = repair_candidate(
        provider, _contract(), _profile(), candidate, _report(),
        return_meta=True,
    )
    assert manifest.artifacts[0].content == FIXED_MD
    assert manifest.artifacts[0].requirement_ids == ["REQ-001"], "修复不得删功能映射"
    # 重新验收：新 hash 由系统计算
    assert manifest.artifacts[0].sha256 != SHA
    assert meta["model_calls"] == 1
    # 证据进了修复提示词（失败证据 + 实际文件内容）
    assert any("front matter" in (m.content or "") for m in provider.calls[0])
    assert any("# 周报" in (m.content or "") for m in provider.calls[0])

    # ---------- 验收 3：不修改测试断言刷分 ----------
    # repair_candidate 签名里没有 suite/断言参数（拿不到就改不了）；
    # 这里以"修复提示词只含失败证据与文件，不含测试断言"作结构验证
    provider2 = ScriptedRepairProvider([_fixed_resp])
    repair_candidate(provider2, _contract(), _profile(), candidate, _report())
    joined = "\n".join(m.content or "" for m in provider2.calls[0])
    assert "assertion" not in joined.lower()
    assert "断言" not in joined

    # ---------- 验收 4：连续相同失败 → 停止并保存候选 ----------
    # 模型连续返回同样的坏文件（修复无进展）
    same_bad = {"path": "SKILL.md", "kind": "text", "complete": True,
                "content": ORIG_MD,
                "requirement_ids": ["REQ-001"]}
    provider3 = ScriptedRepairProvider([same_bad, same_bad, same_bad])
    manifest, meta = repair_candidate(
        provider3, _contract(), _profile(), candidate, _report(), return_meta=True,
    )
    # 有界：轮次上限内停止，返回最后候选（保存，不抛"无限修复"）
    assert meta["model_calls"] <= MAX_REPAIR_ROUNDS
    assert manifest is not None, "停止时保存候选"
    assert meta.get("stopped_reason"), "停止原因显式记录"

    # ---------- 验收 4b：轮次耗尽 → 停止 ----------
    provider4 = ScriptedRepairProvider([same_bad, same_bad, same_bad])
    m, meta = repair_candidate(
        provider4, _contract(), _profile(), candidate,
        _report(with_failed=True), return_meta=True, max_rounds=1,
    )
    assert meta["model_calls"] == 1, "轮次上限后不再调用"
    assert meta.get("stopped_reason") == "rounds"

    # ---------- 预算耗尽 → 停止 ----------
    provider5 = ScriptedRepairProvider([_fixed_resp])
    m, meta = repair_candidate(
        provider5, _contract(), _profile(), candidate, _report(),
        return_meta=True, budget={"max_model_calls": 0},
    )
    assert meta["model_calls"] == 0, "预算耗尽不再调用"
    assert meta.get("stopped_reason") == "budget"
    assert m is candidate, "零调用返回原候选"

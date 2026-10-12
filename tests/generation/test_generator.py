"""T07：模型生成所有技能文件而非只写正文。

本项验收（工程计划 §5 T07，样本 B）：
1. 产物包含正文、脚本、模板且与方案（plan.files）引用一致；
2. 格式错误可有界请求纠正（修复轮次有限，超界失败）；
3. 文件截断 / 缺资源不能标生成成功；
4. 用户功能不丢（必要需求全部映射进产物清单）；
5. 哈希由系统计算而非采信模型自报；元数据记录提示版本与 usage。
"""
import json
from typing import Any

import pytest

from app.generation.contracts import (
    ArtifactKind,
    Confirmation,
    EngineeringProfile,
    Requirement,
    RequirementContract,
    SourceRef,
    SourceType,
)


# ---------- 假生成模型（记录调用、按脚本回放） ----------


class FakeGenerator:
    capabilities = __import__("app.llm.base", fromlist=["Capabilities"]).Capabilities(
        tools=False, streaming=False, json_mode=True, max_context_tokens=100_000
    )

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN202
        from app.llm import ModelResponse, Usage
        self.calls.append(list(messages))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        finish = "stop"
        if isinstance(item, dict) and item.get("_finish_reason"):
            finish = item.pop("_finish_reason")
        return ModelResponse(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
            usage=Usage(60, 120), finish_reason=finish,
        )


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
    def _req(req_id: str, content: str) -> Requirement:
        return Requirement(
            requirement_id=req_id, content=content, necessary=True,
            source=SourceRef(type=SourceType.USER_CONFIRMED, ref="msg-1"),
            confirmation=Confirmation.CONFIRMED,
        )
    return RequirementContract(
        request_id="flow-01AAAAAAAAAAAAAAAAAAAAAAAA",
        target_platform="codex",
        requirements=[_req("REQ-001", "按模板输出周报"), _req("REQ-002", "运行脚本处理 CSV")],
    )


def _plan():
    from app.generation.planning import build_skill_plan
    provider = FakeGenerator([{
        "plan_id": "plan-g",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "主技能",
             "requirement_ids": ["REQ-001"]},
            {"path": "scripts/extract.py", "kind": "text", "purpose": "CSV 提取",
             "requirement_ids": ["REQ-002"]},
            {"path": "references/template.md", "kind": "text", "purpose": "输出模板",
             "requirement_ids": ["REQ-001"]},
            {"path": "assets/logo.png", "kind": "binary", "purpose": "报告徽标",
             "requirement_ids": ["REQ-001"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "头信息完整", "requirement_ids": ["REQ-001"]},
            {"acceptance_id": "ACC-002", "kind": "execution",
             "description": "extract.py 可运行", "requirement_ids": ["REQ-002"]},
        ],
        "tool_notes": ["run_script 可用（.py，沙箱内）"],
        "blocking_gaps": [],
    }])
    return build_skill_plan(provider, _contract(), _profile())


LOGO = b"\x89PNG" + bytes(30)
RESOURCES = {"res-01": LOGO}


def _file_responses(**overrides: dict) -> list[dict]:
    """按 _plan() 的四个文件回放合法响应；overrides[key] 替换单个响应。"""
    base = [
        {"path": "SKILL.md", "kind": "text", "complete": True,
         "content": "---\nname: weekly-report\n---\n# 周报"},
        {"path": "scripts/extract.py", "kind": "text", "complete": True,
         "content": "import csv\n\ndef extract(p):\n    return list(csv.reader(open(p)))\n"},
        {"path": "references/template.md", "kind": "text", "complete": True,
         "content": "# 周报模板\n\n- 本周进展\n- 下周计划"},
        {"path": "assets/logo.png", "kind": "binary", "complete": True,
         "resource_id": "res-01"},
    ]
    for idx, ov in overrides.items():
        base[int(idx)] = ov
    return base


# ---------- 验收 1：正文 + 脚本 + 模板 + 二进制，引用一致 ----------


def test_generate_skill_with_script_reference_and_asset() -> None:
    from app.generation.generator import generate_candidate

    provider = FakeGenerator(_file_responses())
    manifest, meta = generate_candidate(
        provider, _contract(), _plan(), _profile(), RESOURCES, return_meta=True
    )

    by_path = {a.path: a for a in manifest.artifacts}
    # 正文、脚本、模板、二进制齐全，路径与方案一致（无发明、无遗漏）
    assert set(by_path) == {"SKILL.md", "scripts/extract.py",
                            "references/template.md", "assets/logo.png"}
    assert by_path["SKILL.md"].kind == ArtifactKind.TEXT
    assert "周报" in by_path["SKILL.md"].content
    assert by_path["scripts/extract.py"].content.startswith("import csv")
    assert by_path["references/template.md"].content.startswith("# 周报模板")
    assert by_path["assets/logo.png"].kind == ArtifactKind.BINARY
    assert by_path["assets/logo.png"].resource_id == "res-01"

    # 用户功能不丢：两个必要需求都映射进产物
    covered = {rid for a in manifest.artifacts for rid in a.requirement_ids}
    assert {"REQ-001", "REQ-002"} <= covered

    # 元数据：提示版本 + 模型调用数 + usage
    from app.generation.prompts import PROMPT_VERSION
    assert meta["prompt_version"] == PROMPT_VERSION
    assert meta["model_calls"] == 4
    assert meta["usage"]["prompt_tokens"] == 240  # 4 × 60
    assert meta["usage"]["completion_tokens"] == 480

    # 提示词包含可用资源 ID 与方案文件路径（模型只引用可用资源）
    for call in provider.calls:
        blob = " ".join(m.content or "" for m in call)
        assert "res-01" in blob
    assert "assets/logo.png" in "".join(
        m.content or "" for m in provider.calls[3]
    )


# ---------- 验收 2：哈希由系统计算（模型自报哈希不采信） ----------


def test_hash_computed_by_system_not_model_claim() -> None:
    import hashlib
    from app.generation.generator import generate_candidate

    # 模型谎报 sha256（全 0）
    lying = _file_responses()
    lying[0]["sha256"] = "0" * 64
    provider = FakeGenerator(lying)
    manifest = generate_candidate(provider, _contract(), _plan(), _profile(), RESOURCES)

    body = manifest.artifacts[0].content.encode("utf-8")
    assert manifest.artifacts[0].sha256 == hashlib.sha256(body).hexdigest()
    assert manifest.artifacts[0].sha256 != "0" * 64
    assert manifest.artifacts[3].sha256 == hashlib.sha256(LOGO).hexdigest()


# ---------- 验收 3：格式错误有界纠正 ----------


def test_format_error_bounded_repair() -> None:
    from app.generation.generator import MAX_REPAIR_ATTEMPTS, generate_candidate

    # 第 1 个文件首次返回非 JSON → 纠正请求后返回合法
    script = _file_responses()
    script[0] = "not json at all"
    script.insert(1, {"path": "SKILL.md", "kind": "text", "complete": True,
                      "content": "---\nname: weekly-report\n---\n# 周报"})
    provider = FakeGenerator(script)
    manifest = generate_candidate(provider, _contract(), _plan(), _profile(), RESOURCES)
    assert len(manifest.artifacts) == 4
    # 纠正请求注入了错误上下文
    assert any("格式" in (m.content or "") or "JSON" in (m.content or "")
               for m in provider.calls[1])

    # 超出修复边界 → 失败
    bad = _file_responses()
    for i in range(MAX_REPAIR_ATTEMPTS + 1):
        bad.insert(0, "still not json")
    provider2 = FakeGenerator(bad)
    from app.generation.generator import GenerationError
    with pytest.raises(GenerationError):
        generate_candidate(provider2, _contract(), _plan(), _profile(), RESOURCES)


# ---------- 验收 4：截断 / 缺资源不能标成功 ----------


def test_truncated_or_missing_resource_not_success() -> None:
    from app.generation.generator import GenerationError, generate_candidate

    # complete=false（模型自认未写完）
    trunc = _file_responses(**{"1": {
        "path": "scripts/extract.py", "kind": "text", "complete": False,
        "content": "import csv\n"}})
    with pytest.raises(GenerationError):
        generate_candidate(FakeGenerator(trunc), _contract(), _plan(), _profile(), RESOURCES)

    # finish_reason=length（模型侧截断）→ 截断响应不采信；修复轮仍截断 → 失败
    trunc2 = _file_responses()
    for i in range(3):
        trunc2.insert(0, {"_finish_reason": "length", "path": "SKILL.md",
                          "kind": "text", "complete": True, "content": "…" * 10})
    with pytest.raises(GenerationError):
        generate_candidate(FakeGenerator(trunc2), _contract(), _plan(), _profile(), RESOURCES)

    # 引用不存在的资源 ID → 失败（缺资源不标成功）
    missing = _file_responses(**{"3": {
        "path": "assets/logo.png", "kind": "binary", "complete": True,
        "resource_id": "res-404"}})
    with pytest.raises(GenerationError):
        generate_candidate(FakeGenerator(missing), _contract(), _plan(), _profile(), RESOURCES)

    # 模型把方案外文件塞进响应 → 拒绝（清单必须与方案一致）
    extra = _file_responses()
    extra[0] = {"path": "EVIL.md", "kind": "text", "complete": True, "content": "x"}
    with pytest.raises(GenerationError):
        generate_candidate(FakeGenerator(extra), _contract(), _plan(), _profile(), RESOURCES)


# ---------- 验收 5：必要功能丢失 → 拒绝 ----------


def test_missing_requirement_coverage_rejected() -> None:
    from app.generation.contracts import PlannedFile, SkillPlan
    from app.generation.generator import GenerationError, generate_candidate

    # 方案本身丢了 REQ-002（无文件覆盖该必要需求）→ 生成必须拒绝
    broken_plan = SkillPlan.model_validate({
        "plan_id": "plan-broken",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "主",
             "requirement_ids": ["REQ-001"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "头信息", "requirement_ids": ["REQ-001"]},
        ],
        "tool_notes": [], "blocking_gaps": [],
    })
    resp = [{"path": "SKILL.md", "kind": "text", "complete": True,
             "content": "# 周报", "requirement_ids": []}]
    with pytest.raises(GenerationError, match="REQ-002"):
        generate_candidate(FakeGenerator(resp), _contract(), broken_plan,
                           _profile(), RESOURCES)

    # 反向防御：模型在响应里丢掉 requirement_ids → 系统仍按方案映射，
    # 用户功能不丢（模型自报清单不采信）
    script = _file_responses()
    script[1] = {"path": "scripts/extract.py", "kind": "text", "complete": True,
                 "content": "import csv\n", "requirement_ids": []}
    manifest = generate_candidate(
        FakeGenerator(script), _contract(), _plan(), _profile(), RESOURCES
    )
    extract = next(a for a in manifest.artifacts if a.path == "scripts/extract.py")
    assert "REQ-002" in extract.requirement_ids

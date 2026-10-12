"""T05：生成可审查的文件与验收工程方案。

本项验收（工程计划 §5 T05）：
1. 每项必要功能至少有实现文件与验收项；
2. 工具真实可用性已标注；
3. 不需要脚本的技能不强加脚本；需要脚本的技能不退化成纯提示词；
4. 方案只描述将生成文件，不声称文件已存在（无 existence 声明）；
5. 无法满足的功能作为 blocking 缺口（plan.blocking_gaps）。

样本映射：A（纯文本技能不加脚本）、B（脚本+模板）、F（缺工具 → 缺口）。
"""
import json
from typing import Any

import pytest

from app.generation.contracts import (
    Confirmation,
    EngineeringProfile,
    Requirement,
    RequirementContract,
    SourceRef,
    SourceType,
)

# ---------- 假规划模型 ----------


class FakePlanner:
    capabilities = __import__("app.llm.base", fromlist=["Capabilities"]).Capabilities(
        tools=False, streaming=False, json_mode=True, max_context_tokens=100_000
    )

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN202
        from app.llm import ModelResponse, Usage
        self.calls.append(list(messages))
        return ModelResponse(
            content=json.dumps(self.script.pop(0), ensure_ascii=False),
            usage=Usage(50, 100), finish_reason="stop",
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


def _contract(requirements: list[Requirement]) -> RequirementContract:
    return RequirementContract(
        request_id="flow-01AAAAAAAAAAAAAAAAAAAAAAAA",
        target_platform="codex",
        requirements=requirements,
    )


def _req(req_id: str, content: str, confirmed: bool = True) -> Requirement:
    return Requirement(
        requirement_id=req_id,
        content=content,
        necessary=True,
        source=SourceRef(type=SourceType.USER_CONFIRMED if confirmed else SourceType.SYSTEM_INFERRED, ref="msg-1"),
        confirmation=Confirmation.CONFIRMED if confirmed else Confirmation.PENDING,
    )


# ---------- 验收 1：每项必要功能至少有实现文件与验收项 ----------


def test_every_required_feature_has_artifact_and_check() -> None:
    from app.generation.planning import build_skill_plan, plan_gaps

    provider = FakePlanner([{
        "plan_id": "plan-1",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "周报主技能",
             "requirement_ids": ["REQ-001", "REQ-002"]},
            {"path": "scripts/extract.py", "kind": "text", "purpose": "数据提取",
             "requirement_ids": ["REQ-002"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "SKILL.md 头信息完整", "requirement_ids": ["REQ-001"]},
            {"acceptance_id": "ACC-002", "kind": "execution",
             "description": "extract.py 可运行", "requirement_ids": ["REQ-002"]},
        ],
        "tool_notes": [],
        "blocking_gaps": [],
    }])
    contract = _contract([
        _req("REQ-001", "按模板输出周报 Markdown"),
        _req("REQ-002", "从 CSV 提取数据"),
    ])
    plan = build_skill_plan(provider, contract, _profile())

    # 全覆盖：两个必要功能都有实现文件与验收项
    assert plan_gaps(plan, contract) == []
    file_map = {r: set() for r in ("REQ-001", "REQ-002")}
    for f in plan.files:
        for rid in f.requirement_ids:
            file_map.setdefault(rid, set()).add(f.path)
    assert file_map["REQ-001"] and file_map["REQ-002"]

    # 缺验收的模型输出会被检出（验收映射缺口）
    provider_bad = FakePlanner([{
        "plan_id": "plan-2",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "主",
             "requirement_ids": ["REQ-001", "REQ-002"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "头信息", "requirement_ids": ["REQ-001"]},
        ],
        "tool_notes": [], "blocking_gaps": [],
    }])
    plan_bad = build_skill_plan(provider_bad, contract, _profile())
    gaps = plan_bad.gaps_for(contract)
    assert any("REQ-002" in g for g in gaps)


# ---------- 验收 2：工具真实可用性标注 ----------


def test_tool_availability_annotated() -> None:
    from app.generation.planning import build_skill_plan

    provider = FakePlanner([{
        "plan_id": "plan-3",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "主",
             "requirement_ids": ["REQ-001"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "头信息", "requirement_ids": ["REQ-001"]},
        ],
        "tool_notes": ["工作台提供 read_file/write_file/run_script（沙箱内）",
                       "无图像渲染工具（截图类需求需用户确认）"],
        "blocking_gaps": [],
    }])
    contract = _contract([_req("REQ-001", "纯文本周报")])
    plan = build_skill_plan(provider, contract, _profile())
    assert len(plan.tool_notes) == 2
    assert "run_script" in plan.tool_notes[0]
    # 提示词中注入了工具可用性事实
    assert any("工具" in (m.content or "") for m in provider.calls[0])


# ---------- 验收 3：不强加脚本 / 不退化为纯提示词 ----------


def test_plain_text_skill_no_script_imposed() -> None:
    """A 样本：纯文本格式技能 → 方案里没有 scripts/ 文件也合法。"""
    from app.generation.planning import build_skill_plan, plan_gaps

    provider = FakePlanner([{
        "plan_id": "plan-4",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "周报模板与步骤",
             "requirement_ids": ["REQ-001"]},
            {"path": "references/template.md", "kind": "text", "purpose": "周报模板",
             "requirement_ids": ["REQ-001"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "模板完整", "requirement_ids": ["REQ-001"]},
        ],
        "tool_notes": ["无需脚本：纯文本转换"],
        "blocking_gaps": [],
    }])
    contract = _contract([_req("REQ-001", "把素材整理成周报文本")])
    plan = build_skill_plan(provider, contract, _profile())
    assert not any(f.path.startswith("scripts/") for f in plan.files)
    assert plan_gaps(plan, contract) == []


def test_script_requiring_skill_not_degraded() -> None:
    """B 样本：明确要脚本的功能 → 方案含 scripts/ 且挂 execution 验收。"""
    from app.generation.planning import build_skill_plan

    provider = FakePlanner([{
        "plan_id": "plan-5",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "主技能",
             "requirement_ids": ["REQ-001"]},
            {"path": "scripts/extract.py", "kind": "text", "purpose": "CSV 提取",
             "requirement_ids": ["REQ-002"]},
            {"path": "references/template.md", "kind": "text", "purpose": "输出模板",
             "requirement_ids": ["REQ-001"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "头信息", "requirement_ids": ["REQ-001"]},
            {"acceptance_id": "ACC-002", "kind": "execution",
             "description": "extract.py 语法通过且可运行", "requirement_ids": ["REQ-002"]},
        ],
        "tool_notes": ["run_script 可用（.py/.sh/.ps1，沙箱内）"],
        "blocking_gaps": [],
    }])
    contract = _contract([
        _req("REQ-001", "按模板输出"),
        _req("REQ-002", "运行脚本处理 CSV"),
    ])
    plan = build_skill_plan(provider, contract, _profile())
    script_files = [f for f in plan.files if f.path.startswith("scripts/")]
    assert script_files, "需要脚本的功能不能退化成纯提示词"
    exec_accs = [a for a in plan.acceptances if a.kind == "execution"]
    assert exec_accs, "脚本文件必须配 execution 验收"


# ---------- 验收 4：方案只描述，不声称存在 ----------


def test_plan_does_not_claim_files_exist() -> None:
    from app.generation.planning import build_skill_plan

    provider = FakePlanner([{
        "plan_id": "plan-6",
        "files": [{"path": "SKILL.md", "kind": "text", "purpose": "x",
                   "requirement_ids": ["REQ-001"]}],
        "acceptances": [{"acceptance_id": "ACC-001", "kind": "static",
                         "description": "y", "requirement_ids": ["REQ-001"]}],
        "tool_notes": [], "blocking_gaps": [],
        "exists": True,  # 模型试图塞"已存在"声明
    }])
    plan = build_skill_plan(provider, _contract([_req("REQ-001", "x")]), _profile())
    # 模型输出多余字段被剥离：SkillPlan 没有 existence 语义
    assert "exists" not in plan.model_dump()
    assert plan.files[0].model_dump().keys() >= {"path", "kind", "purpose", "requirement_ids"}
    assert not set(plan.files[0].model_dump()) & {"exists", "created", "written"}


# ---------- 验收 5：无法满足的功能 → blocking 缺口 ----------


def test_unsatisfiable_feature_becomes_blocking_gap() -> None:
    """F 样本：用户坚持要 PNG 渲染但环境无工具 → blocking_gaps + 该功能不出现在文件映射。"""
    from app.generation.planning import build_skill_plan

    provider = FakePlanner([{
        "plan_id": "plan-7",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "主",
             "requirement_ids": ["REQ-001"]},
        ],
        "acceptances": [
            {"acceptance_id": "ACC-001", "kind": "static",
             "description": "头信息", "requirement_ids": ["REQ-001"]},
        ],
        "tool_notes": ["无图像渲染工具"],
        "blocking_gaps": ["REQ-003: PNG 渲染需要图像工具，环境不可用"],
    }])
    contract = _contract([
        _req("REQ-001", "周报文本"),
        _req("REQ-003", "渲染 PNG 截图"),
    ])
    plan = build_skill_plan(provider, contract, _profile())
    assert any("REQ-003" in g for g in plan.blocking_gaps)
    # 缺口功能不被文件映射掩盖
    assert all("REQ-003" not in f.requirement_ids for f in plan.files)


# ---------- 非法方案拒绝 ----------


def test_invalid_plan_rejected() -> None:
    from app.generation.planning import PlanBuildError, build_skill_plan

    # 非 JSON
    p1 = FakePlanner(["not json"])
    with pytest.raises(PlanBuildError):
        build_skill_plan(p1, _contract([_req("REQ-001", "x")]), _profile())

    # 重复文件路径
    p2 = FakePlanner([{
        "plan_id": "p",
        "files": [
            {"path": "SKILL.md", "kind": "text", "purpose": "a", "requirement_ids": ["REQ-001"]},
            {"path": "SKILL.md", "kind": "text", "purpose": "b", "requirement_ids": []},
        ],
        "acceptances": [{"acceptance_id": "ACC-001", "kind": "static",
                         "description": "y", "requirement_ids": ["REQ-001"]}],
        "tool_notes": [], "blocking_gaps": [],
    }])
    with pytest.raises(PlanBuildError):
        build_skill_plan(p2, _contract([_req("REQ-001", "x")]), _profile())


# ---------- 提示引用规则 ID + 元数据 ----------


def test_plan_prompt_references_rules_and_records_usage() -> None:
    from app.generation.planning import build_skill_plan
    from app.generation.prompts import PROMPT_VERSION

    provider = FakePlanner([{
        "plan_id": "plan-8",
        "files": [{"path": "SKILL.md", "kind": "text", "purpose": "x",
                   "requirement_ids": ["REQ-001"]}],
        "acceptances": [{"acceptance_id": "ACC-001", "kind": "static",
                         "description": "y", "requirement_ids": ["REQ-001"]}],
        "tool_notes": [], "blocking_gaps": [],
    }])
    plan, meta = build_skill_plan(
        provider, _contract([_req("REQ-001", "x")]), _profile(), return_meta=True
    )
    assert meta["prompt_version"] == PROMPT_VERSION
    assert meta["model_calls"] == 1
    assert any("SKILL_HEADER" in (m.content or "") for m in provider.calls[0])

"""工程方案生成（工程计划 §5 T05）。

职责：
- build_skill_plan：合同 + 生效规则 → SkillPlan（功能 → 文件 → 验收映射）；
- plan_gaps / SkillPlan.gaps_for：覆盖判定（每项必要功能至少一个实现文件
  + 一个验收项）；
- 方案只描述"将生成"的文件——SkillPlan 类型本身没有存在性语义，
  模型输出中的 exists/created 等多余字段在解析时剥离。

纪律：
- 提示注入工具真实可用性事实与生效规则 ID；
- 无法满足的功能 → blocking_gaps（不放进文件映射掩盖）；
- 模型输出非法 → PlanBuildError（拒绝而非猜测）。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.generation.contracts import (
    EngineeringProfile,
    RequirementContract,
    SkillPlan,
)
from app.generation.policy import effective_rules, profile_hash
from app.generation.prompts import PROMPT_VERSION
from app.llm import ChatMessage, ModelProvider

# 工作台工具真实可用性事实（提示注入，模型不得声称不存在的工具）
WORKBENCH_TOOL_FACTS = [
    "read_file / write_file / run_script 三工具可用（限沙箱目录内）",
    "run_script 支持 .py / .sh / .ps1，60 秒超时，输出截断",
    "write_file 仅接受文本扩展名白名单（md/txt/py/sh/ps1/json/yaml 等）",
    "无图像渲染、无网络下载、无数据库连接工具",
]

PLAN_SYSTEM = """你是 skill 工程规划引擎。根据需求合同生成工程方案（JSON）。

规则：
1. 只输出一个 JSON 对象，不要输出任何其他文字。
2. JSON 结构：
   {{
     "plan_id": "plan-<序号>",
     "files": [
       {{"path": "SKILL.md", "kind": "text", "purpose": "用途",
         "requirement_ids": ["REQ-001"]}}
     ],
     "acceptances": [
       {{"acceptance_id": "ACC-001", "kind": "static | execution | manual | host",
         "description": "验收描述", "requirement_ids": ["REQ-001"]}}
     ],
     "tool_notes": ["工具可用性说明"],
     "blocking_gaps": ["REQ-xxx: 无法满足的原因"]
   }}
3. 每项必要功能（necessary=true 且已确认）必须至少出现在一个文件的
   requirement_ids 和一个验收项的 requirement_ids 中。
4. 方案只描述"将要生成"的文件，不要声称文件已存在。
5. 纯文本技能不要强加 scripts/ 文件；需要脚本处理的功能（数据提取、
   格式转换等）必须规划 scripts/ 文件并配 execution 验收，不能退化成
   纯提示词描述。
6. 二进制资源只能引用已上传的 resource_id（已上传资源会列在输入中），
   不能计划生成二进制，也不能用外部下载链接。
7. 工具能力以"工作台工具事实"为准：缺工具的功能写进 blocking_gaps，
   格式 "REQ-xxx: 原因"。
8. 不得声称工具已执行——方案阶段没有任何执行。
"""


def plan_user_prompt(
    contract: RequirementContract, profile: EngineeringProfile
) -> str:
    """规划提示：合同 + 生效规则 + 工具事实。"""
    lines = [
        f"生效工程规则（profile={profile.profile_id} rev={profile.revision} "
        f"sha256={profile_hash(profile)[:12]}）："
    ]
    for rule in effective_rules(profile, contract):
        lines.append(f"- {rule.rule_id} [{rule.severity.value}] {rule.description}")

    lines.append("\n工作台工具事实（不得声称超出此范围的能力）：")
    lines.extend(f"- {fact}" for fact in WORKBENCH_TOOL_FACTS)

    lines.append("\n需求合同：")
    for req in contract.requirements:
        if not req.necessary:
            continue
        lines.append(
            f"- {req.requirement_id} (necessary={req.necessary}, "
            f"confirmed={req.confirmation.value}): {req.content}"
        )
    if contract.conflicts:
        lines.append("\n未解决冲突（方案必须显式处理，不能静默取舍）：")
        lines.extend(f"- {c}" for c in contract.conflicts)

    return "\n".join(lines)


class PlanBuildError(Exception):
    """模型方案输出非法（非 JSON / 结构不符 / 校验失败）。"""


def build_skill_plan(
    provider: ModelProvider,
    contract: RequirementContract,
    profile: EngineeringProfile,
    *,
    return_meta: bool = False,
) -> SkillPlan | tuple[SkillPlan, dict]:
    """从需求合同生成工程方案。"""
    response = provider.complete([
        ChatMessage(role="system", content=PLAN_SYSTEM),
        ChatMessage(role="user", content=plan_user_prompt(contract, profile)),
    ])
    raw = (response.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanBuildError(f"模型方案输出不是合法 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise PlanBuildError(f"模型方案输出不是 JSON 对象: {type(data).__name__}")

    try:
        plan = SkillPlan.model_validate(data)
    except ValidationError as exc:
        raise PlanBuildError(f"模型方案结构非法: {exc}") from exc

    if return_meta:
        usage = response.usage
        meta = {
            "prompt_version": PROMPT_VERSION,
            "model_calls": 1,
            "usage": ({"prompt_tokens": usage.prompt_tokens,
                       "completion_tokens": usage.completion_tokens}
                      if usage is not None else None),
            "usage_missing_calls": 0 if usage is not None else 1,
        }
        return plan, meta
    return plan


def plan_gaps(plan: SkillPlan, contract: RequirementContract) -> list[str]:
    """覆盖判定：必要功能无实现文件或无验收项 → 缺口描述列表。"""
    return plan.gaps_for(contract)

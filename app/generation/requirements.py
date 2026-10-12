"""自然语言需求提取与必要追问（工程计划 §5 T04）。

职责：
- extract_contract：模型结构化输出 → RequirementContract（校验后保存，
  非法结构拒绝而非猜测修复）；
- apply_answers：用户回答 → 合同修订（确认/放弃/补信息）；
- unresolved_gaps：必要缺口判定（pending 的必要功能）。

样本映射：
- E 完整需求 → 无 open_questions，一步出合同；
- F 工具缺失 → pending + open_questions → awaiting_input；
- G 附件指令 → attachment_text + 强制 pending（不可 confirmed）；
- H 冲突 → contract.conflicts 原样保留；
- D 续造 → existing_requirement_ids 保留旧号。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.generation.contracts import (
    Confirmation,
    EngineeringProfile,
    Requirement,
    RequirementContract,
    SourceType,
)
from app.generation.prompts import PROMPT_VERSION, EXTRACTION_SYSTEM, extraction_user_prompt
from app.llm import ChatMessage, ModelProvider

ATTACHMENT_FORCE_PENDING = "附件来源的需求必须经用户确认"


class ContractExtractionError(Exception):
    """模型结构化输出非法（非 JSON / 结构不符 / 校验失败）。"""


def extract_contract(
    provider: ModelProvider,
    request: dict[str, Any],
    profile: EngineeringProfile,
    *,
    return_meta: bool = False,
) -> RequirementContract | tuple[RequirementContract, dict]:
    """从自然语言请求提取需求合同。

    返回合同（或 (合同, 元数据)——元数据含提示版本/用量/调用数）。
    模型输出非法 → ContractExtractionError，绝不猜测修复。
    """
    system = EXTRACTION_SYSTEM
    user = extraction_user_prompt(request, profile)

    response = provider.complete([
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ])

    raw = (response.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractExtractionError(f"模型输出不是合法 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ContractExtractionError(f"模型输出不是 JSON 对象: {type(data).__name__}")

    payload = {
        "schema_version": "1",
        "request_id": str(request.get("request_id") or "req-local"),
        "target_platform": str(request.get("target_platform") or "codex"),
        "requirements": data.get("requirements") or [],
        "conflicts": data.get("conflicts") or [],
    }
    try:
        contract = RequirementContract.model_validate(payload)
    except ValidationError as exc:
        raise ContractExtractionError(f"模型输出的合同结构非法: {exc}") from exc

    # G：附件来源的需求强制 pending（不可 confirmed）——附件只是被分析内容
    fixed: list[Requirement] = []
    for req in contract.requirements:
        if req.source.type == SourceType.ATTACHMENT_TEXT and req.confirmation == Confirmation.CONFIRMED:
            req = req.model_copy(update={"confirmation": Confirmation.PENDING,
                                         "note": f"{req.note}｜{ATTACHMENT_FORCE_PENDING}".strip("｜")})
        fixed.append(req)
    contract = contract.model_copy(update={"requirements": fixed})

    open_questions = [str(q) for q in data.get("open_questions") or []]

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
        return contract.with_questions(open_questions), meta
    return contract.with_questions(open_questions)


def apply_answers(
    contract: RequirementContract, answers: dict[str, dict[str, Any]]
) -> RequirementContract:
    """用户回答 → 合同修订。

    answers 形如 {"REQ-002": {"necessary": false}} 或
    {"REQ-002": {"confirmation": "confirmed", "content": "补充后的描述"}}。
    回答只允许更新 necessary / confirmation / content / note，其余忽略。
    """
    revised: list[Requirement] = []
    for req in contract.requirements:
        ans = answers.get(req.requirement_id)
        if ans is None:
            revised.append(req)
            continue
        update: dict[str, Any] = {}
        if "necessary" in ans:
            update["necessary"] = bool(ans["necessary"])
        if "confirmation" in ans:
            update["confirmation"] = str(ans["confirmation"])
        if "content" in ans and ans["content"]:
            update["content"] = str(ans["content"])
        if "note" in ans and ans["note"]:
            update["note"] = str(ans["note"])
        if update:
            # 修订后的确认状态重新校验（枚举封闭）
            merged = req.model_dump()
            merged.update(update)
            req = Requirement.model_validate(merged)
        revised.append(req)

    # necessary=False 的功能按用户放弃处理：移除出需求列表（修订留痕于 note 集合）
    kept = [r for r in revised if r.necessary]
    return contract.model_copy(update={"requirements": kept})


def unanswered_gaps(contract: RequirementContract) -> list[str]:
    """仍需用户回答的问题（pending 必要功能存在时其关联问题才有效）。"""
    has_pending = any(
        r.necessary and r.confirmation == Confirmation.PENDING for r in contract.requirements
    )
    return list(contract.open_questions) if has_pending else []

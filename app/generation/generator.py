"""模型生成全部技能文件（工程计划 §5 T07）。

设计要点：
- 逐文件请求（分批生成，明确完成标志 complete），不拼接自由长文本；
- 格式错误/截断 → 有界纠正（MAX_REPAIR_ATTEMPTS 轮），超界即失败——
  文件截断、缺资源、方案外文件都不能标生成成功；
- 哈希由系统从内容/资源字节计算，模型自报 sha256 一律不采信；
- 模型只可引用请求中列出的可用资源 ID；
- 生成后做必要需求覆盖检查：用户功能不丢；
- 记录提示版本、模型调用数与 usage（元数据进流程检查点）。
"""
from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from app.generation.contracts import (
    ArtifactFile,
    ArtifactKind,
    ArtifactManifest,
    EngineeringProfile,
    PlannedFile,
    RequirementContract,
    SkillPlan,
)
from app.generation.prompts import (
    GENERATION_SYSTEM,
    PROMPT_VERSION,
    generation_repair_prompt,
    generation_user_prompt,
)
from app.llm.base import ChatMessage, ModelProvider

MAX_REPAIR_ATTEMPTS = 2  # 每个文件的纠正请求上限（首轮 + 2 轮纠正）


class GenerationError(Exception):
    """生成失败：格式错误超界 / 截断 / 缺资源 / 方案外文件 / 功能覆盖缺口。


    消息包含结构化上下文（文件路径 + 原因），进流程 error 字段。
    """


def _parse_file_response(raw: str | None, plan_file: PlannedFile) -> dict[str, Any]:
    """解析并校验单文件响应；非法时抛 ValueError（由调用方转纠正轮）。"""
    if not raw:
        raise ValueError("响应为空")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"响应不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("响应不是 JSON 对象")

    path = data.get("path")
    if path != plan_file.path:
        raise ValueError(f"path 与方案文件不一致（期望 {plan_file.path!r}，得到 {path!r}）")

    kind = data.get("kind")
    if kind == "text":
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("kind=text 但 content 缺失或为空")
        if data.get("resource_id"):
            raise ValueError("kind=text 不得携带 resource_id")
    elif kind == "binary":
        rid = data.get("resource_id")
        if not isinstance(rid, str) or not rid:
            raise ValueError("kind=binary 但 resource_id 缺失")
    else:
        raise ValueError(f"kind 非法: {kind!r}")

    if data.get("complete") is not True:
        raise ValueError("complete 不为 true（文件未完成或字段缺失）")

    return data


def _file_is_complete(response: Any) -> bool:
    """finish_reason=length 视为截断，不得采信其内容。"""
    return getattr(response, "finish_reason", None) != "length"


def generate_candidate(
    provider: ModelProvider,
    contract: RequirementContract,
    plan: SkillPlan,
    profile: EngineeringProfile,
    resources: dict[str, bytes],
    return_meta: bool = False,
):
    """按方案逐文件生成完整产物清单。

    返回 ArtifactManifest；return_meta=True 时返回 (manifest, meta)，
    meta 记录 prompt_version / model_calls / usage / provider。
    """
    resource_ids = list(resources)
    files: list[ArtifactFile] = []
    model_calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    usage_missing_calls = 0

    total = len(plan.files)
    for index, plan_file in enumerate(plan.files, start=1):
        user_prompt = generation_user_prompt(
            plan_file, contract, profile, index, total, resource_ids
        )
        messages = [
            ChatMessage(role="system", content=GENERATION_SYSTEM),
            ChatMessage(role="user", content=user_prompt),
        ]

        data: dict[str, Any] | None = None
        last_error = ""
        for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
            response = provider.complete(messages)
            model_calls += 1
            if response.usage is None:
                usage_missing_calls += 1
            else:
                prompt_tokens += response.usage.prompt_tokens
                completion_tokens += response.usage.completion_tokens

            if not _file_is_complete(response):
                last_error = "模型响应被截断（finish_reason=length）"
            else:
                try:
                    data = _parse_file_response(response.content, plan_file)
                    break
                except ValueError as exc:
                    data = None
                    last_error = str(exc)

            if attempt < MAX_REPAIR_ATTEMPTS:
                messages = messages + [
                    ChatMessage(role="assistant", content=response.content or ""),
                    ChatMessage(role="user", content=generation_repair_prompt(last_error)),
                ]

        if data is None:
            raise GenerationError(
                f"文件 {plan_file.path} 生成失败（{MAX_REPAIR_ATTEMPTS + 1} 次尝试）：{last_error}"
            )

        kind = ArtifactKind(data["kind"])
        if kind == ArtifactKind.TEXT:
            content = data["content"]
            digest = sha256(content.encode("utf-8")).hexdigest()
            files.append(
                ArtifactFile(
                    path=plan_file.path,
                    kind=kind,
                    sha256=digest,
                    purpose=plan_file.purpose,
                    requirement_ids=list(plan_file.requirement_ids),
                    content=content,
                )
            )
        else:
            rid = data["resource_id"]
            blob = resources.get(rid)
            if blob is None:
                raise GenerationError(
                    f"文件 {plan_file.path} 引用的资源不存在: {rid!r}（缺资源不得标生成成功）"
                )
            files.append(
                ArtifactFile(
                    path=plan_file.path,
                    kind=kind,
                    sha256=sha256(blob).hexdigest(),
                    purpose=plan_file.purpose,
                    requirement_ids=list(plan_file.requirement_ids),
                    resource_id=rid,
                )
            )

    manifest = ArtifactManifest(artifacts=files)

    # 用户功能不丢：必要需求必须被方案覆盖。覆盖 = 文件映射 ∪ 验收项映射
    # （约束型需求如"不得含密钥"没有实现文件，由验收项承担——真实链路
    # 抓到的缺陷：只看文件映射会把纯验证型必要需求误判为功能丢失）
    covered = {rid for f in files for rid in f.requirement_ids}
    covered |= {rid for a in plan.acceptances for rid in a.requirement_ids}
    missing = sorted(
        r.requirement_id
        for r in contract.requirements
        if r.necessary and r.requirement_id not in covered
    )
    if missing:
        raise GenerationError(f"必要需求未被产物覆盖（用户功能丢失）: {', '.join(missing)}")

    if return_meta:
        meta = {
            "prompt_version": PROMPT_VERSION,
            "model_calls": model_calls,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            } if not usage_missing_calls else None,
            "usage_observed": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "usage_missing_calls": usage_missing_calls,
            "provider": type(provider).__name__,
        }
        return manifest, meta
    return manifest

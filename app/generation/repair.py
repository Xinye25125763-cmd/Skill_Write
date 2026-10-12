"""证据驱动的有界自动修复（工程计划 §5 T15）。

repair_candidate：把失败检查的证据 + 实际文件内容交给模型，得到修订
文件（同 T07 的逐文件 JSON 纪律），重算系统哈希，保留 requirement_ids
映射（修复缺链接不删相关功能）。

有界纪律：
- 连续相同失败（同文件同内容 = 无进展）→ 停止并保存候选；
- 轮次（MAX_REPAIR_ROUNDS，可传 max_rounds 覆盖）或预算到达 → 停止；
- 停止原因显式进 meta（stopped_reason），不假装修好；
- 修复层拿不到测试套件/断言（签名不含），结构上不可能改断言刷分。

修复后的重新验收由调用方（flow 阶段任务）用新 manifest 重跑 T09/T12。
"""
from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from app.generation.contracts import (
    ArtifactFile,
    ArtifactManifest,
    CheckReport,
    EngineeringProfile,
    RequirementContract,
)
from app.generation.generator import GenerationError, _parse_file_response
from app.generation.prompts import GENERATION_SYSTEM, generation_repair_prompt
from app.llm.base import ChatMessage, ModelProvider

MAX_REPAIR_ROUNDS = 3  # 有界轮次上限（可配置覆盖）


class RepairStop(Exception):
    """有界停止信号（内部使用；结果经 meta 返回，不向外抛）。"""


def _hash_text(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _failure_evidence(report: CheckReport) -> list[str]:
    return [
        f"{c.check_id}（{c.status.value}）: {c.evidence}"
        for c in report.checks if c.status.value in ("failed", "not_run", "unsupported")
    ]


def repair_candidate(
    provider: ModelProvider,
    contract: RequirementContract,
    profile: EngineeringProfile,
    candidate: ArtifactManifest,
    report: CheckReport,
    *,
    return_meta: bool = False,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    budget: dict[str, Any] | None = None,
) -> ArtifactManifest | tuple[ArtifactManifest, dict[str, Any]]:
    """证据驱动的有界修复。

    返回（return_meta=True 时）(manifest, meta)：
    - meta: model_calls / usage / stopped_reason（"no_progress" |
      "rounds" | "budget" | ""=修完）/ rounds。
    - 修复无进展或到达边界 → 返回最后候选（保存，不无限循环）。
    """
    budget = budget or {}
    max_calls = budget.get("max_model_calls")

    failures = _failure_evidence(report)
    if not failures:
        meta = {"model_calls": 0, "usage": None, "stopped_reason": "", "rounds": 0}
        return (candidate, meta) if return_meta else candidate

    current = candidate
    meta: dict[str, Any] = {"model_calls": 0, "usage": None,
                            "stopped_reason": "", "rounds": 0}
    last_contents: dict[str, str] = {
        a.path: a.content or "" for a in current.artifacts
    }
    rounds_without_progress = 0

    for round_no in range(1, max_rounds + 1):
        if max_calls is not None and meta["model_calls"] >= int(max_calls):
            meta["stopped_reason"] = "budget"
            break

        round_changed = False
        budget_hit = False
        for artifact in list(current.artifacts):
            if artifact.kind.value != "text" or not _file_has_failures(report, artifact.path):
                continue
            if max_calls is not None and meta["model_calls"] >= int(max_calls):
                budget_hit = True
                break

            messages = _repair_messages(contract, profile, current, failures, artifact)
            resp = provider.complete(messages)
            meta["model_calls"] += 1
            u = getattr(resp, "usage", None)
            if u is not None:
                prev = meta.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0}
                meta["usage"] = {
                    "prompt_tokens": prev.get("prompt_tokens", 0) + getattr(u, "prompt_tokens", 0),
                    "completion_tokens": prev.get("completion_tokens", 0) + getattr(u, "completion_tokens", 0),
                }

            try:
                data = _parse_file_response(resp.content, _as_planned(artifact))
            except ValueError:
                continue  # 单文件解析失败：留原文件，下一轮证据仍会带上

            new_content = data["content"]
            if new_content != last_contents.get(artifact.path):
                round_changed = True
            last_contents[artifact.path] = new_content

            # 保留原映射（修复缺链接不删功能）；系统重算哈希
            updated = artifact.model_copy(update={
                "content": new_content,
                "sha256": _hash_text(new_content),
                "requirement_ids": artifact.requirement_ids,
            })
            current = ArtifactManifest(artifacts=[
                updated if a.path == artifact.path else a for a in current.artifacts
            ])
        if budget_hit:
            meta["stopped_reason"] = "budget"
            break
        meta["rounds"] = round_no

        # 失败检查指向的哈希已全部不在当前清单 → 已处理，交由调用方重新验收
        if _failures_addressed(report, current):
            break

        if round_changed:
            rounds_without_progress = 0
        else:
            rounds_without_progress += 1
            if rounds_without_progress >= 2:
                # 连续 2 轮无进展（同错误且无文件变化）→ 停止并保存候选
                meta["stopped_reason"] = "no_progress"
                break

    if not meta["stopped_reason"] and meta["rounds"] >= max_rounds \
            and not _failures_addressed(report, current):
        meta["stopped_reason"] = "rounds"

    return (current, meta) if return_meta else current


def _failures_addressed(report: CheckReport, manifest: ArtifactManifest) -> bool:
    """报告中的失败是否已被本轮修复处理（按 artifact_hash 判断）。

    哈希指向的文件已变 → 该失败交由调用方重新验收；
    无哈希的全局失败无从判断，不视为已处理。重新验收不在此层伪造。
    """
    hashes = {a.sha256 for a in manifest.artifacts}
    failed = [c for c in report.checks
              if c.status.value in ("failed", "not_run", "unsupported")]
    if not failed:
        return True
    if any(not c.artifact_hash for c in failed):
        return False
    return all(c.artifact_hash not in hashes for c in failed)


def _file_has_failures(report: CheckReport, path: str) -> bool:
    """检查报告里与该文件相关的失败（按 artifact_hash 或路径提及）。"""
    for c in report.checks:
        if c.status.value not in ("failed", "not_run", "unsupported"):
            continue
        if path in c.evidence or c.check_id.endswith(path.split("/")[-1]):
            return True
    return any(
        c.status.value in ("failed", "not_run", "unsupported") and not c.artifact_hash
        for c in report.checks
    )


def _as_planned(artifact: ArtifactFile):
    from app.generation.contracts import PlannedFile
    return PlannedFile.model_validate({
        "path": artifact.path, "kind": artifact.kind.value,
        "purpose": artifact.purpose or "修复", "requirement_ids": artifact.requirement_ids,
    })


def _repair_messages(
    contract: RequirementContract,
    profile: EngineeringProfile,
    candidate: ArtifactManifest,
    failures: list[str],
    artifact: ArtifactFile,
) -> list[ChatMessage]:
    """修复提示：失败证据 + 实际文件（不含测试断言——拿不到就改不了）。"""
    rules = "\n".join(
        f"- {r.rule_id}（{r.severity}）: {r.description}"
        for r in profile.rules
    ) or "-（无规则）"
    user = (
        f"以下技能文件未通过检查，请修复并完整重新输出该文件。\n\n"
        f"## 失败证据\n" + "\n".join(f"- {f}" for f in failures) + "\n\n"
        f"## 当前文件内容（{artifact.path}）\n```\n{artifact.content or ''}\n```\n\n"
        f"## 需求（不可删除任何功能映射）\n" + "\n".join(
            f"- {r.requirement_id}: {r.content}" for r in contract.requirements if r.necessary
        ) + "\n\n"
        f"## 工程规则\n{rules}\n\n"
        f"只输出该文件的 JSON（path/kind/complete/content），内容完整，"
        f"不得降低任何验收标准。"
    )
    return [
        ChatMessage(role="system", content=GENERATION_SYSTEM),
        ChatMessage(role="user", content=user),
    ]

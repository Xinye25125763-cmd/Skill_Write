"""v1.4 R3：盲评对照（需求文档 §4）。

动机：程序断言只能验证"格式/包含关系"，评价不了输出质量高低。
同一个模型以盲态（不知道哪个来自 candidate、哪个来自 baseline）
即可担任评审——不需要第二个人。

组件：
- blind_pair：A/B 匿名化（随机分配位置，映射关系只入库不进提示）；
- judge_blind：盲评调用（提示含原题 + 两份匿名输出 + 评维度，
  要求结构化裁决 preference + attribution；提示中不出现任何可暴露
  来源的措辞——没有 candidate/baseline/技能 等字样）；
- verify_attribution：归因防作弊——归因文字必须引用所偏好输出的
  实际内容（连续 ≥QUOTE_MIN 字符），防评审凭位置/长度等伪线索裁决；
- blind_summary：偏好分布汇总（平手不算 candidate 优势；多数平手/
  无有效裁决 → 结论 tie，证据不足不粉饰）。

诚实纪律：
- 单例盲评失败（模型返回不合 JSON / 归因引用不实）→ 该例记失败，
  不拖垮整组（与程序评分失败同语义）；
- 汇总结论 tie → eval:blind 折算为 not_run（证据不足）。
"""
from __future__ import annotations

import json
import random
import re
from typing import Any

from app.llm.base import ChatMessage, ModelProvider

# 归因引用的最小连续字符数（防"引用"过短沦为伪线索）
QUOTE_MIN = 6

# 归因文字的引文切分边界（引号 + 常见标点 + 空白）
_ATTRIB_SPLIT = re.compile(r"[「」『』\"'“”‘’·；;，,。.！!？?\s（）()\[\]【】：:|]+")


class BlindError(Exception):
    """盲评失败（模型回复不合 JSON、裁决字段非法等）。"""


# ---------- 匿名化 ----------

def blind_pair(
    rng: random.Random, candidate_output: str, baseline_output: str,
) -> tuple[str, str, bool]:
    """A/B 随机分配：返回 (输出A文本, 输出B文本, candidate_is_a)。

    映射关系（candidate_is_a）只入库供审计复验，绝不进评审提示。
    """
    candidate_is_a = rng.random() < 0.5
    if candidate_is_a:
        return candidate_output, baseline_output, True
    return baseline_output, candidate_output, False


# ---------- 盲评调用 ----------

def judge_blind(
    provider: ModelProvider, case_prompt: str,
    output_a: str, output_b: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """盲态评审一次：返回 (裁决, 记账元数据 model_calls/usage)。

    提示只含：任务原题、输出 A、输出 B、评维度（正确性/完整性/格式
    纪律）。不得出现 candidate / baseline / 技能 / 候选 等可暴露来源
    的措辞——两名执行者的身份对评审完全对称。
    """
    prompt = (
        "你是质量评审员。用户提出了一个任务，两名执行者分别给出了输出。\n\n"
        f"任务：{case_prompt}\n\n"
        f"输出 A：\n{output_a}\n\n"
        f"输出 B：\n{output_b}\n\n"
        "请从正确性、完整性、格式纪律三个维度评审，判断哪个输出更好地"
        "完成了任务；若两者质量相当，判平手。\n"
        '只输出 JSON：{"preference": "A"|"B"|"tie", '
        '"attribution": "<引用你偏好输出的原文片段并说明理由>"}\n'
        f"attribution 必须包含所偏好输出中至少 {QUOTE_MIN} 个连续字符的"
        "原文引用（平手则引用任一输出）。"
    )
    resp = provider.complete([
        ChatMessage(role="system", content="你是质量评审员，只输出 JSON。"),
        ChatMessage(role="user", content=prompt),
    ])
    meta: dict[str, Any] = {
        "model_calls": 1,
        "usage": {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        } if resp.usage is not None else None,
    }
    data = _extract_json(resp.content)
    if not isinstance(data, dict):
        raise BlindError("盲评回复不是 JSON 对象")
    preference = str(data.get("preference") or "").strip().lower()
    if preference not in ("a", "b", "tie"):
        raise BlindError(f"盲评 preference 非法: {preference!r}")
    attribution = str(data.get("attribution") or "").strip()
    if not attribution:
        raise BlindError("盲评归因为空")
    return {"preference": preference, "attribution": attribution}, meta


def _extract_json(text: str) -> Any:
    """从容错回复中提取 JSON（与 trigger 模块同策略）。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    brace = re.search(r"\{.*\}", text, re.S)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    raise BlindError(f"无法从盲评回复解析 JSON: {text[:120]!r}")


# ---------- 归因防作弊 ----------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def verify_attribution(
    attribution: str, output_a: str, output_b: str, preference: str,
) -> bool:
    """归因引用抽查：归因须引用所偏好输出的实际内容（≥QUOTE_MIN 连续字符）。

    - preference A/B → 引文必须出现在对应输出；
    - tie → 引文出现在任一输出即可（平手无单一偏好对象）；
    - 切分归因文字为片段，任一 ≥QUOTE_MIN 的片段命中即通过。
    """
    target = {
        "a": [_normalize(output_a)],
        "b": [_normalize(output_b)],
        "tie": [_normalize(output_a), _normalize(output_b)],
    }.get(preference)
    if target is None:
        return False
    attr_norm = _normalize(attribution)
    # 滑窗抽查：归因中任何 ≥QUOTE_MIN 的连续片段命中目标文本即算引用存在
    n = len(attr_norm)
    for start in range(n):
        seg = attr_norm[start:start + QUOTE_MIN]
        if len(seg) < QUOTE_MIN:
            break
        if any(seg in t for t in target):
            return True
    return False


# ---------- 汇总 ----------

def blind_summary(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """偏好分布汇总（纯函数，输入逐例裁决，输出统计与结论）。

    verdict 元素：{case_id, candidate_is_a, preference, attribution,
    attribution_ok, error}。error 或 attribution_ok=False → invalid
    （该例记评分失败，不计入胜平负）。

    结论语义（R3-6）：平手不算 candidate 优势；candidate 胜 == baseline
    胜（含全平手）→ 结论 tie（证据不足）；无有效裁决 → 结论 none。
    """
    valid = [v for v in verdicts if not v.get("error") and v.get("attribution_ok")]
    candidate_wins = 0
    baseline_wins = 0
    ties = 0
    for v in valid:
        pref = str(v.get("preference"))
        cand_is_a = bool(v.get("candidate_is_a"))
        if pref == "tie":
            ties += 1
        elif (pref == "a") == cand_is_a:
            candidate_wins += 1
        else:
            baseline_wins += 1
    if not valid:
        conclusion = "none"
    elif candidate_wins > baseline_wins:
        conclusion = "candidate"
    elif baseline_wins > candidate_wins:
        conclusion = "baseline"
    else:
        conclusion = "tie"
    return {
        "verdicts": len(verdicts),
        "valid": len(valid),
        "invalid": len(verdicts) - len(valid),
        "candidate_wins": candidate_wins,
        "baseline_wins": baseline_wins,
        "ties": ties,
        "conclusion": conclusion,
    }


def blind_check_status(summary: dict[str, Any]):  # noqa: ANN201
    """汇总结论 → eval:blind 检查状态（折算层用）。

    candidate 胜 → PASSED；baseline 胜 → FAILED；tie/none → NOT_RUN
    （证据不足，不粉饰；平手不算 candidate 优势）。
    """
    from app.generation.contracts import CheckStatus

    conclusion = summary.get("conclusion")
    if conclusion == "candidate":
        return CheckStatus.PASSED
    if conclusion == "baseline":
        return CheckStatus.FAILED
    return CheckStatus.NOT_RUN

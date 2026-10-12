"""v1.4 R1：触发验证与 description 有界优化（需求文档 §2）。

动机：五维验证证明"产物没坏"，不能证明"产物会被宿主触发"
（真实事故：5→1 召回塌缩）。本模块补第六维 trigger：
- build_trigger_cases：正例（should-trigger，含口语化/错别字）+
  负例（should-not-trigger，相邻领域）用例集；模型生成，失败降级
  为确定性回退（需求内容派生）；
- judge_trigger：模拟宿主选路——呈现候选技能列表（目标 + 干扰项，
  随机排序），判定目标是否被选中；
- run_trigger_verification：评分（召回率/误触率/总分）+ 有界迭代优化
  （≤3 轮改写 description，no_progress/rounds/length 三停止信号）。

诚实纪律：
- 判定失败的用例如实 failed（不粉饰）；
- 优化停止原因显式进 meta（stopped_reason），不假装修好；
- description 长度 ≤1024 字符，超长视为该轮失败。
"""
from __future__ import annotations

import json
import random
import re
from typing import Any

import yaml

from app.llm.base import ChatMessage, ModelProvider

# 触发验证参数（需求文档 §2.3；测试可传小值覆盖）
TRIGGER_CASES_TOTAL = 20
TRIGGER_HIT_CASES = 12
TRIGGER_SCORE_THRESHOLD = 0.85
MAX_DESCRIPTION_LEN = 1024
MAX_OPTIMIZE_ROUNDS = 3

# 相邻领域占位干扰项（项目内其他技能不足时填充）
PLACEHOLDER_DISTRACTORS: list[dict[str, str]] = [
    {"name": "data-viz-helper",
     "description": "制作数据图表、看板与可视化报告的技能，覆盖图表选型与排版。"},
    {"name": "meeting-notes-summarizer",
     "description": "把会议记录压缩成摘要与待办的技能，输出结构化纪要。"},
    {"name": "code-review-assistant",
     "description": "审查代码缺陷、风格与安全风险的技能，输出评审意见清单。"},
    {"name": "travel-planner",
     "description": "规划行程、比价与签证材料的技能，生成旅行方案。"},
]

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


class TriggerError(Exception):
    """触发验证失败（用例生成失败且无法回退等）。"""


# ---------- frontmatter ----------

def parse_frontmatter(skill_md: str) -> tuple[str, str]:
    """解析 SKILL.md frontmatter 的 name 与 description（缺失返回空串）。"""
    m = _FRONTMATTER_RE.match(skill_md or "")
    if m is None:
        return "", ""
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return "", ""
    if not isinstance(meta, dict):
        return "", ""
    name = meta.get("name")
    description = meta.get("description")
    return (name if isinstance(name, str) else "",
            description if isinstance(description, str) else "")


def rewrite_description(skill_md: str, new_description: str) -> str:
    """确定性替换 frontmatter 的 description（无 frontmatter 则补一个）。

    只动 description 行：name 等其余 frontmatter 字段原样保留。
    """
    if not new_description or not new_description.strip():
        raise TriggerError("新 description 为空")
    m = _FRONTMATTER_RE.match(skill_md or "")
    if m is None:
        front = yaml.safe_dump(
            {"name": "unnamed-skill", "description": new_description},
            allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        return (
            f"---\n{front}---\n\n"
            + (skill_md or "")
        )
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise TriggerError("SKILL.md frontmatter 不是有效 YAML，拒绝改写") from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise TriggerError("SKILL.md frontmatter 必须是 YAML 映射，拒绝改写")
    meta["description"] = new_description
    front = yaml.safe_dump(
        meta, allow_unicode=True, sort_keys=False, default_flow_style=False,
    )
    return skill_md[:m.start(1)] + front.rstrip("\n") + skill_md[m.end(1):]


# ---------- JSON 提取（容错） ----------

def _extract_json(text: str) -> Any:
    """从模型回复中提取第一个 JSON 对象/数组（容忍围栏与前后噪声）。"""
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
    brace = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    raise TriggerError(f"无法从模型回复解析 JSON: {text[:120]!r}")


# ---------- 用例生成 ----------

def _fallback_cases(contract: Any, skill_name: str, description: str,
                    total: int, hits: int) -> list[dict[str, Any]]:
    """确定性回退用例：正例派生自必要需求内容，负例用占位邻域提示。"""
    reqs: list[str] = []
    if contract is not None:
        reqs = [r.content for r in getattr(contract, "requirements", [])
                if getattr(r, "necessary", False)]
    if not reqs:
        reqs = [description or f"{skill_name} 的核心功能"]
    hit_prompts = [f"帮我{r[:40]}" for r in reqs[:hits]] or ["帮我处理这个任务"]
    while len(hit_prompts) < hits:
        hit_prompts.append(f"用{skill_name}处理：{hit_prompts[0]}")
    miss_pool = [
        "画个柱状图对比季度数据", "把昨天的会议录音整理成摘要",
        "看看这段代码有没有安全隐患", "帮我查去大阪的机票和行程",
    ]
    miss_prompts = miss_pool[: total - hits] or miss_pool[:1]
    cases: list[dict[str, Any]] = []
    for i, p in enumerate(hit_prompts[:hits]):
        cases.append({"id": f"trigger:hit:{i + 1:03d}", "expect": "hit", "prompt": p})
    for i, p in enumerate(miss_prompts[: total - hits]):
        cases.append({"id": f"trigger:miss:{i + 1:03d}", "expect": "miss", "prompt": p})
    return cases


def build_trigger_cases(
    provider: ModelProvider, contract: Any, skill_name: str, description: str,
    *, total: int = TRIGGER_CASES_TOTAL, hits: int = TRIGGER_HIT_CASES,
) -> tuple[list[dict[str, Any]], int]:
    """生成触发用例集（模型 1 次调用；失败降级确定性回退，model_calls=0）。

    返回 (cases, model_calls)。正例要求真实用户口吻（口语化/错别字/近义表达），
    负例要求相邻领域易混淆场景。
    """
    reqs = ""
    if contract is not None:
        reqs = "\n".join(
            f"- {r.requirement_id}: {r.content}"
            for r in getattr(contract, "requirements", [])
            if getattr(r, "necessary", False)
        )
    prompt = (
        "为以下技能生成触发验证用例。技能名："
        f"{skill_name}\ndescription：{description}\n\n必要需求：\n{reqs or '（无）'}\n\n"
        f"生成 {total} 条用例：{hits} 条 should-trigger 正例（真实用户口吻，"
        "包含口语化、错别字、近义表达，但不直接复述技能名），"
        f"{total - hits} 条 should-not-trigger 负例（相邻领域、易混淆但确实不该触发）。\n"
        '只输出 JSON：{"cases": [{"expect": "hit"|"miss", "prompt": "用户消息"}]}'
    )
    try:
        resp = provider.complete([
            ChatMessage(role="system", content="你是测试用例生成器，只输出 JSON。"),
            ChatMessage(role="user", content=prompt),
        ])
        data = _extract_json(resp.content)
        raw = data.get("cases") if isinstance(data, dict) else data
        if not isinstance(raw, list) or not raw:
            raise TriggerError("用例列表为空")
        cases: list[dict[str, Any]] = []
        for i, item in enumerate(raw[:total]):
            expect = "hit" if str(item.get("expect")) == "hit" else "miss"
            p = str(item.get("prompt") or "").strip()
            if not p:
                continue
            cases.append({"id": f"trigger:{expect}:{i + 1:03d}",
                          "expect": expect, "prompt": p})
        hits_got = sum(1 for c in cases if c["expect"] == "hit")
        if hits_got == 0 or hits_got == len(cases):
            raise TriggerError("正/负例比例失衡")
        return cases, 1
    except Exception:  # noqa: BLE001 生成失败 → 确定性回退（诚实降级）
        return _fallback_cases(contract, skill_name, description, total, hits), 0


# ---------- 触发判定 ----------

def judge_trigger(
    provider: ModelProvider, user_message: str,
    target_name: str, target_description: str,
    distractors: list[dict[str, str]],
    rng: random.Random,
) -> tuple[str | None, int]:
    """模拟宿主选路一次：返回 (被选中技能名或 None, model_calls)。"""
    options = [{"name": target_name, "description": target_description},
               *distractors]
    rng.shuffle(options)
    listing = "\n".join(
        f"- {o['name']}：{o['description']}" for o in options
    )
    prompt = (
        "你是技能路由器。用户发来一条消息，以下是可用技能列表：\n"
        f"{listing}\n\n用户消息：\"{user_message}\"\n\n"
        "判断这条消息应该触发哪个技能（只依据各技能的 description 与用户意图的匹配度）。"
        '只输出 JSON：{"selected": "<技能 name>"}；'
        '若没有任何技能应该被触发，输出 {"selected": null}'
    )
    resp = provider.complete([
        ChatMessage(role="system", content="你是技能路由器，只输出 JSON。"),
        ChatMessage(role="user", content=prompt),
    ])
    data = _extract_json(resp.content)
    selected = data.get("selected") if isinstance(data, dict) else None
    return (str(selected) if selected else None), 1


def score_trigger(
    cases: list[dict[str, Any]], selections: dict[str, str | None],
    target_name: str,
) -> dict[str, float | int]:
    """评分：召回率（正例命中）、误触率（负例误选）、总分（正确决策比例）。"""
    hits = [c for c in cases if c["expect"] == "hit"]
    misses = [c for c in cases if c["expect"] == "miss"]
    hit_ok = sum(1 for c in hits if selections.get(c["id"]) == target_name)
    miss_ok = sum(1 for c in misses if selections.get(c["id"]) != target_name)
    recall = hit_ok / len(hits) if hits else 0.0
    false_rate = (len(misses) - miss_ok) / len(misses) if misses else 0.0
    overall = (hit_ok + miss_ok) / len(cases) if cases else 0.0
    return {"recall": recall, "false_rate": false_rate, "overall": overall,
            "hits": len(hits), "misses": len(misses),
            "hit_ok": hit_ok, "miss_ok": miss_ok}


# ---------- 有界迭代优化 ----------

def _optimize_description_call(
    provider: ModelProvider, description: str,
    failures: list[str], skill_name: str,
) -> tuple[str, int]:
    """一次 description 改写调用：失败用例作为证据。"""
    prompt = (
        f"技能 {skill_name} 的 description 触发不准确，请改写。\n"
        f"当前 description：\n{description}\n\n"
        "以下用户消息本应触发该技能但没触发（正例失败）：\n"
        + "\n".join(f"- {f}" for f in failures if not f.startswith("误触"))
        + "\n\n以下用户消息不该触发却被误触（负例失败）：\n"
        + "\n".join(f"- {f}" for f in failures if f.startswith("误触"))
        + "\n\n改写要求：更准确覆盖正例场景、排除负例场景；保持具体、"
        f"可判断；总长不超过 {MAX_DESCRIPTION_LEN} 字符。\n"
        '只输出 JSON：{"description": "<新 description>"}'
    )
    resp = provider.complete([
        ChatMessage(role="system", content="你是技能描述优化器，只输出 JSON。"),
        ChatMessage(role="user", content=prompt),
    ])
    data = _extract_json(resp.content)
    new_desc = str(data.get("description") or "").strip() if isinstance(data, dict) else ""
    if not new_desc:
        raise TriggerError("模型未返回有效 description")
    return new_desc, 1


def run_trigger_verification(
    provider: ModelProvider,
    skill_md: str,
    contract: Any,
    distractors: list[dict[str, str]],
    *,
    threshold: float = TRIGGER_SCORE_THRESHOLD,
    total_cases: int = TRIGGER_CASES_TOTAL,
    hit_cases: int = TRIGGER_HIT_CASES,
    max_rounds: int = MAX_OPTIMIZE_ROUNDS,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """完整触发验证：生成用例 → 评分 → 有界优化 → 重评。

    返回 {cases, selections, score, initial_score, final_description,
    description_changed, rounds, stopped_reason, model_calls}。

    stopped_reason：""=达标 | "rounds"=轮次耗尽 | "no_progress"=改写无进展
    | "length"=改写超长。停止后如实返回当前最佳结果，不假装修好。
    """
    rng = rng or random.Random()
    skill_name, description = parse_frontmatter(skill_md)
    if not skill_name:
        raise TriggerError("SKILL.md frontmatter 缺少 name，无法做触发验证")

    cases, calls = build_trigger_cases(
        provider, contract, skill_name, description,
        total=total_cases, hits=hit_cases,
    )

    def _judge_all(desc: str) -> tuple[dict[str, str | None], int]:
        selections: dict[str, str | None] = {}
        n = 0
        for c in cases:
            sel, k = judge_trigger(
                provider, c["prompt"], skill_name, desc, distractors, rng
            )
            selections[c["id"]] = sel
            n += k
        return selections, n

    selections, n = _judge_all(description)
    calls += n
    initial = score_trigger(cases, selections, skill_name)
    best_desc = description
    best_score = initial
    best_selections = selections
    stopped_reason = ""
    rounds = 0

    if initial["overall"] < threshold:
        for _ in range(max_rounds):
            rounds += 1
            failures = [
                (f"误触（不该触发却选中了本技能）：{c['prompt']}"
                 if selections.get(c["id"]) == skill_name
                 else f"未触发（应触发却没选中）：{c['prompt']}")
                for c in cases
                if (c["expect"] == "hit") != (selections.get(c["id"]) == skill_name)
            ]
            try:
                new_desc, k = _optimize_description_call(
                    provider, best_desc, failures, skill_name
                )
                calls += k
            except TriggerError:
                stopped_reason = "no_progress"
                break
            if len(new_desc) > MAX_DESCRIPTION_LEN:
                stopped_reason = "length"
                break
            if new_desc == best_desc:
                stopped_reason = "no_progress"
                break
            selections, n = _judge_all(new_desc)
            calls += n
            sc = score_trigger(cases, selections, skill_name)
            if sc["overall"] > best_score["overall"]:
                best_desc, best_score, best_selections = new_desc, sc, selections
            if sc["overall"] >= threshold:
                stopped_reason = ""
                break
        else:
            stopped_reason = "rounds"
        if stopped_reason == "" and best_score["overall"] < threshold:
            stopped_reason = "rounds"

    return {
        "skill_name": skill_name,
        "cases": cases,
        "selections": best_selections,
        "score": best_score,
        "initial_score": initial,
        "initial_description": description,
        "final_description": best_desc,
        "description_changed": best_desc != description,
        "rounds": rounds,
        "stopped_reason": stopped_reason,
        "model_calls": calls,
    }

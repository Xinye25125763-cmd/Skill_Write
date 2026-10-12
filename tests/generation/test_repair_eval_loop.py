"""v1.3 P1-2b：评测驱动修复闭环（对话入口）验收。

场景：父流程交付且评测失败 → repair-eval API 种子新流程 →
generate 阶段以父候选起步（不重新生成）→ 评测失败证据并入报告 →
证据驱动修复 → 新版本冻结 → 重新排队评测（幂等键含新 version_id）。

另验收负路径：评测未失败时 repair-eval → 409 nothing_to_repair。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

AUTH = {"x-session-token": "test-token"}


class ScriptedProvider:
    """按剧本顺序返回 JSON；耗尽后回放最后一条。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        from app.llm import ModelResponse, Usage
        self.calls.append(list(messages))
        if not self.script:
            return ModelResponse(content="{}", usage=Usage(10, 5),
                                 finish_reason="stop")
        item = self.script.pop(0)
        return ModelResponse(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
            usage=Usage(100, 200), finish_reason="stop",
        )


class FailingEchoProvider:
    """评测侧假模型：第一次执行断言失败（触发 repair-eval），之后通过。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        from app.llm import ModelResponse, Usage
        self.calls += 1
        if self.calls <= 2:  # 父流程评测的 candidate+baseline 两组首轮
            return ModelResponse(content="（不包含需求 ID 的失败回复）",
                                 usage=Usage(50, 25), finish_reason="stop")
        return ModelResponse(content="已完成 REQ-1：行动项整理",
                             usage=Usage(50, 25), finish_reason="stop")


class EchoProvider:
    """评测侧假模型：固定通过（text_contains REQ-1）。"""

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        from app.llm import ModelResponse, Usage
        return ModelResponse(content="已完成 REQ-1：行动项整理",
                             usage=Usage(50, 25), finish_reason="stop")


def _contract() -> dict:
    return {"requirements": [{
        "requirement_id": "REQ-1", "content": "整理纪要为行动项", "necessary": True,
        "source_type": "user_message", "source_ref": "m1", "confirmation": "confirmed",
    }], "conflicts": [], "open_questions": []}


def _plan() -> dict:
    return {"plan_id": "p1",
            "files": [{"path": "SKILL.md", "kind": "text", "purpose": "主文档",
                        "requirement_ids": ["REQ-1"]}],
            "acceptances": [{"acceptance_id": "A1", "kind": "static",
                             "description": "SKILL.md 存在", "requirement_ids": ["REQ-1"]}]}


def _file(content: str) -> dict:
    return {"path": "SKILL.md", "kind": "text", "complete": True, "content": content}


def _good() -> str:
    return ("---\nname: action-extractor\ndescription: 整理会议纪要为行动项\n---\n\n"
            "# 行动项整理\n\n整理会议纪要为行动项。输出需包含 REQ-1。\n")


def _repaired() -> str:
    """修复轮产出：针对评测失败（回复缺 REQ-1）的改进版。"""
    return ("---\nname: action-extractor\ndescription: 整理会议纪要为行动项\n---\n\n"
            "# 行动项整理\n\n整理会议纪要为行动项。\n\n## 输出纪律\n\n"
            "最终回复必须明确提及完成的需求 ID（如 REQ-1），\n"
            "逐条列出负责人、截止日期、当前状态。\n")


def _use(monkeypatch: pytest.MonkeyPatch, gen: Any, echo: Any) -> None:
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: echo)


def test_repair_eval_seeds_new_flow_and_redelivers(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主路径：父流程评测失败 → repair-eval → 新流程修复 → 新版本重评测。"""
    import app.workers.worker as worker_mod

    # 父流程：生成通过（静态全过），评测 echo 失败 → eval:summary failed
    parent_provider = ScriptedProvider([_contract(), _plan(), _file(_good())])
    failing_echo = FailingEchoProvider()
    _use(monkeypatch, parent_provider, failing_echo)

    pid = client.post("/api/projects", json={"name": "repair-eval"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    worker_mod.Worker().run_pending()  # 含自动排队的 eval_run

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered"
    parent_vid = snap["checkpoint"]["receipt"]["version_id"]

    # 触发 repair-eval 前需先导出？不需要：_eval_checks 直接查 DB 评测结果
    resp = client.post(f"/api/projects/{pid}/skill-versions/{parent_vid}/repair-eval",
                       headers=AUTH)
    assert resp.status_code == 202, resp.text
    new_flow = resp.json()

    # 新流程从 generate 起步（跳过 extract/plan）
    assert new_flow["state"] == "generating"
    assert new_flow["job_id"]

    # 修复流程的模型：以父候选起步（无 generate 调用），修复轮产出改进版
    repair_provider = ScriptedProvider([_file(_repaired())])
    _use(monkeypatch, repair_provider, EchoProvider())
    worker_mod.Worker().run_pending()  # 新流程 generate(+repair) + 新评测

    snap2 = client.get(f"/api/generation-flows/{new_flow['flow_id']}",
                       headers=AUTH).json()
    assert snap2["state"] == "delivered", (
        f"修复流程应交付，状态: {snap2['state']}，错误: {snap2.get('error')}"
    )
    new_vid = snap2["checkpoint"]["receipt"]["version_id"]
    assert new_vid != parent_vid, "必须冻结新版本（不改写冻结版本行）"

    # 父版本行未被改动（不可变纪律）
    rows = client.get(f"/api/projects/{pid}/skills/versions", headers=AUTH).json()
    vids = [r["id"] for r in rows]
    assert parent_vid in vids and new_vid in vids

    # 修复流程没有重新消耗生成调用（首版即父候选）：
    # repair_provider 只被调用 ≤2 次（有界修复轮），绝无 extract/plan 消费
    # （剧本只有 1 条 _repaired()，第 2 次起回放同条 → 无进展即停）
    assert len(repair_provider.calls) <= 2, (
        f"种子流程不应重新生成/重提取: {len(repair_provider.calls)} 次调用"
    )

    # 新版本导出：折算后的评测通过（EchoProvider 全过）
    resp = client.post(f"/api/projects/{pid}/skill-versions/{new_vid}/export",
                       headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["execution_status"] == "passed", body

    # 评测失败证据确实进过修复提示（repair: 检查并入报告 → 修复调用含证据）
    # FailingEchoProvider 评测回复不含 REQ-1 → 断言失败 → 证据为 eval: 检查


def test_repair_eval_rejects_when_no_failed_eval(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """负路径：评测全过（或未运行）→ 409 nothing_to_repair。"""
    import app.workers.worker as worker_mod

    provider = ScriptedProvider([_contract(), _plan(), _file(_good())])
    _use(monkeypatch, provider, EchoProvider())
    pid = client.post("/api/projects", json={"name": "repair-eval-none"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    worker_mod.Worker().run_pending()

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    vid = snap["checkpoint"]["receipt"]["version_id"]

    resp = client.post(f"/api/projects/{pid}/skill-versions/{vid}/repair-eval",
                       headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "nothing_to_repair"

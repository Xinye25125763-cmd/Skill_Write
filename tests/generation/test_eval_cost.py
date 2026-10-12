"""v1.5 M4：成本入收据验收（skill-creator 的"成本可见性"作业）。

- eval_run 视图新增 cost 段（candidate/baseline/total tokens）；
- eval:summary 证据文本带 token 消耗；
- 导出收据（execution 维度）自然携带该证据。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.llm import ModelResponse, Usage
from app.workers.worker import Worker

AUTH = {"x-session-token": "test-token"}


class GenScriptedProvider:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        if not self.script:
            return ModelResponse(content="{}", usage=Usage(10, 5),
                                 finish_reason="stop")
        item = self.script.pop(0)
        return ModelResponse(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
            usage=Usage(100, 200), finish_reason="stop",
        )


class PricedAgentProvider:
    """执行侧假模型：candidate/baseline 输出可区分、usage 可辨识。

    candidate（首轮含 SKILL.md）每次 3000+300；baseline 每次 1000+100。
    """

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        first = str(messages[0].content or "")
        if "SKILL.md" in first:
            return ModelResponse(content="已完成 REQ-1：行动项整理",
                                 usage=Usage(3_000, 300), finish_reason="stop")
        return ModelResponse(content="已完成 REQ-1：行动项整理",
                             usage=Usage(1_000, 100), finish_reason="stop")


class PricedJudgeProvider:
    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        return ModelResponse(
            content=json.dumps({"preference": "tie", "attribution": "行动项整理"},
                               ensure_ascii=False),
            usage=Usage(700, 70), finish_reason="stop",
        )


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


def _good_skill() -> str:
    return ("---\nname: action-extractor\n"
            "description: 把会议纪要整理为行动项表格。\n---\n\n"
            "# 行动项整理\n\n整理会议纪要为行动项。输出需包含 REQ-1。\n")


def test_eval_cost_visible_in_view_and_receipt(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主路径：视图 cost 段 + eval:summary 证据 + 导出收据三处可见。"""
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.eval_run as er_mod
    import app.workers.handlers.generation_stage as gs_mod

    gen = GenScriptedProvider([_contract(), _plan(),
                               {"path": "SKILL.md", "kind": "text",
                                "complete": True, "content": _good_skill()}])
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    agent = PricedAgentProvider()
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: agent)
    monkeypatch.setattr(er_mod, "judge_provider_factory", PricedJudgeProvider)

    pid = client.post("/api/projects", json={"name": "m4-cost"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    Worker().run_pending()
    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered", snap.get("error")
    vid = snap["checkpoint"]["receipt"]["version_id"]

    # 1) run 视图 cost 段
    runs_list = client.get(f"/api/projects/{pid}/eval-runs", headers=AUTH).json()
    assert runs_list, "应有一次评测运行"
    run_id = runs_list[0]["id"]
    view = client.get(f"/api/eval-runs/{run_id}", headers=AUTH).json()
    cost = view["cost"]
    # 内建套件 1 用例 × 2 组 + 1 次盲评裁判调用
    assert cost["candidate_tokens"] == 3_300, cost
    assert cost["baseline_tokens"] == 1_100, cost
    assert cost["judge_tokens"] == 770, cost
    assert cost["total_tokens"] == 5_170, cost

    # 2) 导出收据：eval:summary 证据含成本
    resp = client.post(f"/api/projects/{pid}/skill-versions/{vid}/export",
                       headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    with zipfile.ZipFile(Path(body["zip_path"])) as zf:
        report = json.loads(zf.read("delivery/report.json"))
    assert body["execution_status"] == "passed", body
    summary_check = next(c for c in report["checks"]
                         if c["check_id"] == "eval:summary")
    assert "成本：本次评测消耗 5,170 tokens" in summary_check["evidence"]
    assert "candidate 3,300" in summary_check["evidence"]
    assert "baseline 1,100" in summary_check["evidence"]
    assert "judge 770" in summary_check["evidence"]

"""审计修复 A1+A3：宿主验证接入导出链路 + e2e 断言收紧。

验收（审计项）：
- export 时按 target_platform 调用宿主适配器，host: checks 并入 CheckReport；
- 宿主不可用 → host 维度 not_run（candidate），绝不 passed、绝不 FAILED；
- API 注入记录（source != codex-cli）→ host 用例 FAILED；
- 宿主验证作为独立 check 写入（不改动冻结版本行，验证是导出时刻的行为）；
- e2e：收据必须带 host 维度状态（非 None），DELIVERED 判定必须含 host。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

AUTH = {"x-session-token": "test-token"}


# ---------- 与 test_end_to_end 一致的剧本模型 ----------


class ScriptedProvider:
    capabilities = None  # type: ignore[assignment]

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        from app.llm import ModelResponse, Usage
        if not self.script:
            return ModelResponse(content="{}", usage=Usage(10, 5), finish_reason="stop")
        item = self.script.pop(0)
        return ModelResponse(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
            usage=Usage(100, 200), finish_reason="stop",
        )


class _EchoProvider:
    """评测侧假模型：固定回复含需求 ID（满足 text_contains 断言）。"""

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        from app.llm import ModelResponse, Usage
        return ModelResponse(
            content="已完成 REQ-1：行动项整理",
            usage=Usage(50, 25), finish_reason="stop",
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


def _file() -> dict:
    return {"path": "SKILL.md", "kind": "text", "complete": True,
            "content": "# 行动项整理\n\n整理会议纪要为行动项。\n"}


class _UnavailableAdapter:
    """宿主不可用：CLI 缺失。"""

    platform = "codex"

    def validate_delivery(self, receipt: Any, cases: list[dict]) -> Any:
        from app.generation.contracts import CheckReport, CheckResult, CheckStatus
        checks = [CheckResult(
            check_id="host:codex:available", status=CheckStatus.NOT_RUN,
            evidence="blocked：环境缺少 codex CLI（不可用），宿主验证不运行",
        )]
        return CheckReport(checks=checks)

    def capabilities(self) -> dict:
        return {"platform": "codex", "verified": False, "native_trigger": True}


class _InjectedAdapter:
    """记录被 API 注入污染：source != codex-cli。"""

    platform = "codex"

    def validate_delivery(self, receipt: Any, cases: list[dict]) -> Any:
        from app.generation.contracts import CheckReport, CheckResult, CheckStatus
        checks = [
            CheckResult(check_id="host:codex:available", status=CheckStatus.PASSED,
                        evidence="codex CLI 探活成功"),
            CheckResult(
                check_id="host:codex:case:trigger-positive",
                status=CheckStatus.FAILED,
                evidence="宿主执行源不合法: source='api'（要求 'codex-cli'），"
                         "API 注入结果不能冒充原生触发",
            ),
        ]
        return CheckReport(checks=checks)

    def capabilities(self) -> dict:
        return {"platform": "codex", "verified": False, "native_trigger": True}


def _make_delivered_flow(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                         request_text: str = "把会议纪要整理成行动项") -> tuple[str, str]:
    """跑完一条 delivered 流程，返回 (pid, version_id)。"""
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod
    import app.workers.worker as worker_mod

    provider = ScriptedProvider([_contract(), _plan(), _file()])
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: provider)
    # v1.3 P1-2：交付时自动排对照评测（eval_run 走 agent_run 执行器），
    # 注入回声模型让两组用例执行完成且断言全过
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: _EchoProvider())

    pid = client.post("/api/projects", json={"name": "host-int"}, headers=AUTH).json()["id"]
    resp = client.post(
        f"/api/projects/{pid}/generation-flows",
        json={"request_text": request_text}, headers=AUTH,
    )
    assert resp.status_code == 202
    flow_id = resp.json()["flow_id"]
    worker_mod.Worker().run_pending()

    snap = client.get(f"/api/generation-flows/{flow_id}", headers=AUTH).json()
    assert snap["state"] == "delivered", snap
    version_id = snap["checkpoint"]["receipt"]["version_id"]
    return pid, version_id


def test_host_validation_wired_into_export_and_delivery_status(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1：export 必须执行宿主验证并把 host 维度写进收据。"""
    import app.api.generation as gen_api

    pid, version_id = _make_delivered_flow(client, monkeypatch)

    # 块1：宿主不可用 → host=not_run，收据 candidate，notes 如实说明
    monkeypatch.setattr(gen_api, "get_adapter", lambda platform: _UnavailableAdapter())
    resp = client.post(
        f"/api/projects/{pid}/skill-versions/{version_id}/export", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_status"] == "not_run", body
    assert body["delivery_status"] == "candidate", (
        "宿主不可用时不得标 delivered——完整验收未完成"
    )
    assert any("host" in n for n in body["notes"])

    # ZIP 里 receipt.json 也必须带 host_status（导出时刻的验证收据）
    with zipfile.ZipFile(body["zip_path"]) as zf:
        receipt_json = json.loads(zf.read("delivery/receipt.json"))
        assert receipt_json["host_status"] == "not_run"
        report_json = json.loads(zf.read("delivery/report.json"))
        host_checks = [c for c in report_json["checks"]
                       if c["check_id"].startswith("host:")]
        assert host_checks, "宿主验证 checks 必须进入导出报告"

    # 块2：API 注入 → host=failed（安全违规必须阻塞 delivered）
    monkeypatch.setattr(gen_api, "get_adapter", lambda platform: _InjectedAdapter())
    resp = client.post(
        f"/api/projects/{pid}/skill-versions/{version_id}/export", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_status"] == "failed"
    assert body["delivery_status"] == "candidate"

    # 块3：其他维度全绿 + host=passed → 才允许 delivered。
    # 把版本行的冻结报告换成全 PASSED（content_hash 只覆盖技能文件，不受影响），
    # 验证导出时刻的 delivered 判定必须同时看宿主维度。
    from app.generation.contracts import CheckReport, CheckResult, CheckStatus
    from app.core.db import create_db_engine, db_path
    from app.core.config import load_config
    from sqlalchemy.orm import Session

    clean_report = CheckReport(checks=[
        CheckResult(check_id="validation:all_files", status=CheckStatus.PASSED,
                    evidence="全部文件在磁盘且哈希一致"),
        CheckResult(check_id="validation:skill_header", status=CheckStatus.PASSED,
                    evidence="SKILL.md 含 YAML front matter"),
        CheckResult(check_id="coverage:REQ-1", status=CheckStatus.PASSED,
                    evidence="verified: 位置 SKILL.md; 验收 A1=passed"),
    ])
    cfg = load_config()
    with Session(create_db_engine(db_path(cfg))) as s:
        from app.models import SkillVersion
        vrow = s.get(SkillVersion, version_id)
        vrow.report_json = clean_report.model_dump_json()
        s.commit()

    class _PassingAdapter:
        platform = "codex"

        def validate_delivery(self, receipt: Any, cases: list[dict]) -> CheckReport:
            return CheckReport(checks=[
                CheckResult(check_id="host:codex:available", status=CheckStatus.PASSED,
                            evidence="codex CLI 探活成功"),
                CheckResult(check_id="host:codex:case:trigger-positive",
                            status=CheckStatus.PASSED,
                            evidence="[codex-cli] hit=True"),
                CheckResult(check_id="host:codex:case:trigger-negative",
                            status=CheckStatus.PASSED,
                            evidence="[codex-cli] 未命中（hit=False）"),
            ])

        def capabilities(self) -> dict:
            return {"platform": "codex", "verified": True, "native_trigger": True}

    monkeypatch.setattr(gen_api, "get_adapter", lambda platform: _PassingAdapter())
    resp = client.post(
        f"/api/projects/{pid}/skill-versions/{version_id}/export", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_status"] == "passed", body
    assert body["delivery_status"] == "delivered", (
        f"全维度含宿主都通过才 delivered: static={body['static_status']} "
        f"exec={body['execution_status']} cov={body['coverage_status']} "
        f"notes={body['notes']}"
    )
    with zipfile.ZipFile(body["zip_path"]) as zf:
        receipt_json = json.loads(zf.read("delivery/receipt.json"))
        assert receipt_json["delivery_status"] == "delivered"

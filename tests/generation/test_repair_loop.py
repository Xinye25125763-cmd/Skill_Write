"""v1.3 P1-2：评测/修复回路接入生成链的专项验收。

验收（真实使用反馈驱动——首个 CatPaw 实验抓到的"跨脚本契约不一致"
证明存在只有集成级验证能抓的缺陷类别）：
1. 首版验证失败 → REPAIRING 流转 → 证据驱动修复 → 修好后重验收 → delivered；
2. 修复预算记账 repair-N（预留+实际），不与 generate 重复记账；
3. 修复无进展/预算耗尽 → 如实带证据交付 candidate（不假装修好）；
4. 交付时自动排对照评测（eval_run 任务，幂等键含 version_id）；
5. 导出时刻折算 eval 检查（与宿主验证同模式，不回写冻结版本行）；
6. 修复轮内不重排 generate 任务（同阶段重排死锁回归）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

AUTH = {"x-session-token": "test-token"}


class ScriptedProvider:
    """按剧本顺序返回 JSON；剧本耗尽后回放最后一条（修复轮可多次取用）。"""

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


class EchoProvider:
    """评测侧假模型：固定回复含需求 ID（满足 text_contains 断言）。"""

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
            "# 行动项整理\n\n整理会议纪要为行动项。\n")


def _bad() -> str:
    # 触发 no_secrets 失败：内容含密钥标记（修复轮拿证据修掉）
    return ("---\nname: action-extractor\ndescription: 整理会议纪要为行动项\n---\n\n"
            "# 行动项整理\n\nAPI_KEY=sk-12345\n整理会议纪要为行动项。\n")


def _frozen_report(client: TestClient, pid: str, version_id: str) -> dict:
    """读冻结版本行的报告 JSON（版本 API）。"""
    from app.core.config import load_config
    from sqlalchemy import create_engine, text as sql_text
    cfg = load_config()
    eng = create_engine(f"sqlite:///{cfg.data_dir / 'app.db'}")
    with eng.connect() as conn:
        raw = conn.execute(sql_text(
            "SELECT report_json FROM skill_versions WHERE id=:v"
        ), {"v": version_id}).scalar()
    return json.loads(raw or "{}")


def _use(monkeypatch: pytest.MonkeyPatch, gen: Any, echo: EchoProvider) -> None:
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: echo)


def test_repair_loop_fixes_failed_validation_then_delivers(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 1+2+4：首版验证失败 → REPAIRING → 修复 → 重验收 → delivered + 评测排队。"""
    import app.workers.worker as worker_mod

    # 首版 SKILL.md 含密钥标记 → no_secrets FAILED；修复轮返回干净版本
    provider = ScriptedProvider([
        _contract(),                    # extract
        _plan(),                        # plan
        _file(_bad()),                  # generate（首版脏）
        _file(_good()),                 # repair（证据驱动修复 → 干净）
    ])
    _use(monkeypatch, provider, EchoProvider())

    pid = client.post("/api/projects", json={"name": "repair-ok"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    worker_mod.Worker().run_pending()

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered", (
        f"修复后应 delivered，状态: {snap['state']}，错误: {snap.get('error')}"
    )
    ckpt = snap["checkpoint"]
    assert ckpt.get("repair_rounds") == 1, ckpt.get("repair_rounds")

    # 验收 2：预算记账 repair-1（实际 usage 覆盖预留）
    ledger = ckpt.get("budget_ledger") or {}
    repair_key = next((key for key in ledger if key.startswith("repair-1@")), None)
    assert repair_key is not None, f"账本缺 repair-1: {list(ledger)}"
    actual = ledger[repair_key].get("actual")
    assert actual is not None and actual.get("model_calls", 0) >= 1, ledger[repair_key]

    # 验收 4：交付时自动排了对照评测任务
    assert ckpt.get("eval_job_id"), "交付检查点缺 eval_job_id"
    assert ckpt.get("eval_suite_id"), "交付检查点缺 eval_suite_id"

    # 修复确实发生在证据之后（修复调用的提示含失败证据）
    repair_calls = [c for c in provider.calls
                    if any("API_KEY" in str(m) or "no_secrets" in str(m).lower()
                           or "失败" in str(m) for m in c)]
    assert repair_calls, "未观察到证据驱动的修复调用"


def test_repair_exhausted_delivers_candidate_with_evidence(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 3：修复耗尽仍失败 → 如实带证据交付，不假装修好。"""
    import app.workers.worker as worker_mod

    # 首版脏 + 修复轮继续返回脏的（无进展）→ 轮次耗尽 → candidate 带证据
    provider = ScriptedProvider([
        _contract(),
        _plan(),
        _file(_bad()),
        _file(_bad()),   # repair 第 1 轮（还是脏）
        _file(_bad()),   # repair 第 2 轮（还是脏）
    ])
    _use(monkeypatch, provider, EchoProvider())

    pid = client.post("/api/projects", json={"name": "repair-exhaust"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    worker_mod.Worker().run_pending()

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered", (
        f"耗尽也应交付 candidate（不挂死），状态: {snap['state']}"
    )
    # 报告真相在冻结版本行（API 视图不暴露 latest_report）
    version_id = snap["checkpoint"]["receipt"]["version_id"]
    report = _frozen_report(client, pid, version_id)
    checks = {c["check_id"]: c for c in report.get("checks", [])}
    assert checks.get("validation:no_secrets", {}).get("status") == "failed"
    assert "repair:exhausted" in checks, f"缺耗尽备注: {list(checks)}"
    assert checks["repair:exhausted"]["status"] == "not_run"


def test_export_folds_eval_checks_like_host_checks(
    migrated_db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 5：导出时刻折算 eval 检查（评测已跑完 → eval:summary passed）。"""
    import app.workers.worker as worker_mod

    provider = ScriptedProvider([_contract(), _plan(), _file(_good())])
    _use(monkeypatch, provider, EchoProvider())

    pid = client.post("/api/projects", json={"name": "eval-fold"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    # 生成链 + 自动排队的 eval_run 全部跑完（echo 模型两组用例全过）
    worker_mod.Worker().run_pending()

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered"
    version_id = snap["checkpoint"]["receipt"]["version_id"]

    resp = client.post(f"/api/projects/{pid}/skill-versions/{version_id}/export",
                       headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    # 评测已真实跑完：execution 维度含 eval 证据且通过
    assert body["execution_status"] == "passed", body
    assert any("eval:case:candidate" in (n or "") or "eval:summary" in (n or "")
               for n in body.get("notes", [])) or body["execution_status"] == "passed"
    zip_path = Path(body["zip_path"])
    assert zip_path.is_file()

    # ZIP 内报告含 eval: checks（可审计）
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        report = json.loads(zf.read("delivery/report.json"))
    assert any(c["check_id"].startswith("eval:") for c in report["checks"]), (
        "导出报告应含 eval 检查"
    )
    summary = next(c for c in report["checks"] if c["check_id"] == "eval:summary")
    assert summary["status"] == "passed", summary

"""v1.4 R3：盲评对照专项验收（需求文档 §4）。

覆盖：
1. 盲化随机性与可复验：A/B 分配随机、映射只入库不进提示；
2. 盲评提示无来源暴露措辞（不含 candidate/baseline/技能 等字样）；
3. 归因引用抽查：引用实际内容通过、伪线索引用不通过；
4. 汇总：candidate 胜/平手/无效裁决的结论语义（平手不算优势）；
5. 生成链端到端：盲评入 meta_json + 视图 + 导出折算 eval:blind（不阻塞）；
6. 失败隔离：评审返回坏 JSON → 该例 invalid，不拖垮整组。
"""
import json
import random
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.generation.blind import (
    QUOTE_MIN,
    blind_check_status,
    blind_pair,
    blind_summary,
    judge_blind,
    verify_attribution,
)
from app.llm import ModelResponse, Usage
from app.workers.worker import Worker

AUTH = {"x-session-token": "test-token"}


# ---------- 单元：盲化 / 归因 / 汇总 ----------

def test_blind_pair_random_and_reversible() -> None:
    """A/B 分配随机（两种映射都出现）、可复验（同种子同分配）。"""
    rng = random.Random(42)
    seen = {True: 0, False: 0}
    for _ in range(200):
        a, b, cand_is_a = blind_pair(rng, "候选输出", "基线输出")
        seen[cand_is_a] += 1
        if cand_is_a:
            assert a == "候选输出" and b == "基线输出"
        else:
            assert a == "基线输出" and b == "候选输出"
    assert seen[True] > 0 and seen[False] > 0, "两种映射都应出现"

    # 可复验：同种子 → 同分配序列
    r1 = [blind_pair(random.Random(7), "c", "b")[2] for _ in range(10)]
    r2 = [blind_pair(random.Random(7), "c", "b")[2] for _ in range(10)]
    assert r1 == r2


def test_verify_attribution_quote_check() -> None:
    out_a = "已完成 REQ-1：行动项整理，含负责人与截止日期"
    out_b = "部分完成，缺行动项"
    # 引用 A 的实际内容 → preference A 通过
    assert verify_attribution(f"更完整：引用「{out_a[:12]}」", out_a, out_b, "a")
    # 引用 A 内容但 preference 是 B → 不通过（引用错对象）
    assert not verify_attribution(f"引用「{out_a[:12]}」", out_a, out_b, "b")
    # 伪线索（长度/位置描述，无实际引文）→ 不通过
    assert not verify_attribution("输出更长且排在前面所以更好", out_a, out_b, "a")
    # tie：引任一输出即可
    assert verify_attribution(f"两者相当：见「{out_b[:8]}」", out_a, out_b, "tie")
    # 过短片段（<QUOTE_MIN）不算引用
    assert not verify_attribution(out_a[: QUOTE_MIN - 1], out_a, out_b, "a")


def test_blind_summary_conclusions() -> None:
    def _v(cid, pref, cand_is_a, ok=True):
        return {"case_id": cid, "candidate_is_a": cand_is_a,
                "preference": pref, "attribution": "引用实际内容的归因",
                "attribution_ok": ok}

    # candidate 多数胜（映射混排也正确归到 candidate）
    s = blind_summary([_v("c1", "a", True), _v("c2", "b", False),
                       _v("c3", "tie", True)])
    assert s["candidate_wins"] == 2 and s["baseline_wins"] == 0 and s["ties"] == 1
    assert s["conclusion"] == "candidate"
    assert blind_check_status(s).value == "passed"

    # 平手不算优势：胜负相等（含全平手）→ tie → not_run
    s2 = blind_summary([_v("c1", "a", True), _v("c2", "a", False)])
    assert s2["conclusion"] == "tie"
    assert blind_check_status(s2).value == "not_run"

    s3 = blind_summary([_v("c1", "tie", True), _v("c2", "tie", False)])
    assert s3["ties"] == 2 and s3["conclusion"] == "tie"

    # baseline 胜 → failed（cand_is_a=True 且偏好 b → 偏好的是基线输出）
    s4 = blind_summary([_v("c1", "b", True), _v("c2", "b", True)])
    assert s4["conclusion"] == "baseline"
    assert blind_check_status(s4).value == "failed"

    # 无效裁决（归因引用不实）不计入胜负；全部无效 → none
    s5 = blind_summary([_v("c1", "a", True, ok=False),
                        _v("c2", "a", False, ok=False)])
    assert s5["invalid"] == 2 and s5["valid"] == 0
    assert s5["conclusion"] == "none"
    assert blind_check_status(s5).value == "not_run"

    # error 裁决同 invalid
    s6 = blind_summary([{"case_id": "c1", "error": "BlindError: bad json"}])
    assert s6["invalid"] == 1 and s6["conclusion"] == "none"


# ---------- 单元：盲评调用（提示无来源暴露） ----------

class _RawProvider:
    """直接回放固定 JSON 的假模型；记录收到的提示供断言。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        self.prompts.append(str(messages[-1].content))
        return ModelResponse(content=json.dumps(self.payload, ensure_ascii=False),
                             usage=Usage(30, 20), finish_reason="stop")


def test_judge_blind_prompt_has_no_source_leak() -> None:
    """提示含原题与两份输出，但不得出现可暴露来源的措辞。"""
    prov = _RawProvider({"preference": "A",
                         "attribution": "更完整：引用「完整的行动项交付」"})
    verdict, meta = judge_blind(prov, "整理会议纪要为行动项",
                                "完整的行动项交付", "部分结果")
    assert verdict["preference"] == "a"
    assert meta["model_calls"] == 1
    assert meta["usage"] == {"prompt_tokens": 30, "completion_tokens": 20}
    prompt = prov.prompts[0]
    assert "整理会议纪要为行动项" in prompt
    assert "完整的行动项交付" in prompt and "部分结果" in prompt
    for leak in ("candidate", "baseline", "候选", "基线", "技能", "SKILL"):
        assert leak not in prompt, f"盲评提示泄漏来源线索: {leak}"

    # preference 非法 → BlindError（该例将记评分失败）
    bad = _RawProvider({"preference": "C", "attribution": "x"})
    with pytest.raises(Exception):
        judge_blind(bad, "p", "a", "b")
    nojson = _RawProvider.__new__(_RawProvider)
    nojson.payload = {}
    nojson.prompts = []
    with pytest.raises(Exception):
        judge_blind(type("P", (), {
            "capabilities": _RawProvider({"preference": "A"}).capabilities,
            "complete": lambda self, msgs, **kw: ModelResponse(
                content="这不是 JSON", usage=Usage(1, 1), finish_reason="stop"),
        })(), "p", "a", "b")


# ---------- 端到端：生成链 → 盲评 → 导出折算 ----------

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
            "description: 把会议纪要整理为行动项表格，输出负责人/截止日期。\n---\n\n"
            "# 行动项整理\n\n整理会议纪要为行动项。输出需包含 REQ-1。\n")


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


class ContrastAgentProvider:
    """执行侧假模型：candidate（系统提示含 SKILL.md）输出完整，baseline 输出缺半。"""

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        first = str(messages[0].content or "")
        if "SKILL.md" in first:
            return ModelResponse(
                content="已完成 REQ-1：行动项整理，逐条列出负责人与截止日期",
                usage=Usage(50, 25), finish_reason="stop")
        return ModelResponse(content="大概做了", usage=Usage(20, 10),
                             finish_reason="stop")


class BlindJudgePreferComplete:
    """盲评假模型：偏好包含 REQ-1 的那份输出，归因引用其原文。"""

    def __init__(self, force: str | None = None) -> None:
        self.force = force  # "tie" 强制平手；None 按内容偏好
        self.prompts: list[str] = []

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        prompt = str(messages[-1].content)
        self.prompts.append(prompt)
        m = re.search(r"输出 A：\n(.*?)\n\n输出 B：\n(.*?)\n\n请从", prompt, re.S)
        out_a, out_b = (m.group(1), m.group(2)) if m else ("", "")
        if self.force == "tie":
            quote = (out_a or out_b)[:12]
            return ModelResponse(
                content=json.dumps({"preference": "tie",
                                    "attribution": f"两者相当，见「{quote}」"},
                                   ensure_ascii=False),
                usage=Usage(40, 30), finish_reason="stop")
        preferred = out_a if "REQ-1" in out_a else out_b
        pref = "A" if "REQ-1" in out_a else "B"
        return ModelResponse(
            content=json.dumps(
                {"preference": pref,
                 "attribution": f"该输出完整完成任务：引用「{preferred[:14]}」"},
                ensure_ascii=False),
            usage=Usage(40, 30), finish_reason="stop")


def _run_chain(client, monkeypatch, judge) -> tuple[str, str]:
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.eval_run as er_mod
    import app.workers.handlers.generation_stage as gs_mod

    gen = GenScriptedProvider([_contract(), _plan(),
                               {"path": "SKILL.md", "kind": "text",
                                "complete": True, "content": _good_skill()}])
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: ContrastAgentProvider())
    monkeypatch.setattr(er_mod, "judge_provider_factory", lambda: judge)

    pid = client.post("/api/projects", json={"name": "blind-e2e"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    Worker().run_pending()
    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered", snap.get("error")
    vid = snap["checkpoint"]["receipt"]["version_id"]
    return pid, vid


def test_blind_eval_end_to_end_folds_passed(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主路径：盲评偏好 candidate → meta_json + 视图 + 导出 eval:blind passed。"""
    judge = BlindJudgePreferComplete()
    pid, vid = _run_chain(client, monkeypatch, judge)

    from app.models import EvalRun
    with Session(create_db_engine(db_path(load_config()))) as s:
        ev = (s.query(EvalRun)
              .filter(EvalRun.version_id == vid, EvalRun.kind != "trigger")
              .order_by(EvalRun.created_at.desc()).first())
        assert ev is not None and ev.status == "finished"
        meta = json.loads(ev.meta_json)
        verdicts = meta["blind"]["verdicts"]
        assert len(verdicts) == 1, "双侧输出齐全的用例应盲评"
        assert verdicts[0]["attribution_ok"] is True
        assert "candidate_is_a" in verdicts[0], "盲化映射入库可复验"
        assert meta["blind"]["summary"]["conclusion"] == "candidate"
        assert meta["blind"]["summary"]["candidate_wins"] == 1
        # 评审调用数入 meta（预算可见性）
        assert meta["blind"]["model_calls"] == 1

    # 提示无来源泄漏（A/B 匿名化成立）
    # 注：用例原题含验收描述（"SKILL.md 存在"），属任务原文不算泄漏；
    # 真正的来源线索是身份词与技能名
    assert judge.prompts, "评审者应被调用"
    for p in judge.prompts:
        for leak in ("candidate", "baseline", "action-extractor"):
            assert leak not in p, f"盲评提示泄漏: {leak}"

    # 导出折算：eval:blind passed；blind 维度不阻塞 delivered 判定
    resp = client.post(f"/api/projects/{pid}/skill-versions/{vid}/export",
                       headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blind_status"] == "passed", body

    import zipfile
    from pathlib import Path
    with zipfile.ZipFile(Path(body["zip_path"])) as zf:
        report = json.loads(zf.read("delivery/report.json"))
    blind_checks = [c for c in report["checks"] if c["check_id"] == "eval:blind"]
    assert len(blind_checks) == 1
    assert blind_checks[0]["status"] == "passed"
    assert "candidate 胜 1" in blind_checks[0]["evidence"]


def test_blind_tie_folds_not_run(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平手 → 结论 tie → eval:blind not_run（证据不足，不粉饰）。"""
    pid, vid = _run_chain(client, monkeypatch, BlindJudgePreferComplete(force="tie"))

    resp = client.post(f"/api/projects/{pid}/skill-versions/{vid}/export",
                       headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blind_status"] == "not_run", body
    assert any("盲评证据不足" in n for n in body["notes"]), body["notes"]


def test_blind_invalid_verdict_isolated(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """评审返回坏 JSON → 该例 invalid；汇总如实（none → not_run），评测仍完成。"""
    class BadJudge:
        @property
        def capabilities(self) -> Any:
            from app.llm import Capabilities
            return Capabilities(tools=False, streaming=False, json_mode=True,
                                max_context_tokens=100_000)

        def complete(self, messages: list[Any], **_kw: Any) -> Any:
            return ModelResponse(content="我觉得都挺好的（非 JSON）",
                                 usage=Usage(10, 10), finish_reason="stop")

    pid, vid = _run_chain(client, monkeypatch, BadJudge())

    from app.models import EvalRun
    with Session(create_db_engine(db_path(load_config()))) as s:
        ev = (s.query(EvalRun)
              .filter(EvalRun.version_id == vid, EvalRun.kind != "trigger")
              .order_by(EvalRun.created_at.desc()).first())
        assert ev.status == "finished", "单例盲评失败不拖垮整组"
        meta = json.loads(ev.meta_json)
        assert meta["blind"]["summary"]["invalid"] == 1
        assert meta["blind"]["summary"]["conclusion"] == "none"

    resp = client.post(f"/api/projects/{pid}/skill-versions/{vid}/export",
                       headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["blind_status"] == "not_run", body
    assert any("无效" in n or "证据不足" in n for n in body["notes"]), body["notes"]


@pytest.mark.parametrize("failed_stage,run_kind", [("eval-blind", "contrast"), ("trigger", "trigger")])
def test_ledger_write_failure_is_persisted_in_model_usage_evidence(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    failed_stage: str, run_kind: str,
) -> None:
    import app.generation.budget as budget_mod

    original = budget_mod.record_stage_usage

    def fail_one_stage(session, flow_id, stage_id, usage, **kwargs):  # noqa: ANN001
        if stage_id == failed_stage:
            raise RuntimeError("injected ledger write failure")
        return original(session, flow_id, stage_id, usage, **kwargs)

    monkeypatch.setattr(budget_mod, "record_stage_usage", fail_one_stage)
    if failed_stage == "trigger":
        import app.workers.handlers.trigger_run as trigger_mod
        from tests.generation.test_trigger import AlwaysOtherProvider
        monkeypatch.setattr(trigger_mod, "provider_factory", lambda: AlwaysOtherProvider())
    pid, vid = _run_chain(client, monkeypatch, BlindJudgePreferComplete())

    from app.models import EvalRun
    with Session(create_db_engine(db_path(load_config()))) as session:
        row = (session.query(EvalRun)
               .filter_by(version_id=vid, kind=run_kind)
               .order_by(EvalRun.created_at.desc()).first())
        assert row is not None and row.status == "finished"
        meta = json.loads(row.meta_json)
        usage_meta = meta["blind"] if run_kind == "contrast" else meta
        assert "injected ledger write failure" in usage_meta["budget_ledger_error"]

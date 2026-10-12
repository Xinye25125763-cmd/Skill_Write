"""v1.4 R1：触发验证与 description 有界优化专项验收（需求文档 §2.4）。

覆盖：
1. frontmatter 解析/改写（含无 frontmatter 补齐）；
2. 评分：正/负例判定、召回率/误触率/总分；
3. 有界迭代：三停止信号（no_progress / length / rounds）与达标路径；
4. 生成链端到端：交付时自动排 trigger_run → EvalRun(kind=trigger) +
   逐例结果落库 → 导出折算第六维（未运行 → not_run 不阻塞导出）。
"""
import json
import random
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.generation.trigger import (
    MAX_DESCRIPTION_LEN,
    parse_frontmatter,
    rewrite_description,
    run_trigger_verification,
    score_trigger,
)
from app.llm import ModelResponse, Usage
from app.workers.worker import Worker

AUTH = {"x-session-token": "test-token"}

SKILL_MD = (
    "---\nname: action-extractor\n"
    "description: 把会议纪要整理为行动项表格，输出负责人/截止日期。\n"
    "---\n\n# 行动项整理\n\n整理会议纪要为行动项。\n"
)

DISTRACTORS = [
    {"name": "data-viz", "description": "制作数据图表与看板"},
    {"name": "travel", "description": "规划旅行行程"},
]


class TriggerProvider:
    """剧本假模型：第 1 个调用是用例生成，其后按回调逐条判定/改写。"""

    def __init__(self, gen_cases: list[dict], decide, rewrite=None) -> None:
        self.gen_cases = gen_cases
        self.decide = decide
        self.rewrite = rewrite
        self.calls: list[str] = []

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        user_msgs = [m for m in messages if getattr(m, "role", "") == "user"]
        text = (user_msgs[-1].content if user_msgs
                else messages[-1].content) or ""
        self.calls.append(text)
        if "生成触发验证用例" in text:
            return ModelResponse(
                content=json.dumps({"cases": self.gen_cases}, ensure_ascii=False),
                usage=Usage(50, 50), finish_reason="stop",
            )
        if "改写" in text and "新 description" in text:
            if self.rewrite is None:
                raise AssertionError("未预期的改写调用")
            new_desc = self.rewrite(len(self.calls))
            return ModelResponse(
                content=json.dumps({"description": new_desc}, ensure_ascii=False),
                usage=Usage(50, 50), finish_reason="stop",
            )
        selected = self.decide(text)
        return ModelResponse(
            content=json.dumps({"selected": selected}, ensure_ascii=False),
            usage=Usage(10, 10), finish_reason="stop",
        )


def _cases(n_hits: int = 2, n_misses: int = 2) -> list[dict]:
    cases: list[dict] = []
    for i in range(n_hits):
        cases.append({"id": f"trigger:hit:{i + 1:03d}", "expect": "hit",
                      "prompt": f"帮我把纪要弄成行动项 {i}"})
    for i in range(n_misses):
        cases.append({"id": f"trigger:miss:{i + 1:03d}", "expect": "miss",
                      "prompt": f"画个图表 {i}"})
    return cases


# ---------- 单元：frontmatter / 评分 ----------

def test_parse_and_rewrite_description() -> None:
    name, desc = parse_frontmatter(SKILL_MD)
    assert name == "action-extractor"
    assert "行动项" in desc

    new_md = rewrite_description(SKILL_MD, "新描述：整理待办")
    assert "description: 新描述：整理待办" in new_md
    assert parse_frontmatter(new_md)[1] == "新描述：整理待办"
    assert "行动项整理" in new_md, "正文不得丢失"
    # 无 frontmatter → 补齐
    padded = rewrite_description("正文", "描述")
    assert parse_frontmatter(padded)[1] == "描述"

    with pytest.raises(Exception):
        rewrite_description(SKILL_MD, "   ")


def test_frontmatter_uses_safe_yaml_and_supports_multiline_values() -> None:
    import yaml

    md = ('---\nname: "action: extractor"\ndescription: >-\n'
          '  Organize meeting notes\n  into action items.\ncustom: [a, b]\n'
          '---\n\nBody\n')
    assert parse_frontmatter(md) == (
        "action: extractor", "Organize meeting notes into action items."
    )
    rewritten = rewrite_description(md, "整理会议纪要：\n并生成行动项")
    front = rewritten.split("---", 2)[1]
    parsed = yaml.safe_load(front)
    assert parsed == {
        "name": "action: extractor",
        "description": "整理会议纪要：\n并生成行动项",
        "custom": ["a", "b"],
    }
    assert rewritten.endswith("Body\n")


def test_frontmatter_rejects_non_mapping_and_unsafe_yaml() -> None:
    assert parse_frontmatter("---\n- name\n- description\n---\n") == ("", "")
    # Python object tags are not constructed by SafeLoader.
    malicious = "---\nname: !!python/object/apply:os.system ['echo unsafe']\n---\n"
    assert parse_frontmatter(malicious) == ("", "")


def test_score_trigger_rates() -> None:
    cases = _cases()
    selections = {
        "trigger:hit:001": "action-extractor",
        "trigger:hit:002": None,          # 漏触发
        "trigger:miss:001": None,          # 正确不选
        "trigger:miss:002": "action-extractor",  # 误触
    }
    sc = score_trigger(cases, selections, "action-extractor")
    assert sc["hits"] == 2 and sc["hit_ok"] == 1
    assert sc["misses"] == 2 and sc["miss_ok"] == 1
    assert sc["recall"] == 0.5
    assert sc["false_rate"] == 0.5
    assert sc["overall"] == 0.5


# ---------- 单元：run_trigger_verification ----------

def _decide_correct(text: str) -> str | None:
    # 只看用户消息部分（judge prompt 内嵌技能 description，不能整段匹配）
    # 注意：不加 re.S，让 (.*) 停在用户消息行内（该行无内嵌引号）
    import re
    m = re.search(r'用户消息："(.*)"', text)
    msg = m.group(1) if m else text
    # "会记录"是口语化正例，也应触发
    return "action-extractor" if ("纪要" in msg or "会记" in msg) else None


def test_verification_pass_without_optimization() -> None:
    """全对 → 不进优化循环，description 不变。"""
    provider = TriggerProvider(_cases(), _decide_correct)
    result = run_trigger_verification(
        provider, SKILL_MD, None, DISTRACTORS,
        total_cases=4, hit_cases=2, rng=random.Random(7),
    )
    assert result["score"]["overall"] == 1.0
    assert result["description_changed"] is False
    assert result["rounds"] == 0 and result["stopped_reason"] == ""
    assert result["model_calls"] == 5, "1 生成 + 4 判定，无优化调用"


def test_verification_optimize_reaches_threshold() -> None:
    """初始差 → 改写一次 → 重评达标；description_changed=True。"""
    state = {"rewrites": 0}

    def decide(text: str) -> str | None:
        import re
        # 不加 re.S：让 (.*) 停在用户消息行（该行无内嵌引号），
        # 否则贪婪匹配会吞掉后面的路由指令文本
        m = re.search(r'用户消息："(.*)"', text)
        msg = m.group(1) if m else text
        hit = "纪要" in msg or "行动项" in msg
        if state["rewrites"] == 0:
            # 旧 description 下：口语化正例（尾号 0）漏触发 + 相邻负例被误触
            if msg.endswith("0"):
                return None if hit else "action-extractor"
        return "action-extractor" if hit else None

    def rewrite(_n: int) -> str:
        state["rewrites"] += 1
        return "整理会议纪要为行动项表格（含负责人与截止日期），也支持待办清单整理"

    provider = TriggerProvider(_cases(), decide, rewrite)
    result = run_trigger_verification(
        provider, SKILL_MD, None, DISTRACTORS,
        threshold=0.75, total_cases=4, hit_cases=2, rng=random.Random(7),
    )
    assert result["initial_score"]["overall"] < 0.75
    assert result["score"]["overall"] >= 0.75
    assert result["description_changed"] is True
    assert result["rounds"] == 1 and result["stopped_reason"] == ""


def test_verification_stops_on_length_and_rounds() -> None:
    """改写超长 → length；改写无进展（同 description）→ no_progress。"""
    provider = TriggerProvider(
        _cases(), lambda t: None,  # 全漏，持续需要优化
        rewrite=lambda n: "x" * (MAX_DESCRIPTION_LEN + 1),
    )
    result = run_trigger_verification(
        provider, SKILL_MD, None, DISTRACTORS,
        total_cases=4, hit_cases=2, rng=random.Random(7),
    )
    assert result["stopped_reason"] == "length"
    assert result["description_changed"] is False

    provider2 = TriggerProvider(
        _cases(), lambda t: None,
        rewrite=lambda n: SKILL_MD.split("description: ")[1].split("\n")[0],  # 原样
    )
    result2 = run_trigger_verification(
        provider2, SKILL_MD, None, DISTRACTORS,
        total_cases=4, hit_cases=2, rng=random.Random(7),
    )
    assert result2["stopped_reason"] == "no_progress"
    assert result2["description_changed"] is False


# ---------- 端到端：生成链交付自动排触发验证 + 导出折算 ----------

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
    """生成链假模型（与 repair_eval_loop 同模式）。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
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
    """评测侧假模型：固定通过。"""

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        return ModelResponse(content="已完成 REQ-1：行动项整理",
                             usage=Usage(50, 25), finish_reason="stop")


class AlwaysOtherProvider:
    """触发判定假模型：永远不选目标技能（全漏 → 触发验证如实 failed）。"""

    @property
    def capabilities(self) -> Any:
        from app.llm import Capabilities
        return Capabilities(tools=False, streaming=False, json_mode=True,
                            max_context_tokens=100_000)

    def complete(self, messages: list[Any], **_kw: Any) -> Any:
        return ModelResponse(content=json.dumps({"selected": "data-viz-helper"}),
                             usage=Usage(20, 10), finish_reason="stop")


def test_generation_chain_schedules_trigger_and_export_folds(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主路径：交付自动排 trigger_run → 结果落库 → 导出折算第六维 passed。"""
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod
    import app.workers.handlers.trigger_run as trig_mod
    from app.services import queue as queue_svc

    gen = GenScriptedProvider([_contract(), _plan(),
                               {"path": "SKILL.md", "kind": "text",
                                "complete": True, "content": _good_skill()}])
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: EchoProvider())

    # 触发验证假模型：生成正负例 + 判定全对（纪要消息选目标，图表选其他）
    trigger_cases = [
        {"expect": "hit", "prompt": "帮我把会议纪要弄成行动项清单"},
        {"expect": "hit", "prompt": "把这份会记录整成待办"},  # 口语化（不含"纪要"）
        {"expect": "miss", "prompt": "画个柱状图对比季度数据"},
    ]
    trigger_provider = TriggerProvider(trigger_cases, _decide_correct)
    monkeypatch.setattr(trig_mod, "provider_factory", lambda: trigger_provider)

    pid = client.post("/api/projects", json={"name": "trig-chain"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    Worker().run_pending()  # 生成链 + 自动排队的 eval_run + trigger_run

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered", snap.get("error")
    vid = snap["checkpoint"]["receipt"]["version_id"]

    # 落库对账：EvalRun(kind=trigger) + 逐例结果 + 触发用例集
    from app.models import EvalCaseResult, EvalRun, Suite
    with Session(create_db_engine(db_path(load_config()))) as s:
        ev = (s.query(EvalRun)
              .filter(EvalRun.kind == "trigger", EvalRun.version_id == vid)
              .one())
        rows = (s.query(EvalCaseResult)
                .filter(EvalCaseResult.eval_run_id == ev.id).all())
        assert len(rows) == 3
        assert all(r.group_name == "target" for r in rows)
        assert all(r.passed_count == 1 for r in rows), "全对用例逐例 passed"
        meta = json.loads(ev.meta_json)
        assert meta["score"]["overall"] == 1.0
        suite = s.get(Suite, ev.suite_id)
        assert suite is not None and len(json.loads(suite.cases_json)) == 3
        # 生成链交付时确实排了 trigger_run（幂等键含 version_id）
        from app.models import Job
        job = s.query(Job).filter_by(
            idempotency_key=f"trigger:{fid}:{vid}").one()
        assert job.type == "trigger_run"

    # 导出折算：trigger: checks 进收据，第六维 passed，不阻塞导出
    resp = client.post(f"/api/projects/{pid}/skill-versions/{vid}/export",
                       headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trigger_status"] == "passed", body

    import zipfile
    from pathlib import Path
    with zipfile.ZipFile(Path(body["zip_path"])) as zf:
        report = json.loads(zf.read("delivery/report.json"))
    trigger_checks = [c for c in report["checks"]
                      if c["check_id"].startswith("trigger:")]
    assert len(trigger_checks) == 4, "3 用例 + 1 汇总"
    assert any(c["check_id"] == "trigger:summary"
               and c["status"] == "passed" for c in trigger_checks)


def test_trigger_failed_folds_honestly_without_blocking(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收：触发验证失败 → 导出 trigger=failed 如实标注，仍可导出。"""
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod
    import app.workers.handlers.trigger_run as trig_mod

    gen = GenScriptedProvider([_contract(), _plan(),
                               {"path": "SKILL.md", "kind": "text",
                                "complete": True, "content": _good_skill()}])
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: EchoProvider())
    monkeypatch.setattr(trig_mod, "provider_factory", lambda: AlwaysOtherProvider())

    pid = client.post("/api/projects", json={"name": "trig-fail"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    Worker().run_pending()

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered", snap.get("error")
    vid = snap["checkpoint"]["receipt"]["version_id"]

    resp = client.post(f"/api/projects/{pid}/skill-versions/{vid}/export",
                       headers=AUTH)
    assert resp.status_code == 200, "触发失败不阻塞导出"
    body = resp.json()
    assert body["trigger_status"] == "failed", body
    assert any("触发质量未达标" in n for n in body["notes"]), body["notes"]


def test_trigger_not_run_folds_not_run(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收：未跑触发验证（任务未执行）→ 导出 trigger=not_run（如实标注）。"""
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod
    import app.workers.handlers.trigger_run as trig_mod

    gen = GenScriptedProvider([_contract(), _plan(),
                               {"path": "SKILL.md", "kind": "text",
                                "complete": True, "content": _good_skill()}])
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: EchoProvider())
    # 触发任务永远不完成（provider 抛异常 → job failed → 无已完成 EvalRun）
    class ExplodingProvider:
        @property
        def capabilities(self) -> Any:
            from app.llm import Capabilities
            return Capabilities(tools=False, streaming=False, json_mode=True,
                                max_context_tokens=100_000)

        def complete(self, messages: list[Any], **_kw: Any) -> Any:
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(trig_mod, "provider_factory", lambda: ExplodingProvider())

    pid = client.post("/api/projects", json={"name": "trig-notrun"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    Worker().run_pending()

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered"
    vid = snap["checkpoint"]["receipt"]["version_id"]

    resp = client.post(f"/api/projects/{pid}/skill-versions/{vid}/export",
                       headers=AUTH)
    assert resp.status_code == 200, "未运行不阻塞导出"
    body = resp.json()
    assert body["trigger_status"] == "not_run", body
    assert any("触发验证未运行" in n for n in body["notes"]), body["notes"]


def test_trigger_optimization_freezes_new_version(
    migrated_db: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v14-5：初始差 → 有界优化改善 → 冻结新版本（不改写原版本行）。

    不可变纪律：改 description 也是新版本；原版本 SKILL.md 原样保留；
    新版本达标 → 自动再排一次触发验证（链长 ≤1，幂等键 trigger:opt:）。
    """
    import app.workers.handlers.agent_run as ar_mod
    import app.workers.handlers.generation_stage as gs_mod
    import app.workers.handlers.trigger_run as trig_mod

    gen = GenScriptedProvider([_contract(), _plan(),
                               {"path": "SKILL.md", "kind": "text",
                                "complete": True, "content": _good_skill()}])
    monkeypatch.setattr(gs_mod, "provider_factory", lambda: gen)
    monkeypatch.setattr(ar_mod, "provider_factory", lambda: EchoProvider())

    trigger_cases = [
        {"expect": "hit", "prompt": "帮我把会议纪要弄成行动项清单"},
        {"expect": "hit", "prompt": "把这份会记录整成待办"},  # 旧描述漏触发
        {"expect": "miss", "prompt": "画个柱状图对比季度数据"},
    ]
    state = {"rewrites": 0}

    def decide(text: str) -> str | None:
        import re
        m = re.search(r'用户消息："(.*)"', text)
        msg = m.group(1) if m else text
        hit = "纪要" in msg or "待办" in msg
        if state["rewrites"] == 0 and "待办" in msg:
            return None  # 旧 description 下漏触发
        return "action-extractor" if hit else None

    def rewrite(_n: int) -> str:
        state["rewrites"] += 1
        return "把会议纪要或会记录整理为行动项/待办清单，输出负责人与截止日期"

    trigger_provider = TriggerProvider(trigger_cases, decide, rewrite)
    monkeypatch.setattr(trig_mod, "provider_factory", lambda: trigger_provider)

    pid = client.post(f"/api/projects", json={"name": "trig-opt"},
                      headers=AUTH).json()["id"]
    fid = client.post(f"/api/projects/{pid}/generation-flows",
                      json={"request_text": "整理纪要为行动项"},
                      headers=AUTH).json()["flow_id"]
    Worker().run_pending()  # 生成链 + eval_run + trigger_run（含优化）

    snap = client.get(f"/api/generation-flows/{fid}", headers=AUTH).json()
    assert snap["state"] == "delivered", snap.get("error")
    vid = snap["checkpoint"]["receipt"]["version_id"]

    from app.models import EvalRun, Job, SkillVersion
    from app.core.config import load_config as _lc
    cfg = _lc()

    with Session(create_db_engine(db_path(cfg))) as s:
        ev = (s.query(EvalRun)
              .filter(EvalRun.kind == "trigger", EvalRun.version_id == vid)
              .one())
        meta = json.loads(ev.meta_json)
        assert meta["description_changed"] is True
        assert meta["initial_score"]["overall"] < 0.85
        assert meta["score"]["overall"] >= 0.85, meta
        assert meta["usage_calls"] == meta["model_calls"]
        assert meta["usage_missing_calls"] == 0
        assert (meta["usage_observed"]["prompt_tokens"]
                + meta["usage_observed"]["completion_tokens"]
                == 320)

        # 优化有效 → 冻结了新版本
        new_rows = (s.query(SkillVersion)
                    .filter(SkillVersion.project_id == pid,
                            SkillVersion.id != vid).all())
        assert len(new_rows) == 1, "应有且只有一个优化后的新版本"
        new_vid = new_rows[0].id
        assert new_vid != vid

        # 新版本达标 → 再排了一次触发验证（链长 ≤1）
        opt_job = s.query(Job).filter_by(
            idempotency_key=f"trigger:opt:{new_vid}").one()
        assert opt_job.type == "trigger_run"

    from app.generation.trigger import parse_frontmatter
    vdir_old = cfg.data_dir / "projects" / pid / "skills" / "versions" / vid
    vdir_new = cfg.data_dir / "projects" / pid / "skills" / "versions" / new_vid
    old_md = (vdir_old / "SKILL.md").read_text(encoding="utf-8")
    new_md = (vdir_new / "SKILL.md").read_text(encoding="utf-8")
    # 不可变纪律：原版本行原样；新版本带新 description
    assert "会记录" not in parse_frontmatter(old_md)[1]
    assert "会记录" in parse_frontmatter(new_md)[1]
    assert "REQ-1" in new_md, "正文（验收依据）不得丢失"

    # 版本列表同时可见两个版本
    rows = client.get(f"/api/projects/{pid}/skills/versions",
                      headers=AUTH).json()
    assert {r["id"] for r in rows} >= {vid, new_vid}

    # 新版本的再验证任务可正常执行（链到此为止，不再冻结）
    Worker().run_pending()
    with Session(create_db_engine(db_path(cfg))) as s:
        evs = (s.query(EvalRun)
               .filter(EvalRun.kind == "trigger", EvalRun.version_id == new_vid)
               .all())
        assert evs and all(e.status == "finished" for e in evs)
        assert all(json.loads(e.meta_json)["optimization_depth"] == 1 for e in evs)
        bounded_job = s.query(Job).filter_by(
            idempotency_key=f"trigger:opt:{new_vid}").one()
        assert json.loads(bounded_job.payload_json)["optimization_depth"] == 1
        n_versions = (s.query(SkillVersion)
                      .filter(SkillVersion.project_id == pid).count())
        assert n_versions == 2, "再验证不再追加版本（链长 ≤1）"

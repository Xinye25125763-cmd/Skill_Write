"""T13：工具运行条件与脚本依赖预检。

本项验收（工程计划 §5 T13）：
1. 缺 pwsh / 第三方库 → 清楚 blocked（可读证据，不猜）；
2. 非 0 退出和超时证据可读（stdout/stderr 摘录进 evidence）；
3. 纯文本技能不要求脚本（预检 PASSED，不强行运行任何东西）；
4. 可信脚本可正常运行（exit 0 → PASSED，不一刀切禁工具）；
5. 执行策略 allow_scripts=False → 脚本检查 NOT_RUN（静态检查不触发
   脚本副作用）；
6. 预算：脚本耗时计入 used_tool_seconds；max_tool_seconds 耗尽后
   剩余检查 NOT_RUN（可恢复语义，不假装跑过）。

模型一律不参与本层；真实宿主另行记录。
"""
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.config import load_config
from app.core.db import create_db_engine, db_path
from app.generation.contracts import (
    CheckStatus,
    FlowBudget,
    PlannedFile,
    SkillPlan,
)
from app.models import Project, SkillVersion

# ---------- 夹具 ----------


def _plan(files: list[dict]) -> SkillPlan:
    return SkillPlan.model_validate({
        "plan_id": "plan-1",
        "files": files or [{"path": "SKILL.md", "kind": "text",
                            "purpose": "主文档", "requirement_ids": []}],
        "acceptances": [{"acceptance_id": "ACC-001", "kind": "static",
                         "description": "占位", "requirement_ids": []}],
    })


def _version_with_files(migrated_db: Path, tmp: Path, files: dict[str, str]) -> tuple[str, Path]:
    """建一个项目 + 版本行 + 版本目录（磁盘真相），返回 (version_id, 版本目录)。"""
    cfg = load_config()
    from app.core.ulid import new_ulid
    with Session(create_db_engine(db_path(cfg))) as s:
        pid = "01RTTIME000000000000000000"
        if s.get(Project, pid) is None:
            s.add(Project(id=pid, name="runtime-test"))
            s.commit()
        vid = new_ulid()
        vdir = cfg.data_dir / "projects" / pid / "skills" / "versions" / vid
        vdir.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            p = vdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8", newline="\n")
        import hashlib
        h = hashlib.sha256(vid.encode()).hexdigest()
        s.add(SkillVersion(
            id=vid, project_id=pid, label="v1", content_hash=h,
            files_json="[]", warnings_json="[]",
            created_at=datetime.now(timezone.utc),
        ))
        s.commit()
        return vid, vdir


def _by_id(report, cid: str):
    got = [c for c in report.checks if c.check_id == cid]
    assert len(got) == 1, f"缺检查 {cid}: {[c.check_id for c in report.checks]}"
    return got[0]


# ---------- 预检 ----------


def test_declared_python_bash_pwsh_dependency_and_policy(
    migrated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation import runtime as rt

    # 验收 3：纯文本技能不要求脚本
    report = rt.preflight_runtime(_plan([]), "codex", {})
    assert len(report.checks) == 1
    c = report.checks[0]
    assert c.status == CheckStatus.PASSED
    assert "纯文本" in c.evidence or "无脚本" in c.evidence

    # 验收 1：缺 pwsh → 清楚 blocked
    monkeypatch.setattr(rt, "_which", lambda name: None)  # 所有解释器都缺失
    plan_ps = _plan([{"path": "scripts/a.ps1", "kind": "text",
                      "purpose": "p", "requirement_ids": []}])
    report = rt.preflight_runtime(plan_ps, "codex", {})
    c = _by_id(report, "runtime:pwsh")
    assert c.status == CheckStatus.UNSUPPORTED, "缺 pwsh 必须 blocked"
    assert "pwsh" in c.evidence

    # python 可用、pwsh 缺失的组合（_which 只对 pwsh 返回 None）
    monkeypatch.setattr(rt, "_which", lambda name: None if name == "pwsh" else f"/usr/bin/{name}")
    plan_mixed = _plan([
        {"path": "scripts/a.py", "kind": "text", "purpose": "p", "requirement_ids": []},
        {"path": "scripts/b.ps1", "kind": "text", "purpose": "p", "requirement_ids": []},
    ])
    report = rt.preflight_runtime(plan_mixed, "codex", {})
    assert _by_id(report, "runtime:python").status == CheckStatus.PASSED
    assert _by_id(report, "runtime:pwsh").status == CheckStatus.UNSUPPORTED

    # 验收 5：allow_scripts=False → NOT_RUN（静态检查不触发脚本副作用）
    monkeypatch.setattr(rt, "_which", lambda name: f"/usr/bin/{name}")
    report = rt.preflight_runtime(plan_mixed, "codex", {"allow_scripts": False})
    for cid in ("runtime:python", "runtime:pwsh"):
        c = _by_id(report, cid)
        assert c.status == CheckStatus.NOT_RUN, f"{cid} 策略禁止运行应 NOT_RUN"
        assert "策略" in c.evidence or "allow_scripts" in c.evidence

    # ---------- 运行候选检查 ----------
    cfg = load_config()

    # 验收 4：可信脚本正常运行 → PASSED
    vid, vdir = _version_with_files(migrated_db, tmp_path, {
        "scripts/ok.py": "print('hello from ok')\n",
    })
    checks = [{"check_id": "run:ok", "script": "scripts/ok.py"}]
    budget = FlowBudget()
    report = rt.run_candidate_checks(vid, checks, budget, cfg=cfg)
    c = _by_id(report, "run:ok")
    assert c.status == CheckStatus.PASSED, c.evidence
    assert "hello from ok" in c.evidence, "证据须含可读 stdout"
    assert budget.used_tool_seconds > 0, "脚本耗时计入 used_tool_seconds"

    # 验收 1：第三方库缺失 → blocked 且证据可读（ModuleNotFoundError 在 stderr）
    vid, vdir = _version_with_files(migrated_db, tmp_path, {
        "scripts/needs.py": "import nonexistent_third_party\n",
    })
    report = rt.run_candidate_checks(vid, [{"check_id": "run:needs",
                                            "script": "scripts/needs.py"}],
                                     FlowBudget(), cfg=cfg)
    c = _by_id(report, "run:needs")
    assert c.status == CheckStatus.FAILED, "依赖缺失必须 FAILED"
    assert "ModuleNotFoundError" in c.evidence or "nonexistent_third_party" in c.evidence, \
        "缺失依赖的证据须可读（stderr 摘录）"

    # 验收 2：非 0 退出证据可读
    vid, vdir = _version_with_files(migrated_db, tmp_path, {
        "scripts/fail.py": "import sys\nsys.stderr.write('boom: bad input\\n')\nsys.exit(3)\n",
    })
    report = rt.run_candidate_checks(vid, [{"check_id": "run:fail",
                                            "script": "scripts/fail.py"}],
                                     FlowBudget(), cfg=cfg)
    c = _by_id(report, "run:fail")
    assert c.status == CheckStatus.FAILED
    assert "exit" in c.evidence.lower()
    assert "boom" in c.evidence, "stderr 须摘录进证据"

    # 验收 2：超时证据可读
    vid, vdir = _version_with_files(migrated_db, tmp_path, {
        "scripts/slow.py": "import time\ntime.sleep(30)\n",
    })
    report = rt.run_candidate_checks(vid, [{"check_id": "run:slow",
                                            "script": "scripts/slow.py",
                                            "timeout": 1}],
                                     FlowBudget(), cfg=cfg)
    c = _by_id(report, "run:slow")
    assert c.status == CheckStatus.FAILED, "超时必须 FAILED"
    assert "超时" in c.evidence or "timeout" in c.evidence.lower()

    # 验收 6：预算耗尽 → 剩余检查 NOT_RUN（不假装跑过）
    vid, vdir = _version_with_files(migrated_db, tmp_path, {
        "scripts/ok.py": "print('ok')\n",
        "scripts/ok2.py": "print('ok2')\n",
    })
    budget = FlowBudget(max_tool_seconds=0.0)  # 一开始就无工具预算
    report = rt.run_candidate_checks(
        vid,
        [{"check_id": "run:1", "script": "scripts/ok.py"},
         {"check_id": "run:2", "script": "scripts/ok2.py"}],
        budget, cfg=cfg,
    )
    assert _by_id(report, "run:1").status == CheckStatus.NOT_RUN
    assert _by_id(report, "run:2").status == CheckStatus.NOT_RUN
    assert "预算" in _by_id(report, "run:1").evidence

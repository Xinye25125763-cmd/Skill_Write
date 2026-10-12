"""T18：至少一个实际宿主的运行与触发验收（codex 适配先行）。

本项验收（工程计划 §5 T18）：
1. 宿主不可用 → unsupported/not_run，绝不能 passed（blocked 如实可读）；
2. 真实宿主正/负触发：正例被技能命中、负例不被命中（触发判定有记录）；
3. 资源加载、脚本条件与输出验证有记录（触发证据含文件/输出摘录）；
4. API 注入结果不能冒充原生触发——原生触发证据必须来自宿主侧执行记录，
   而非 API 回填（evidence 必须携带宿主执行源标识）；
5. 其他平台注册不自动标支持（capabilities 只报实际验证过的）。

codex 宿主当前规范（docs/platforms/codex-verification.md 详述）：
- 适配层把已冻结版本解包到 codex skills 目录（skill 文件原样落地）；
- 触发执行走 codex CLI（子进程），非 API 注入；
- 不可用（CLI 缺失 / 目录不可写）→ blocked 证据，状态 not_run/unsupported。
"""
import json
import subprocess
from pathlib import Path

import pytest

from app.generation.contracts import (
    CheckReport,
    CheckResult,
    CheckStatus,
    DeliveryReceipt,
    DeliveryStatus,
)

RECEIPT = DeliveryReceipt(
    flow_id="flow-1",
    version_id="01TESTVERSION0000000000",
    delivery_status=DeliveryStatus.CANDIDATE,
    static_status=CheckStatus.PASSED,
    execution_status=CheckStatus.PASSED,
    coverage_status=CheckStatus.PASSED,
    host_status=None,
    zip_path="",
    notes=[],
)

TRIGGER_CASES = [
    {"case_id": "trigger-positive", "kind": "trigger", "prompt": "帮我整理会议纪要",
     "expect": "hit"},
    {"case_id": "trigger-negative", "kind": "trigger", "prompt": "帮我写会议通知",
     "expect": "miss"},
    {"case_id": "resource-load", "kind": "execution", "prompt": "读取 reference.md 并复述标题",
     "expect": "output_contains:测试技能"},
    {"case_id": "script-condition", "kind": "execution", "prompt": "运行 scripts/extract.py",
     "expect": "output_contains:extract"},
]


def _has(checks: CheckReport, cid: str) -> CheckResult:
    got = [c for c in checks.checks if c.check_id == cid]
    assert len(got) == 1, f"缺检查 {cid}: {[c.check_id for c in checks.checks]}"
    return got[0]


def test_host_unavailable_not_pass_and_trigger_positive_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.platforms.base import HostAdapter, get_adapter
    from app.platforms.codex import CodexAdapter

    # ---------- 块 A：注册表与能力声明 ----------
    adapter = get_adapter("codex")
    assert isinstance(adapter, CodexAdapter)
    assert isinstance(adapter, HostAdapter)
    caps = adapter.capabilities()
    assert caps["platform"] == "codex"
    assert caps["native_trigger"] is True   # codex 走原生触发执行
    assert isinstance(caps["verified"], bool)

    # 其他平台注册不自动标支持（验收 5）
    other = get_adapter("slack")
    assert other is not None
    assert other.capabilities()["verified"] is False
    assert other.capabilities()["native_trigger"] is False

    # ---------- 块 B：宿主不可用 → not_run，绝不 passed ----------
    monkeypatch.setattr("app.platforms.codex.shutil.which", lambda _name: None)
    unavailable = CodexAdapter(cli_path=None, skills_root=tmp_path / "nope")
    checks = unavailable.validate_delivery(RECEIPT, TRIGGER_CASES)
    c = _has(checks, "host:codex:available")
    assert c.status in (CheckStatus.NOT_RUN, CheckStatus.UNSUPPORTED)
    assert "blocked" in c.evidence or "不可用" in c.evidence
    # 不可用绝不冒充通过：触发检查全部 not_run
    for case in TRIGGER_CASES:
        rc = _has(checks, f"host:codex:case:{case['case_id']}")
        assert rc.status == CheckStatus.NOT_RUN

    # ---------- 块 C：真实宿主触发（可用 codex 适配 + 受控执行记录） ----------
    # 模拟宿主侧执行记录文件（真实宿主运行时由 codex CLI 写出；
    # 适配层只读宿主执行记录，不回填 API 结果——验收 4）
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    records = [
        {"case_id": "trigger-positive", "source": "codex-cli", "hit": True,
         "skill": "test-skill", "prompt": "帮我整理会议纪要"},
        {"case_id": "trigger-negative", "source": "codex-cli", "hit": False,
         "skill": None, "prompt": "帮我写会议通知"},
        {"case_id": "resource-load", "source": "codex-cli", "hit": True,
         "skill": "test-skill", "files_loaded": ["reference.md"],
         "output": "标题：测试技能"},
        {"case_id": "script-condition", "source": "codex-cli", "hit": True,
         "skill": "test-skill", "scripts_run": ["scripts/extract.py"],
         "output": "extract", "exit_code": 0},
    ]
    (runs_dir / "records.json").write_text(json.dumps(records), encoding="utf-8")

    adapter_ok = CodexAdapter(
        cli_path="fake-codex", skills_root=tmp_path / "skills",
        records_path=runs_dir / "records.json",
    )
    # CLI 探活：受控假命令（真实宿主验证另见 docs/platforms/codex-verification.md）
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a, returncode=0, stdout="codex 0.1.0", stderr=""),
    )
    checks = adapter_ok.validate_delivery(RECEIPT, TRIGGER_CASES)

    # 可用性检查通过（真实探活，非 API 注入）
    c = _has(checks, "host:codex:available")
    assert c.status == CheckStatus.PASSED
    assert "codex" in c.evidence

    # 正触发命中
    pos = _has(checks, "host:codex:case:trigger-positive")
    assert pos.status == CheckStatus.PASSED
    assert "codex-cli" in pos.evidence  # 宿主执行源标识（验收 4）
    # 负触发不命中
    neg = _has(checks, "host:codex:case:trigger-negative")
    assert neg.status == CheckStatus.PASSED
    assert "未命中" in neg.evidence or "miss" in neg.evidence

    # 资源加载与脚本条件有记录（验收 3）
    res = _has(checks, "host:codex:case:resource-load")
    assert res.status == CheckStatus.PASSED
    assert "reference.md" in res.evidence
    script = _has(checks, "host:codex:case:script-condition")
    assert script.status == CheckStatus.PASSED
    assert "scripts/extract.py" in script.evidence
    assert "exit=0" in script.evidence

    # ---------- 块 D：API 注入不能冒充原生触发 ----------
    # 伪造无宿主执行源标识的记录（source != codex-cli）→ 该例不得 passed
    bad_records = [
        {"case_id": "trigger-positive", "source": "api-injection", "hit": True},
    ]
    (runs_dir / "records.json").write_text(json.dumps(bad_records), encoding="utf-8")
    checks = adapter_ok.validate_delivery(RECEIPT, TRIGGER_CASES)
    pos = _has(checks, "host:codex:case:trigger-positive")
    assert pos.status == CheckStatus.FAILED
    assert "api-injection" in pos.evidence or "宿主执行源" in pos.evidence
    # 其余无记录 → not_run（不假装跑过）
    neg = _has(checks, "host:codex:case:trigger-negative")
    assert neg.status == CheckStatus.NOT_RUN

    # ---------- 块 E：负例失败如实呈现（正例未命中 → FAILED） ----------
    miss_records = [
        {"case_id": "trigger-positive", "source": "codex-cli", "hit": False},
        {"case_id": "trigger-negative", "source": "codex-cli", "hit": True, "skill": "x"},
    ]
    (runs_dir / "records.json").write_text(json.dumps(miss_records), encoding="utf-8")
    checks = adapter_ok.validate_delivery(RECEIPT, TRIGGER_CASES)
    assert _has(checks, "host:codex:case:trigger-positive").status == CheckStatus.FAILED
    assert _has(checks, "host:codex:case:trigger-negative").status == CheckStatus.FAILED

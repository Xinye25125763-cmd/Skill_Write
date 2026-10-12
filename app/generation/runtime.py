"""工具运行条件与脚本依赖预检（工程计划 §5 T13）。

preflight_runtime：按方案声明解释器（.py→python、.sh→bash、.ps1→pwsh）
探测可用性——缺哪个清楚 blocked（UNSUPPORTED + 可读证据），不猜。

run_candidate_checks：在冻结版本目录上按清单运行脚本检查：
- 纯文本技能不要求脚本（预检直接 PASSED，不强行运行）；
- 可信脚本可正常运行（受控子进程 + 超时 + 输出收集），不一刀切禁工具；
- 非 0 退出 / 超时 / 依赖缺失 → FAILED，stdout/stderr 摘录进证据；
- 执行策略 allow_scripts=False → NOT_RUN（静态检查默认不触发脚本副作用）；
- 工具预算耗尽 → 剩余检查 NOT_RUN（不假装跑过）。

运行模式：可信个人运行模式（继承当前进程环境，白名单扩展名）；
安装依赖不自动执行——需可见授权与记录（后续任务接入）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.generation.contracts import (
    CheckReport,
    CheckResult,
    CheckStatus,
    FlowBudget,
    SkillPlan,
)

DEFAULT_SCRIPT_TIMEOUT = 60  # 秒
EVIDENCE_MAX = 800           # 证据摘录上限（可读，不淹没报告）

# 扩展名 → (解释器探测名, 检查 ID)
_INTERPRETERS: dict[str, tuple[str, str]] = {
    ".py": ("python", "runtime:python"),
    ".sh": ("bash", "runtime:bash"),
    ".ps1": ("pwsh", "runtime:pwsh"),
}


def _which(name: str) -> str | None:
    """解释器探测（测试可 patch）。"""
    return shutil.which(name)


def _clip(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= EVIDENCE_MAX:
        return text
    return text[:EVIDENCE_MAX] + f"…（截断，原始 {len(text)} 字符）"


def preflight_runtime(
    plan: SkillPlan, platform: str, execution_policy: dict[str, Any]
) -> CheckReport:
    """方案声明的脚本依赖预检：解释器可用性 + 策略闸。

    - 纯文本方案（无脚本扩展名文件）→ 单项 PASSED，不要求脚本；
    - 缺解释器 → UNSUPPORTED（清楚 blocked：哪个工具、哪些文件受影响）；
    - allow_scripts=False → NOT_RUN（静态检查不触发脚本副作用）。
    """
    allow_scripts = bool((execution_policy or {}).get("allow_scripts", True))

    by_ext: dict[str, list[str]] = {}
    for f in plan.files:
        ext = Path(f.path).suffix.lower()
        if ext in _INTERPRETERS:
            by_ext.setdefault(ext, []).append(f.path)

    if not by_ext:
        return CheckReport(checks=[CheckResult(
            check_id="runtime:text_only",
            status=CheckStatus.PASSED,
            evidence="纯文本技能：方案未声明任何脚本文件，无需解释器预检",
        )])

    results: list[CheckResult] = []
    for ext, paths in sorted(by_ext.items()):
        tool_name, check_id = _INTERPRETERS[ext]
        if not allow_scripts:
            results.append(CheckResult(
                check_id=check_id,
                status=CheckStatus.NOT_RUN,
                evidence=(
                    f"策略禁止运行脚本（allow_scripts=False），"
                    f"静态检查不触发脚本副作用；涉及文件: {', '.join(paths)}"
                ),
            ))
            continue
        found = _which(tool_name)
        if found is None:
            results.append(CheckResult(
                check_id=check_id,
                status=CheckStatus.UNSUPPORTED,
                evidence=(
                    f"blocked：环境缺少 {tool_name}（.{ext} 脚本无法运行）；"
                    f"涉及文件: {', '.join(paths)}"
                ),
            ))
        else:
            results.append(CheckResult(
                check_id=check_id,
                status=CheckStatus.PASSED,
                evidence=f"{tool_name} 可用（{found}）；涉及文件: {', '.join(paths)}",
            ))
    return CheckReport(checks=results)


def _version_dir(cfg, version_id: str) -> Path:  # noqa: ANN001
    """从版本行定位版本目录（磁盘真相）。"""
    from sqlalchemy.orm import Session
    from app.core.db import create_db_engine, db_path
    from app.models import SkillVersion

    engine = create_db_engine(db_path(cfg))
    with Session(engine) as s:
        row = s.get(SkillVersion, version_id)
        if row is None:
            raise LookupError(f"版本不存在: {version_id}")
        pid = row.project_id
    return cfg.data_dir / "projects" / pid / "skills" / "versions" / version_id


def _run_one(vdir: Path, spec: dict[str, Any], budget: FlowBudget) -> CheckResult:
    """运行一个脚本检查：受控子进程 + 超时 + 输出摘录 + 工具预算。"""
    check_id = str(spec.get("check_id") or spec.get("script") or "run")
    rel = str(spec.get("script") or "")
    timeout = float(spec.get("timeout") or DEFAULT_SCRIPT_TIMEOUT)

    script = vdir / rel
    if not script.is_file():
        return CheckResult(check_id=check_id, status=CheckStatus.FAILED,
                           evidence=f"脚本不存在: {rel}")

    ext = script.suffix.lower()
    if ext == ".py":
        cmd = [sys.executable, str(script)]
    elif ext == ".sh":
        bash = _which("bash") or "bash"
        cmd = [bash, str(script)]
    elif ext == ".ps1":
        pwsh = _which("pwsh") or "pwsh"
        cmd = [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)]
    else:
        return CheckResult(check_id=check_id, status=CheckStatus.FAILED,
                           evidence=f"不支持的脚本类型: {ext}")

    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 参数受控、超时约束
            cmd, cwd=str(vdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        budget.used_tool_seconds += time.monotonic() - started
        return CheckResult(
            check_id=check_id, status=CheckStatus.FAILED,
            evidence=f"脚本超时（>{timeout:.0f}s）: {rel}；进程被终止，无输出可用",
        )
    budget.used_tool_seconds += time.monotonic() - started

    out, err = _clip(proc.stdout), _clip(proc.stderr)
    if proc.returncode == 0:
        return CheckResult(
            check_id=check_id, status=CheckStatus.PASSED,
            evidence=f"exit 0；stdout: {out or '(空)'}",
        )
    return CheckResult(
        check_id=check_id, status=CheckStatus.FAILED,
        evidence=(
            f"exit {proc.returncode}；stdout: {out or '(空)'}；"
            f"stderr: {err or '(空)'}"
        ),
    )


def run_candidate_checks(
    version_id: str,
    checks: list[dict[str, Any]],
    budget: FlowBudget,
    *,
    cfg=None,  # noqa: ANN001 AppConfig；缺省读当前配置
    execution_policy: dict[str, Any] | None = None,
) -> CheckReport:
    """在冻结版本上运行脚本检查清单。

    - allow_scripts=False → 全部 NOT_RUN（不触发脚本副作用）；
    - 工具预算耗尽 → 剩余 NOT_RUN（不假装跑过）；
    - 依赖缺失 / 非 0 退出 / 超时 → FAILED，证据可读。
    """
    if cfg is None:
        from app.core.config import load_config
        cfg = load_config()
    policy = execution_policy or {}
    allow_scripts = bool(policy.get("allow_scripts", True))

    vdir = _version_dir(cfg, version_id)
    results: list[CheckResult] = []
    for spec in checks:
        check_id = str(spec.get("check_id") or spec.get("script") or "run")
        if not allow_scripts:
            results.append(CheckResult(
                check_id=check_id, status=CheckStatus.NOT_RUN,
                evidence="策略禁止运行脚本（allow_scripts=False）",
            ))
            continue
        max_tool = budget.max_tool_seconds
        if max_tool is not None and budget.used_tool_seconds >= max_tool:
            results.append(CheckResult(
                check_id=check_id, status=CheckStatus.NOT_RUN,
                evidence=(
                    f"工具预算耗尽（used {budget.used_tool_seconds:.2f}s / "
                    f"max {max_tool:.2f}s），未运行——不假装跑过"
                ),
            ))
            continue
        results.append(_run_one(vdir, spec, budget))
    return CheckReport(checks=results)

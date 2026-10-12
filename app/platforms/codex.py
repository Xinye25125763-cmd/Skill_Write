"""codex 宿主适配器（工程计划 §5 T18，个人目标宿主先行）。

设计要点（docs/platforms/codex-verification.md 详述真实验收步骤）：
- 触发执行走 codex CLI 子进程（原生触发），适配层只读宿主侧执行记录；
- 执行记录 JSON：[{case_id, source, hit, skill?, files_loaded?, scripts_run?,
  output?, exit_code?}]——source 必须是宿主执行源标识（codex-cli），
  API 注入（source != codex-cli）的记录不得判 passed（验收 4）；
- 宿主不可用（CLI 缺失/探活失败/记录缺失）→ blocked 证据 + not_run，
  绝不能 passed（验收 1）；
- 资源加载、脚本条件与输出验证逐例进证据（验收 3）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.generation.contracts import (
    CheckReport,
    CheckResult,
    CheckStatus,
    DeliveryReceipt,
)
from app.platforms.base import HostAdapter

#: 宿主执行源标识：只有宿主侧进程写出的记录才可采信
NATIVE_SOURCE = "codex-cli"

DEFAULT_SKILLS_ROOT = Path.home() / ".codex" / "skills"
DEFAULT_RECORDS = Path.home() / ".codex" / "skill-verification" / "records.json"


class CodexAdapter(HostAdapter):
    """codex 宿主适配：CLI 探活 + 原生触发执行记录验证。"""

    platform = "codex"

    def __init__(
        self,
        cli_path: str | None = None,
        skills_root: Path | None = None,
        records_path: Path | None = None,
    ) -> None:
        self.cli_path = cli_path if cli_path is not None else shutil.which("codex")
        self.skills_root = skills_root or DEFAULT_SKILLS_ROOT
        self.records_path = records_path or DEFAULT_RECORDS

    def capabilities(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "verified": bool(self.cli_path),  # CLI 在场才声明可验证；真实验收见文档
            "native_trigger": True,
            "cli": self.cli_path,
            "skills_root": str(self.skills_root),
            "records": str(self.records_path),
        }

    # ---------- 内部 ----------

    def _probe_cli(self) -> tuple[bool, str]:
        """探活 codex CLI（子进程真实执行，非 API 注入）。"""
        if not self.cli_path:
            return False, "blocked：环境缺少 codex CLI（不可用），宿主验证不运行"
        try:
            proc = subprocess.run(  # noqa: S603 受控探活
                [self.cli_path, "--version"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"blocked：codex CLI 探活失败（{type(exc).__name__}），不可用"
        if proc.returncode != 0:
            return False, f"blocked：codex CLI 探活非零退出（{proc.returncode}），不可用"
        version = (proc.stdout or proc.stderr or "").strip()[:60]
        return True, f"codex CLI 探活成功：{version}"

    def _load_records(self) -> list[dict[str, Any]]:
        if not self.records_path or not Path(self.records_path).is_file():
            return []
        try:
            data = json.loads(Path(self.records_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    @staticmethod
    def _expect_ok(expect: str, record: dict[str, Any]) -> tuple[bool, str]:
        """按 expect 判定单例；返回 (passed, evidence 片段)。"""
        if expect == "hit":
            return bool(record.get("hit")), f"hit={bool(record.get('hit'))}"
        if expect == "miss":
            return not record.get("hit"), (
                f"{'未命中' if not record.get('hit') else '命中'}"
                f"（hit={bool(record.get('hit'))}, skill={record.get('skill')!r}）"
            )
        if expect.startswith("output_contains:"):
            needle = expect[len("output_contains:"):]
            output = str(record.get("output") or "")
            return needle in output, f"输出含 {needle!r}: {output[:120]}"
        return False, f"未知 expect: {expect!r}"

    # ---------- HostAdapter ----------

    def validate_delivery(
        self, receipt: DeliveryReceipt, cases: list[dict[str, Any]]
    ) -> CheckReport:
        checks: list[CheckResult] = []

        ok, evidence = self._probe_cli()
        checks.append(CheckResult(
            check_id=f"host:{self.platform}:available",
            status=CheckStatus.PASSED if ok else CheckStatus.NOT_RUN,
            evidence=evidence,
        ))
        if not ok:
            # 宿主不可用：全部用例 not_run，绝不冒充通过（验收 1）
            for case in cases:
                checks.append(CheckResult(
                    check_id=f"host:{self.platform}:case:{case.get('case_id', '')}",
                    status=CheckStatus.NOT_RUN,
                    evidence="blocked：宿主不可用，本例未运行——不假装跑过",
                ))
            return CheckReport(checks=checks)

        records = self._load_records()
        by_case: dict[str, dict[str, Any]] = {}
        for r in records:
            cid = str(r.get("case_id") or "")
            if cid:
                by_case[cid] = r

        for case in cases:
            cid = str(case.get("case_id") or "")
            check_id = f"host:{self.platform}:case:{cid}"
            record = by_case.get(cid)
            if record is None:
                checks.append(CheckResult(
                    check_id=check_id, status=CheckStatus.NOT_RUN,
                    evidence="无宿主执行记录（未运行）——不假装跑过",
                ))
                continue

            # 验收 4：宿主执行源标识——API 注入不能冒充原生触发
            source = str(record.get("source") or "")
            if source != NATIVE_SOURCE:
                checks.append(CheckResult(
                    check_id=check_id, status=CheckStatus.FAILED,
                    evidence=(
                        f"宿主执行源不合法: source={source!r}（要求 {NATIVE_SOURCE!r}），"
                        "API 注入结果不能冒充原生触发"
                    ),
                ))
                continue

            expect = str(case.get("expect") or "")
            passed, detail = self._expect_ok(expect, record)

            parts = [f"[{NATIVE_SOURCE}] {detail}"]
            if record.get("files_loaded"):
                parts.append(f"资源加载: {', '.join(map(str, record['files_loaded']))}")
            if record.get("scripts_run"):
                parts.append(
                    f"脚本: {', '.join(map(str, record['scripts_run']))}"
                    f"（exit={record.get('exit_code', 'n/a')}）"
                )
            if record.get("prompt"):
                parts.append(f"prompt: {str(record['prompt'])[:80]}")

            checks.append(CheckResult(
                check_id=check_id,
                status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
                evidence="；".join(parts),
            ))

        return CheckReport(checks=checks)

"""完整交付包与验证收据（工程计划 §5 T16）。

设计要点：
- ZIP 含：技能文件、需求覆盖、工程规则版本、测试报告、依赖与运行说明（README）、
  逐文件 sha256 清单；收据与界面如实区分候选/已交付（五维状态分离）；
- missing/not_run 检查存在 → delivery_status=candidate，绝不出现"全通过"；
- 版本/报告必须同 hash：提供 session 时与版本行 content_hash/report_json 对账，
  不一致 → DeliveryError（API 层映射 409），拒绝导出；
- ZIP 不含会话/模型密钥和运行数据库：敏感文件名排除 + 打包前内容扫描兜底；
- 全部哈希由系统从磁盘字节计算，模型自报一律不采信。
"""
from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.generation.contracts import (
    CheckReport,
    CheckResult,
    CheckStatus,
    DeliveryReceipt,
    DeliveryStatus,
    EngineeringProfile,
    RequirementContract,
)

# 敏感文件排除：会话/模型密钥、运行数据库不得进包（验收 4）
SENSITIVE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".env", ".key", ".pem")
SENSITIVE_NAME_PARTS = ("secret", "token", "credential", "password", "session")

# 检查 ID 前缀 → 收据维度（与 validation/runtime/coverage/evaluation 约定一致）
# 注意顺序：eval:blind 先于 eval: 匹配（盲评是建议性维度，归入 blind
# 而非 execution；其余 eval: 检查仍归 execution 维度）
_DIMENSION_PREFIXES: list[tuple[str, str]] = [
    ("blind", ("eval:blind",)),  # v1.4 R3：盲评偏好（建议性，导出时刻折算）
    ("static", ("validation:", "rule:")),
    ("execution", ("runtime:", "eval:")),
    ("coverage", ("coverage:",)),
    ("host", ("host:",)),
    ("trigger", ("trigger:",)),  # v1.4 R1：第六维（导出时刻折算，不回写冻结行）
]


class DeliveryError(Exception):
    """打包失败：版本/报告哈希不一致、空候选、密钥内容扫描命中。"""


#: 审计 C8：导出 ZIP 保留上限（超过后删最旧）
MAX_EXPORTS = 20


def _prune_exports(exports_dir: Path) -> None:
    """清理旧导出：保留最近 MAX_EXPORTS 个 ZIP（按 mtime，最新优先）。"""
    try:
        zips = sorted(
            (p for p in exports_dir.glob("*.zip") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in zips[MAX_EXPORTS:]:
            stale.unlink()
    except OSError:
        pass  # 清理失败不影响导出主流程


def _is_sensitive(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    return any(part in name for part in SENSITIVE_NAME_PARTS)


def _tree_files(root: Path) -> list[tuple[str, bytes]]:
    """收集版本目录实际文件（排除 .manifest.json 与敏感文件）。

    排序按 posix 相对路径字符串（大小写敏感）——与 versioning._tree_hash 的
    规范化算法一致；不能用 sorted(Path)（Windows 上 Path 比较大小写不敏感，
    会导致树哈希与冻结 content_hash 不一致）。
    """
    out: list[tuple[str, bytes]] = []
    for f in root.rglob("*"):
        if not f.is_file() or f.name == ".manifest.json":
            continue
        if "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        rel = f.relative_to(root).as_posix()
        if _is_sensitive(rel):
            continue
        out.append((rel, f.read_bytes()))
    if not out:
        raise DeliveryError("空候选：版本目录没有任何可打包文件")
    out.sort(key=lambda t: t[0])
    return out


def _tree_hash(files: list[tuple[str, bytes]]) -> str:
    """版本树哈希（与 versioning 同算法：路径排序 + 文件哈希拼接再哈希）。"""
    material = "\n".join(
        f"{rel}:{sha256(data).hexdigest()}" for rel, data in sorted(files, key=lambda t: t[0])
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _summarize(results: list[CheckResult]) -> CheckStatus | None:
    """单维度汇总：FAILED > NOT_RUN > PASSED；仅 UNSUPPORTED 视为不阻塞。"""
    if not results:
        return None
    statuses = {r.status for r in results}
    if CheckStatus.FAILED in statuses:
        return CheckStatus.FAILED
    if CheckStatus.NOT_RUN in statuses:
        return CheckStatus.NOT_RUN
    return CheckStatus.PASSED


def _dimensions(checks: CheckReport) -> dict[str, CheckStatus | None]:
    """前缀 → 维度。按 _DIMENSION_PREFIXES 顺序互斥消费：
    更具体的前缀先认领（eval:blind 归 blind），通用前缀（eval:）
    只接其余（eval:summary/eval:case… 仍归 execution）。
    """
    out: dict[str, CheckStatus | None] = {}
    remaining = list(checks.checks)
    for dim, prefixes in _DIMENSION_PREFIXES:
        sub = [c for c in remaining if c.check_id.startswith(prefixes)]
        remaining = [c for c in remaining if not c.check_id.startswith(prefixes)]
        out[dim] = _summarize(sub)
    return out


def _verify_against_version_row(session, version_id: str, version_hash: str,
                                contract: RequirementContract,
                                checks: CheckReport) -> None:
    """版本/报告必须同 hash：与冻结版本行对账（T16 验收 5）。"""
    from app.models import SkillVersion

    row = session.get(SkillVersion, version_id)
    if row is None:
        raise DeliveryError(f"版本不存在，无法对账: {version_id}")
    if row.content_hash != version_hash:
        raise DeliveryError(
            f"版本哈希与磁盘不符（版本 {row.content_hash[:12]}，磁盘 {version_hash[:12]}）："
            "候选在冻结后被修改，拒绝导出"
        )
    if row.contract_json:
        frozen = RequirementContract.model_validate_json(row.contract_json)
        if frozen != contract:
            raise DeliveryError("导出合同与版本行冻结合同不一致，拒绝导出")
    if row.report_json:
        frozen_report = CheckReport.model_validate_json(row.report_json)
        # 审计 A1：host:/eval: checks 是导出时刻的宿主验证/对照评测结果，
        # 不属于冻结内容，对账时剥离（防止验证状态变化误判版本篡改）
        frozen_stripped = [c for c in frozen_report.checks
                           if not _is_export_time_check(c.check_id)]
        incoming_stripped = [c for c in checks.checks
                             if not _is_export_time_check(c.check_id)]
        if frozen_stripped != incoming_stripped:
            raise DeliveryError("导出报告与版本行冻结报告不一致，拒绝导出")


def _is_export_time_check(check_id: str) -> bool:
    """导出时刻才折算的检查前缀（宿主验证 / 对照评测 / 触发验证，不回写冻结行）。"""
    return check_id.startswith(("host:", "eval:", "trigger:"))


def _dependencies(files: list[tuple[str, bytes]]) -> list[str]:
    """按文件后缀推导运行依赖（README 依赖段数据）。"""
    deps: list[str] = []
    exts = {rel.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower() if "." in rel else ""
            for rel, _ in files}
    if "py" in exts:
        deps.append("Python 3（解释器 python，用于运行 scripts/*.py）")
    if "sh" in exts:
        deps.append("bash（用于运行 scripts/*.sh）")
    if "ps1" in exts:
        deps.append("PowerShell 7+（pwsh，用于运行 scripts/*.ps1）")
    if not deps:
        deps.append("无脚本依赖：纯文本技能")
    return deps


def _readme(receipt_notes: list[str], files: list[tuple[str, bytes]],
            dims: dict[str, CheckStatus | None], delivery_status: DeliveryStatus,
            version_id: str, flow_id: str, metadata_root: str) -> str:
    deps = _dependencies(files)
    dim_lines = "\n".join(
        f"- {dim}：{(d.value if d else '未接入（not run）')}"
        for dim, d in dims.items()
    ) or "- （无检查记录）"
    notes_lines = "\n".join(f"- {n}" for n in receipt_notes) or "- （无）"
    file_lines = "\n".join(f"- `{rel}`" for rel, _ in files)
    status_word = "candidate（未验证候选——不得当作验收通过）" \
        if delivery_status == DeliveryStatus.CANDIDATE else "delivered（已交付）"
    return f"""# 技能交付包 {version_id}

- 生成流程：{flow_id}
- 交付状态：**{status_word}**

## 验证维度

{dim_lines}

未验证项说明：
{notes_lines}

## 文件清单

{file_lines}

完整逐文件 sha256 清单见 `{metadata_root}/manifest.json`；收据见 `{metadata_root}/receipt.json`；
需求覆盖与测试报告见 `{metadata_root}/report.json`；工程规则版本见 `{metadata_root}/profile.json`。

## 依赖

{chr(10).join('- ' + d for d in deps)}

## 使用说明

1. 解包本 ZIP，得到完整技能目录（入口文件为 `SKILL.md`）；
2. 将整个目录复制到目标宿主（如 catpaw/codex）的技能目录下；
3. 若含脚本，请先确认上方依赖（解释器可用）再运行；
4. 本包为{('未验证候选' if delivery_status == DeliveryStatus.CANDIDATE else '已验证交付')}：
   {('存在未通过/未运行检查，使用前请先人工核验并完成待验收项。' if delivery_status == DeliveryStatus.CANDIDATE else '必要检查已通过。')}
"""


def _scan_for_secrets(rel: str, data: bytes) -> None:
    """打包前内容扫描兜底：文本文件中出现疑似密钥字样 → 拒绝进包。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return  # 二进制不扫
    markers = ("SKILLFORGE_TOKEN", "API_KEY=", "SECRET_TOKEN", "api_key =")
    for m in markers:
        if m in text:
            raise DeliveryError(f"文件 {rel} 含疑似密钥内容（{m}），拒绝打包")


def _metadata_root(files: list[tuple[str, bytes]]) -> str:
    """Pick a system metadata directory that cannot overwrite skill payload."""
    paths = {rel.casefold() for rel, _ in files}
    candidate = "delivery"
    suffix = 0
    while any(path == candidate.casefold()
              or path.startswith(candidate.casefold() + "/") for path in paths):
        suffix += 1
        candidate = f"delivery-{suffix}"
    return candidate


def _evaluation_usage_receipt(session: Any, version_id: str) -> list[dict[str, Any]]:
    """Persist the separate contrast, judge, and trigger usage scopes in receipt."""
    from app.models import AgentRun, EvalCaseResult, EvalRun

    rows = (session.query(EvalRun).filter_by(version_id=version_id)
            .order_by(EvalRun.created_at.asc()).all())
    from app.generation.flow import get_flow
    from app.models import GenerationFlow, SkillVersion
    version = session.get(SkillVersion, version_id)
    generation_ledger: dict[str, dict[str, Any]] = {}
    if (version is not None and version.generation_flow_id
            and session.get(GenerationFlow, version.generation_flow_id) is not None):
        generation_ledger = dict(get_flow(session, version.generation_flow_id)
                                 .checkpoint.get("budget_ledger") or {})
    usage: list[dict[str, Any]] = []
    for row in rows:
        if row.kind == "trigger":
            try:
                meta = json.loads(row.meta_json or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            observed = meta.get("usage_observed") or {}
            known = int(observed.get("prompt_tokens") or 0) + int(
                observed.get("completion_tokens") or 0)
            ledger_entry = generation_ledger.get(str(meta.get("budget_ledger_key") or ""), {})
            ledger_actual = ledger_entry.get("actual") or {}
            usage.append({
                "eval_run_id": row.id,
                "kind": "trigger",
                "budget": {
                    "per_run_limit": None,
                    "suite_total_cap": None,
                    "actual_tokens": known,
                    "unknown_usage_estimated_tokens": ledger_actual.get("unknown_usage_estimated_tokens"),
                    "usage_missing_calls": int(meta.get("usage_missing_calls") or 0),
                    "cap_hit": None,
                    "cap_scope": "trigger is a separate stage within the generation-flow budget",
                    "budget_ledger_error": meta.get("budget_ledger_error"),
                },
                "cost": {"trigger_tokens": known},
            })
            continue
        try:
            meta = json.loads(row.meta_json or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        result_rows = session.query(EvalCaseResult).filter_by(eval_run_id=row.id).all()
        run_ids = [case.agent_run_id for case in result_rows if case.agent_run_id]
        agent_runs = session.query(AgentRun).filter(AgentRun.id.in_(run_ids)).all() if run_ids else []
        candidate_tokens = sum((r.prompt_tokens or 0) + (r.completion_tokens or 0)
                               for r in agent_runs if r.version_id is not None)
        baseline_tokens = sum((r.prompt_tokens or 0) + (r.completion_tokens or 0)
                              for r in agent_runs if r.version_id is None)
        budget = meta.get("budget") or {}
        blind = meta.get("blind") or {}
        observed = blind.get("usage_observed") or blind.get("usage") or {}
        judge_known = int(observed.get("prompt_tokens") or 0) + int(
            observed.get("completion_tokens") or 0)
        ledger_entry = generation_ledger.get(str(blind.get("budget_ledger_key") or ""), {})
        ledger_actual = ledger_entry.get("actual") or {}
        usage.append({
            "eval_run_id": row.id,
            "kind": "contrast",
            "budget": {
                "per_run_limits_by_case": budget.get("per_run_limits_by_case", {}),
                "suite_total_cap": budget.get("suite_total_cap"),
                "actual_tokens": budget.get("used_tokens", 0),
                "cap_accounted_tokens": budget.get("cap_accounted_tokens", budget.get("used_tokens", 0)),
                "unknown_usage_estimated_tokens": budget.get("unknown_usage_estimated_tokens", 0),
                "usage_missing_calls": budget.get("usage_missing_calls", 0),
                "cap_hit": budget.get("cap_hit", False),
                "judge_unknown_usage_estimated_tokens": ledger_actual.get("unknown_usage_estimated_tokens"),
                "cap_scope": "candidate, baseline, and judge within remaining generation-flow budget",
                "budget_ledger_error": blind.get("budget_ledger_error"),
            },
            "cost": {"candidate_tokens": candidate_tokens,
                     "baseline_tokens": baseline_tokens,
                     "judge_tokens": judge_known,
                     "total_known_tokens": candidate_tokens + baseline_tokens + judge_known,
                     "judge_usage_missing_calls": int(blind.get("usage_missing_calls") or 0)},
        })
    return usage


def _generation_budget_ledger(session: Any, version_id: str) -> dict[str, dict[str, Any]]:
    from app.generation.flow import get_flow
    from app.models import GenerationFlow, SkillVersion

    version = session.get(SkillVersion, version_id)
    if (version is None or not version.generation_flow_id
            or session.get(GenerationFlow, version.generation_flow_id) is None):
        return {}
    snap = get_flow(session, version.generation_flow_id)
    return dict(snap.checkpoint.get("budget_ledger") or {})


def package_delivery(
    flow_id: str,
    version_id: str,
    contract: RequirementContract,
    checks: CheckReport,
    root: Path,
    *,
    profile: EngineeringProfile | None = None,
    session: Any = None,
) -> DeliveryReceipt:
    """把冻结版本打包为完整交付 ZIP，返回验证收据。

    - root：版本目录（磁盘真相，技能文件来源）；
    - ZIP 写到 root.parent.parent / "exports" / {version_id}.zip（版本目录保持不可变）；
    - session 提供时做版本行对账（content_hash / contract / report 同 hash）；
    - 未验证候选照常可导出，但收据/README 显示 candidate 而非验收通过。
    """
    root = Path(root)
    if not root.is_dir():
        raise DeliveryError(f"版本目录不存在: {root}")

    files = _tree_files(root)
    for rel, data in files:
        _scan_for_secrets(rel, data)
    version_hash = _tree_hash(files)

    if session is not None:
        _verify_against_version_row(session, version_id, version_hash, contract, checks)

    dims = _dimensions(checks)
    static_status = dims["static"]
    execution_status = dims["execution"]
    coverage_status = dims["coverage"]
    host_status = dims["host"]
    trigger_status = dims.get("trigger")
    blind_status = dims.get("blind")

    # 验收 3：missing/not_run/failed 任一存在 → candidate，绝不"全通过"
    # trigger / blind 维度除外：它们是建议性维度（R1/R3 底线——触发质量
    # 与盲评偏好不阻塞导出，由收据状态 + notes 如实标注，交用户决定）
    _ADVISORY_PREFIXES = ("trigger:", "eval:blind")
    blocking = [c for c in checks.checks
                if c.status in (CheckStatus.FAILED, CheckStatus.NOT_RUN)
                and not c.check_id.startswith(_ADVISORY_PREFIXES)]
    required_dimensions = {
        "static": static_status,
        "execution": execution_status,
        "coverage": coverage_status,
        "host": host_status,
    }
    clean = not blocking and all(
        status == CheckStatus.PASSED for status in required_dimensions.values()
    )
    delivery_status = DeliveryStatus.DELIVERED if clean else DeliveryStatus.CANDIDATE

    notes: list[str] = []
    for c in blocking:
        notes.append(f"{c.check_id}={c.status.value}：{(c.evidence or '无证据')[:120]}")
    for dimension, status in required_dimensions.items():
        if status is None:
            notes.append(f"{dimension}=未接入：缺少必要验收维度的检查结果")
    if trigger_status is None:
        notes.append("trigger=未接入：触发验证未运行（not run），触发质量未知")
    elif trigger_status == CheckStatus.NOT_RUN:
        notes.append("trigger=触发验证未运行（not run）：任务未执行或失败，"
                     "触发质量未知")
    elif trigger_status != CheckStatus.PASSED:
        notes.append("trigger=触发质量未达标：产物内容合格但宿主可能不选中它，"
                     "建议人工评估 description 或重跑触发优化")
    if blind_status is None:
        notes.append("blind=未接入：盲评未运行（not run），输出主观质量未知")
    elif blind_status == CheckStatus.NOT_RUN:
        notes.append("blind=盲评证据不足：平手/无效裁决或不齐全的双侧输出，"
                     "不构成 candidate 优势结论")
    elif blind_status != CheckStatus.PASSED:
        notes.append("blind=盲评偏好 baseline：盲态评审更偏好基线输出，"
                     "建议人工复核产出质量")
    if not blocking and delivery_status == DeliveryStatus.CANDIDATE:
        notes.append("必要维度未全部通过，按未验证候选导出")
    if delivery_status == DeliveryStatus.DELIVERED and host_status != CheckStatus.PASSED:
        notes.append("宿主维度未通过完整验收：即使 delivered 也非 fully_verified")

    exports_dir = root.parent.parent / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    zip_path = exports_dir / f"{version_id}.zip"

    # 审计 C8：导出产物保留最近 MAX_EXPORTS 个，防止无限累积（同版本覆盖幂等）
    _prune_exports(exports_dir)

    receipt = DeliveryReceipt(
        flow_id=flow_id,
        version_id=version_id,
        delivery_status=delivery_status,
        static_status=static_status,
        execution_status=execution_status,
        coverage_status=coverage_status,
        host_status=host_status,
        trigger_status=trigger_status,
        blind_status=blind_status,
        zip_path=str(zip_path),
        requirement_coverage=[c for c in checks.checks
                              if c.check_id.startswith("coverage:")],
        evaluation_usage=_evaluation_usage_receipt(session, version_id)
            if session is not None else [],
        generation_budget_ledger=_generation_budget_ledger(session, version_id)
            if session is not None else {},
        notes=notes,
    )

    report_bytes = checks.model_dump_json().encode("utf-8")
    report_hash = sha256(report_bytes).hexdigest()

    metadata_root = _metadata_root(files)
    readme = _readme(
        notes, files, dims, delivery_status, version_id, flow_id, metadata_root,
    )

    payload: list[tuple[str, bytes]] = [
        (rel, data) for rel, data in files
    ]
    # Preserve user-authored README verbatim; add the generated package guide
    # under the metadata namespace when the root README already belongs to user.
    if "README.md" not in {rel for rel, _ in files}:
        payload.append(("README.md", readme.encode("utf-8")))
    else:
        payload.append((f"{metadata_root}/README.md", readme.encode("utf-8")))
    payload += [
        (f"{metadata_root}/contract.json", contract.model_dump_json().encode("utf-8")),
        (f"{metadata_root}/report.json", report_bytes),
        (f"{metadata_root}/receipt.json", receipt.model_dump_json().encode("utf-8")),
        (f"{metadata_root}/profile.json", json.dumps(
            profile.model_dump() if profile is not None else {},
            ensure_ascii=False, indent=2,
        ).encode("utf-8")),
    ]

    manifest_files = [
        {"path": rel, "sha256": sha256(data).hexdigest(), "size": len(data)}
        for rel, data in sorted(payload)
    ]
    manifest = {
        "schema_version": "1.1.0",
        "flow_id": flow_id,
        "version_id": version_id,
        "version_hash": version_hash,
        "report_hash": report_hash,
        "metadata_root": metadata_root,
        "files": manifest_files,
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in payload:
            zf.writestr(rel, data)
        zf.writestr(f"{metadata_root}/manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

    # 写后回读校验：包可解压且成员齐全（磁盘真相，不假装写成功）
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise DeliveryError(f"ZIP 写入后校验失败: {bad}")
        names = zf.namelist()
        if len(names) != len(set(names)):
            raise DeliveryError("ZIP 含重复路径，拒绝交付")
        expected = {rel for rel, _ in payload} | {f"{metadata_root}/manifest.json"}
        if set(names) != expected:
            raise DeliveryError("ZIP 成员与预期不符，拒绝交付")
        manifest_data = json.loads(zf.read(f"{metadata_root}/manifest.json"))
        for entry in manifest_data.get("files", []):
            archived = zf.read(entry["path"])
            if (sha256(archived).hexdigest() != entry.get("sha256")
                    or len(archived) != entry.get("size")):
                raise DeliveryError(
                    f"ZIP 文件与 manifest 不符，拒绝交付: {entry.get('path')}"
                )

    return receipt

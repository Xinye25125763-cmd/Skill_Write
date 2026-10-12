"""trigger_run 处理器（v1.4 R1）：触发验证 + description 有界优化。

任务语义：
- 对已冻结版本做触发验证（第六维 trigger）：生成正/负例用例 → 模拟宿主
  选路判定 → 评分（召回率/误触率/总分）→ 低于阈值时有界优化 description
  并重评（≤3 轮，停止原因显式入 meta）；
- 结果持久化：触发用例集（Suite）+ EvalRun(kind=trigger) + 逐例
  EvalCaseResult（group=target），导出时刻按 kind 折算为 trigger: checks；
- 优化产生了更好 description 且验证改善 → 以确定性 frontmatter 替换冻结
  新版本（不可变纪律：改 description 也是新版本，不覆盖原版本行）；
  新版本仅在总分达标时才再排一次触发验证（链长 ≤1，不无限迭代）；
- 触发不合格不阻塞导出（与 eval 折算同语义：如实标注，由用户决定）。

预算：模型调用全记账（stage_id=trigger，需版本有 generation_flow_id；
草稿版本无流程则跳过记账，结果如实可见）。
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from app.core.ulid import new_ulid
from app.generation import budget as budget_svc
from app.generation.trigger import (
    PLACEHOLDER_DISTRACTORS,
    TRIGGER_SCORE_THRESHOLD,
    rewrite_description,
    run_trigger_verification,
)
from app.llm import ProviderNotConfigured, judge_provider_from_env
from app.llm.usage import ModelTokenBudgetExceeded
from app.models import EvalCaseResult, EvalRun, SkillVersion, Suite
from app.services.skill_files import SkillFileService
from app.workers.handlers import HandlerContext, payload_of, register

# 测试可替换的提供商工厂（与 generation_stage / agent_run 同模式）。
# v1.5 M5：默认优先读 SKILLFORGE_JUDGE_LLM_*（回落主模型）——触发
# 用例生成与技能选中判定属验证侧工作负载，可配分级模型
provider_factory = judge_provider_from_env

try:  # VersionNotFound 在 agent_run 模块内定义
    from app.workers.handlers.agent_run import VersionNotFound
except ImportError:  # pragma: no cover
    class VersionNotFound(Exception):  # type: ignore[no-redef]
        pass

MAX_DISTRACTORS = 6
MAX_OPTIMIZATION_CHAIN_DEPTH = 1


class _UsageTrackingProvider:
    """Accumulate provider usage across every trigger/judge/optimization call."""

    def __init__(self, provider, max_tokens_total: int | None = None) -> None:  # noqa: ANN001
        self.provider = provider
        self.capabilities = provider.capabilities
        self.max_tokens_total = max_tokens_total
        self.used_tokens = 0
        self.unknown_usage_estimated_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usage_calls = 0
        self.unknown_calls = 0
        self.calls = 0
        self.responses = 0

    def complete(self, messages, **kwargs):  # noqa: ANN001
        if self.max_tokens_total is not None:
            remaining = self.max_tokens_total - self.used_tokens
            if remaining <= 0:
                raise ModelTokenBudgetExceeded(
                    f"全流程 token 预算已耗尽（触发验证，{self.used_tokens}/{self.max_tokens_total}）"
                )
            requested = kwargs.get("max_tokens")
            kwargs["max_tokens"] = min(int(requested), remaining) if requested else remaining
        self.calls += 1
        try:
            response = self.provider.complete(messages, **kwargs)
        except Exception:
            self.unknown_calls += 1
            estimate = max(0, int(kwargs.get("max_tokens") or 0))
            self.unknown_usage_estimated_tokens += estimate
            self.used_tokens += estimate
            raise
        usage = getattr(response, "usage", None)
        self.responses += 1
        if usage is None:
            self.unknown_calls += 1
            estimate = max(0, int(kwargs.get("max_tokens") or 0))
            self.unknown_usage_estimated_tokens += estimate
            self.used_tokens += estimate
        else:
            self.prompt_tokens += int(usage.prompt_tokens or 0)
            self.completion_tokens += int(usage.completion_tokens or 0)
            self.usage_calls += 1
            self.used_tokens += int(usage.prompt_tokens or 0) + int(usage.completion_tokens or 0)
        return response


def _collect_distractors(
    session, cfg, svc: SkillFileService, project_id: str, exclude_version: str,
) -> list[dict[str, str]]:
    """干扰项：本项目其他已冻结版本的 description（真实混淆源）+ 占位集。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    rows = (
        session.query(SkillVersion)
        .filter(SkillVersion.project_id == project_id,
                SkillVersion.id != exclude_version)
        .order_by(SkillVersion.created_at.desc())
        .limit(10)
        .all()
    )
    for r in rows:
        try:
            md = svc.read_version_file(project_id, r.id, "SKILL.md")
        except Exception:  # noqa: BLE001 版本目录缺失/读取失败 → 跳过
            continue
        from app.generation.trigger import parse_frontmatter
        name, desc = parse_frontmatter(md)
        if name and desc and desc not in seen:
            seen.add(desc)
            out.append({"name": name, "description": desc})
        if len(out) >= MAX_DISTRACTORS:
            break
    for d in PLACEHOLDER_DISTRACTORS:
        if d["description"] not in seen and len(out) < MAX_DISTRACTORS:
            seen.add(d["description"])
            out.append(dict(d))
    return out


def _stage_profile(cfg):  # noqa: ANN001
    """加载工程规则集（与 generation_stage._profile 同锚点规则）。"""
    from app.generation.policy import load_profile
    path = Path(cfg.generation.profile_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    return load_profile(path)


def _freeze_with_description(
    session, cfg, row: SkillVersion, skill_md: str, new_description: str,
) -> str | None:
    """确定性替换 description 后冻结新版本（不可变纪律）。

    新内容先独立静态验证（验证报告属于新版本，不沿用旧报告）；
    验证异常 → 不创建新版本（返回 None，停止原因由调用方记录）。
    仅支持有生成元数据的版本（合同/流程齐全）；草稿版本返回 None。
    """
    from app.generation.artifacts import manifest_hash, publish_candidate, stage_artifacts
    from app.generation.contracts import (
        ArtifactFile, ArtifactKind, ArtifactManifest, RequirementContract,
    )
    from app.generation.validation import validate_candidate
    from app.generation.versioning import freeze_candidate

    if not (row.contract_json and row.generation_flow_id):
        return None
    contract = RequirementContract.model_validate_json(row.contract_json)

    # plan 的需求映射必须进新清单（coverage 静态检查依赖）
    from app.generation.flow import FlowNotFound, get_flow
    req_ids_by_path: dict[str, list[str]] = {}
    try:
        snap = get_flow(session, row.generation_flow_id)
        if snap.plan is not None:
            for f in snap.plan.files:
                req_ids_by_path[f.path] = list(f.requirement_ids or [])
    except FlowNotFound:
        pass  # 流程缺失 → 无法补映射，静态验证会如实报 coverage 缺口

    vdir = Path(cfg.data_dir) / "projects" / row.project_id / "skills" / "versions" / row.id
    new_skill_md = rewrite_description(skill_md, new_description)

    files = json.loads(row.files_json)
    artifacts: list[ArtifactFile] = []
    for f in files:
        data = (vdir / f["path"]).read_bytes()
        req_ids = req_ids_by_path.get(f["path"], [])
        if f["path"] == "SKILL.md":
            content = new_skill_md
            artifacts.append(ArtifactFile(
                path=f["path"], kind=ArtifactKind.TEXT, content=content,
                sha256=_sha256_text(content), size=len(content.encode("utf-8")),
                requirement_ids=req_ids,
            ))
            continue
        kind = ArtifactKind(f.get("kind", "text"))
        if kind == ArtifactKind.TEXT:
            content = data.decode("utf-8")
            artifacts.append(ArtifactFile(
                path=f["path"], kind=kind, content=content,
                sha256=f.get("hash") or _sha256_text(content), size=len(data),
                requirement_ids=req_ids,
            ))
        else:
            artifacts.append(ArtifactFile(
                path=f["path"], kind=kind, resource_id=f["path"],
                sha256=f.get("hash") or "", size=len(data),
                requirement_ids=req_ids,
            ))
    manifest = ArtifactManifest(artifacts=artifacts)

    root = Path(cfg.data_dir) / "generation" / row.project_id
    flow_id = row.generation_flow_id or f"trigger-{row.id}"
    staged = stage_artifacts(flow_id, manifest, {}, root)
    publish_candidate(staged, manifest_hash(manifest), root)

    profile = _stage_profile(cfg)
    report = validate_candidate(manifest, contract, profile, root / "candidate" / flow_id)
    if report.has_failed:
        return None  # 改写后的内容验证不过 → 不冻结（诚实：不交付坏版本）

    return freeze_candidate(
        session, flow_id, manifest, root / "candidate" / flow_id,
        contract=contract, profile_hash=row.profile_hash, report=report,
        cfg=cfg, project_id=row.project_id,
    )


def _sha256_text(content: str) -> str:
    from hashlib import sha256
    return sha256(content.encode("utf-8")).hexdigest()


@register("trigger_run")
def handle(session, cfg, job, ctx: HandlerContext) -> dict:  # noqa: ANN001
    payload = payload_of(job)
    project_id = str(payload["project_id"])
    version_id = str(payload["version_id"])
    optimization_depth = max(0, int(payload.get("optimization_depth") or 0))

    row = session.get(SkillVersion, version_id)
    if row is None or row.project_id != project_id:
        raise VersionNotFound(f"版本不存在: {version_id}")

    svc = SkillFileService(cfg, session)
    skill_md = svc.read_version_file(project_id, version_id, "SKILL.md")

    contract = None
    if row.contract_json:
        from app.generation.contracts import RequirementContract
        contract = RequirementContract.model_validate_json(row.contract_json)

    distractors = _collect_distractors(session, cfg, svc, project_id, version_id)
    ctx.event("start", f"触发验证：{version_id}（干扰项 {len(distractors)} 个）")

    flow_id = row.generation_flow_id
    flow_allowance = None
    if flow_id:
        flow_snap = budget_svc.flow_store.get_flow(session, flow_id)
        token_cap = (flow_snap.budget.max_tokens_total
                     if flow_snap.budget.max_tokens_total is not None
                     else flow_snap.budget.total_tokens)
        flow_allowance = max(0, token_cap - flow_snap.budget.used_tokens)
    provider = _UsageTrackingProvider(provider_factory(), flow_allowance)
    result = run_trigger_verification(
        provider, skill_md, contract, distractors, rng=random.Random(),
    )

    # 预算记账（版本有生成流程才记；草稿版本跳过）
    observed_usage = {
        "prompt_tokens": provider.prompt_tokens,
        "completion_tokens": provider.completion_tokens,
    }
    usage_complete = provider.unknown_calls == 0
    attempt_id = f"{job.id}:{int(job.attempt or 1)}"
    result_meta = {
        "model_calls": provider.calls,
        "budget_ledger_key": f"trigger@{attempt_id}",
        "usage": observed_usage if usage_complete and provider.usage_calls else None,
        "usage_observed": observed_usage,
        "usage_calls": provider.usage_calls,
        "usage_missing_calls": provider.unknown_calls,
        "unknown_usage_estimated_tokens": provider.unknown_usage_estimated_tokens,
        "limit_tokens": provider.max_tokens_total,
        "accounted_tokens": provider.used_tokens,
        "over_limit": (provider.max_tokens_total is not None
                       and provider.used_tokens > provider.max_tokens_total),
        "overrun_tokens": (max(0, provider.used_tokens - provider.max_tokens_total)
                           if provider.max_tokens_total is not None else 0),
        "budget_exhausted": provider.max_tokens_total is not None
            and provider.used_tokens >= provider.max_tokens_total,
    }
    if flow_id:
        try:
            budget_svc.reserve_stage_budget(
                session, flow_id, "trigger", attempt_id=attempt_id,
            )
            budget_svc.record_stage_usage(
                session, flow_id, "trigger", budget_svc.usage_from_meta(result_meta),
                attempt_id=attempt_id,
            )
        except Exception as exc:  # noqa: BLE001: persist explicit ledger error with trigger evidence
            session.rollback()
            result_meta["budget_ledger_error"] = f"{type(exc).__name__}: {exc}"

    if provider.calls and provider.responses == 0:
        # A failed provider request is not evidence that the skill failed
        # routing. Keep its unknown cost in the flow ledger, but leave the
        # trigger quality check as not_run instead of manufacturing negatives.
        ctx.event("budget_exceeded", "触发验证请求未返回结果；已记账并停止后续调用")
        return {"outcome": "not_run", "reason": "provider_failed_without_response",
                "budget": result_meta}

    # 持久化：触发用例集 + EvalRun(kind=trigger) + 逐例结果
    cases_json = [
        {"id": c["id"], "prompt": c["prompt"], "expect": c["expect"]}
        for c in result["cases"]
    ]
    suite = Suite(
        id=new_ulid(),
        project_id=project_id,
        name=f"触发用例集（{version_id}）",
        cases_json=json.dumps(cases_json, ensure_ascii=False),
        created_at=time.time(),
    )
    session.add(suite)
    session.commit()

    ev = EvalRun(
        id=new_ulid(),
        project_id=project_id,
        suite_id=suite.id,
        version_id=version_id,
        status="running",
        kind="trigger",
        meta_json=json.dumps({
            "skill_name": result["skill_name"],
            "initial_score": result["initial_score"],
            "score": result["score"],
            "rounds": result["rounds"],
            "stopped_reason": result["stopped_reason"],
            "description_changed": result["description_changed"],
            "final_description": result["final_description"],
            "threshold": TRIGGER_SCORE_THRESHOLD,
            "optimization_depth": optimization_depth,
            **result_meta,
        }, ensure_ascii=False),
        created_at=time.time(),
    )
    session.add(ev)
    session.commit()

    skill_name = result["skill_name"]
    for c in result["cases"]:
        selected = result["selections"].get(c["id"])
        is_hit = c["expect"] == "hit"
        correct = (selected == skill_name) if is_hit else (selected != skill_name)
        session.add(EvalCaseResult(
            id=new_ulid(),
            eval_run_id=ev.id,
            case_id=c["id"],
            group_name="target",
            exec_status="executed",
            exit_reason="completed",
            score_status="scored",
            assertions_json=json.dumps([{
                "type": "trigger_selected_is", "description": c["prompt"],
                "passed": correct, "detail": f"selected={selected!r}",
            }], ensure_ascii=False),
            passed_count=1 if correct else 0,
            total_count=1,
            score_error=None,
            created_at=time.time(),
        ))
    ev.status = "finished"
    ev.finished_at = time.time()
    session.commit()

    # 优化有效（description 变了且分数提升）→ 冻结改进版本（不可变纪律）
    new_version_id: str | None = None
    if (optimization_depth < MAX_OPTIMIZATION_CHAIN_DEPTH
            and result["description_changed"]
            and result["score"]["overall"] > result["initial_score"]["overall"]):
        try:
            new_version_id = _freeze_with_description(
                session, cfg, row, skill_md, result["final_description"],
            )
        except Exception:  # noqa: BLE001 冻结失败如实记录，不拖垮验证结果
            new_version_id = None
        # 新版本达标才再排一次触发验证（链长 ≤1）
        if (new_version_id and new_version_id != version_id
                and result["score"]["overall"] >= TRIGGER_SCORE_THRESHOLD):
            from app.services import queue
            queue.enqueue(
                session, "trigger_run",
                {"project_id": project_id, "version_id": new_version_id,
                 "optimization_depth": optimization_depth + 1},
                idempotency_key=f"trigger:opt:{new_version_id}",
            )
            session.commit()

    ctx.event(
        "finished",
        f"触发验证完成：总分 {result['score']['overall']}"
        f"（轮次 {result['rounds']}，停止 {result['stopped_reason'] or '达标'}）",
    )
    return {
        "eval_run_id": ev.id,
        "suite_id": suite.id,
        "version_id": version_id,
        "skill_name": skill_name,
        "score": result["score"],
        "initial_score": result["initial_score"],
        "rounds": result["rounds"],
        "stopped_reason": result["stopped_reason"],
        "description_changed": result["description_changed"],
        "final_description": result["final_description"],
        "new_version_id": new_version_id,
        "model_calls": result["model_calls"],
    }


def trigger_checks_report(session, version_id: str):  # noqa: ANN001
    """把最近一次已完成触发验证转成 CheckReport（导出时刻折算用）。

    与 evaluate_candidate 同模式：逐例 trigger:case:{case_id}，
    汇总 trigger:summary 带 recall/false_rate/rounds/stopped_reason 证据。
    无已完成运行 → not_run（未运行的检查不能 passed）。
    """
    from app.generation.contracts import CheckReport, CheckResult, CheckStatus

    ev = (
        session.query(EvalRun)
        .filter(EvalRun.kind == "trigger", EvalRun.version_id == version_id,
                EvalRun.status.in_(("finished", "failed")))
        .order_by(EvalRun.created_at.desc())
        .first()
    )
    if ev is None:
        return CheckReport(checks=[CheckResult(
            check_id="trigger:summary", status=CheckStatus.NOT_RUN,
            evidence="尚无已完成的触发验证（未运行的检查不能 passed）",
        )])

    cases = (
        session.query(EvalCaseResult)
        .filter(EvalCaseResult.eval_run_id == ev.id)
        .order_by(EvalCaseResult.created_at)
        .all()
    )
    meta = json.loads(ev.meta_json or "{}")
    threshold = float(meta.get("threshold") or TRIGGER_SCORE_THRESHOLD)
    score = meta.get("score") or {}

    results: list[CheckResult] = []
    for c in cases:
        if c.score_status != "scored":
            results.append(CheckResult(
                check_id=f"trigger:case:{c.case_id}",
                status=CheckStatus.NOT_RUN,
                evidence=f"判定失败（计入分母）: {c.score_error or ''}",
            ))
            continue
        passed = (c.passed_count or 0) >= 1 and (c.total_count or 0) >= 1
        results.append(CheckResult(
            check_id=f"trigger:case:{c.case_id}",
            status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
            evidence=(json.loads(c.assertions_json) or [{}])[0].get("detail", ""),
        ))
    overall = score.get("overall")
    summary_status = (
        CheckStatus.PASSED if overall is not None and float(overall) >= threshold
        else (CheckStatus.FAILED if results else CheckStatus.NOT_RUN)
    )
    stopped = meta.get("stopped_reason") or "达标"
    evidence = (
        f"总分 {overall}（阈值 {threshold}）；"
        f"召回率 {score.get('recall', '—')}、误触率 {score.get('false_rate', '—')}"
        f"（正例 {score.get('hit_ok', 0)}/{score.get('hits', 0)} 命中，"
        f"负例误选 {score.get('misses', 0) - score.get('miss_ok', 0)}"
        f"/{score.get('misses', 0)}）；"
        f"优化轮次 {meta.get('rounds', 0)}（停止原因：{stopped}）；"
        f"模型调用 {meta.get('model_calls', 0)} 次，"
        f"已知用量 {int((meta.get('usage_observed') or {}).get('prompt_tokens') or 0) + int((meta.get('usage_observed') or {}).get('completion_tokens') or 0):,} tokens"
        + (f"；另有 {meta.get('usage_missing_calls')} 次用量未知"
           if meta.get("usage_missing_calls") else "")
    )
    results.append(CheckResult(
        check_id="trigger:summary", status=summary_status, evidence=evidence,
    ))
    return CheckReport(checks=results)

"""agent_run 处理器（S5）：展开版本快照 → 工具循环 → 落执行记录。

任务语义：
- 基础设施失败（未配置提供商、版本不存在）→ job failed；
- 循环退出原因（completed/budget_exceeded/tool_timeout/model_failed/cancelled）
  是执行结果而非基础设施失败 → job succeeded，run.exit_reason 记录之。

S6 起 execute_prompt() 被 eval_run 处理器复用（每"用例×组"一次执行）。
"""
import json
import time
from pathlib import Path

from app.core.ulid import new_ulid
from app.executors import ApiExecutor
from app.llm import ProviderNotConfigured, provider_from_env
from app.models import AgentRun
from app.services.skill_files import SkillFileService
from app.workers.handlers import HandlerContext, payload_of, register

# 测试可替换的提供商工厂（生产读环境变量；与 generation_stage 同模式）
provider_factory = provider_from_env

BASELINE_SYSTEM_PROMPT = (
    "你是一个通用任务执行助手。你可以调用工具读写文件和执行脚本来完成用户的任务。"
    "完成后直接给出最终答复，不再调用工具。"
)

SKILL_SYSTEM_TEMPLATE = (
    "你是一个任务执行助手，并且装配了以下技能（SKILL.md）。"
    "严格按照技能说明完成任务；必要时可调用工具读写文件和执行脚本。"
    "完成后直接给出最终答复，不再调用工具。\n\n--- SKILL.md ---\n{skill_md}"
)

# v1.5 M2：执行预算默认收紧（200k/40 轮 → 80k/15 轮）。
# 依据：真实链路 97 次运行实测——completed 平均 62k token / 中位 22 步，
# 旧默认下 budget_exceeded 运行平均踱到 216k 才止损（占总消耗 80%）。
# 新默认覆盖成功路径，撞墙运行在 1/3 成本处止损。
# 需要长任务时显式传参（runs/evals API 均支持 max_tokens_total/max_rounds）。
MAX_TOKENS_TOTAL_DEFAULT = 80_000
MAX_ROUNDS_DEFAULT = 15


class VersionNotFound(Exception):
    """payload 指定的版本不存在。"""


def _step_to_dict(step) -> dict:  # noqa: ANN001
    return {
        "kind": step.kind,
        "name": step.name,
        "arguments": step.arguments,
        "content": step.content,
        "output": step.output,
        "output_id": step.output_id,
        "output_length": step.output_length,
        "usage": None
        if step.usage is None
        else {
            "prompt_tokens": step.usage.prompt_tokens,
            "completion_tokens": step.usage.completion_tokens,
        },
        "duration_ms": step.duration_ms,
    }


def run_view(run: AgentRun) -> dict:
    """AgentRun 行 → API 视图。"""
    return {
        "id": run.id,
        "project_id": run.project_id,
        "version_id": run.version_id,
        "prompt": run.prompt,
        "status": run.status,
        "exit_reason": run.exit_reason,
        "final_message": run.final_message,
        "steps": json.loads(run.steps_json),
        "usage": {
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
        },
        "error_detail": run.error_detail,
        "sandbox_files": json.loads(run.sandbox_files_json),
        "budget": json.loads(run.budget_json),
        "sandbox_dir": run.sandbox_dir,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


def _prepare_sandbox(session, cfg, project_id: str, version_id: str | None, sandbox: Path) -> str:
    """把版本快照展开进沙箱；无版本（基线）时沙箱为空。返回 system prompt。"""
    if version_id is None:
        return BASELINE_SYSTEM_PROMPT

    svc = SkillFileService(cfg, session)
    version = svc.get_version(project_id, version_id)
    if version is None:
        raise VersionNotFound(f"版本不存在: {version_id}")
    files = json.loads(version.files_json)
    for f in files:
        rel = f["path"]
        content = svc.read_version_file(project_id, version_id, rel)
        target = sandbox.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    skill_md = sandbox.joinpath("SKILL.md").read_text(encoding="utf-8")
    return SKILL_SYSTEM_TEMPLATE.format(skill_md=skill_md)


def execute_prompt(
    session,  # noqa: ANN001
    cfg,  # noqa: ANN001
    ctx: HandlerContext,
    *,
    project_id: str,
    version_id: str | None,
    prompt: str,
    max_tokens_total: int | None = None,
    max_rounds: int | None = None,
) -> AgentRun:
    """执行一次"提示词 × 版本"：建沙箱 → 工具循环 → 落 AgentRun 行并返回。

    F08③：基础设施前置（版本展开、provider 构造）先于 AgentRun 建行，
    失败时不会遗留 running 行；执行器循环整体兑底，任何意外异常
    都会以 model_failed 收尾落库，AgentRun 不再永久停在 running。
    """
    run_id = new_ulid()
    sandbox = cfg.data_dir / "projects" / project_id / "runs" / run_id / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)

    system_prompt = _prepare_sandbox(session, cfg, project_id, version_id, sandbox)
    provider = provider_factory()  # F08③：前置到建 run 行之前，未配置时不遗留 running 行

    run = AgentRun(
        id=run_id,
        project_id=project_id,
        version_id=version_id,
        prompt=prompt,
        status="running",
        created_at=time.time(),
        sandbox_dir=str(sandbox),
    )
    session.add(run)
    session.commit()

    def on_step(step) -> None:  # noqa: ANN001
        ctx.touch()
        ctx.event(
            "step",
            f"{step.kind}" + (f":{step.name}" if step.name else ""),
            progress={
                "run_id": run_id,
                "kind": step.kind,
                "tool": step.name,
            },
        )

    executor = ApiExecutor(
        provider,
        system_prompt=system_prompt,
        user_prompt=prompt,
        sandbox=sandbox,
        max_tokens_total=max_tokens_total or MAX_TOKENS_TOTAL_DEFAULT,
        max_rounds=max_rounds or MAX_ROUNDS_DEFAULT,
        on_step=on_step,
        persist_tool_output=lambda output_id, call_id, name, content: _persist_tool_output(
            session, run_id, output_id, call_id, name, content
        ),
        read_tool_output=lambda output_id: _read_tool_output(session, run_id, output_id),
    )
    try:
        result = executor.run(is_cancelled=ctx.is_cancelled)
    except Exception as exc:  # noqa: BLE001 F08③：意外异常不得让 run 行永久 running
        session.rollback()
        run.status = "finished"
        run.exit_reason = "model_failed"
        run.error_detail = f"执行器意外异常: {type(exc).__name__}: {exc}"
        run.prompt_tokens = executor.total_usage.prompt_tokens
        run.completion_tokens = executor.total_usage.completion_tokens
        run.budget_json = json.dumps({
            "limit_tokens": executor.budget.max_tokens_total,
            "used_tokens": executor.budget.used_tokens,
            "over_limit": executor.budget.used_tokens > executor.budget.max_tokens_total,
            "overrun_tokens": max(0, executor.budget.used_tokens - executor.budget.max_tokens_total),
            "model_calls": executor.request_calls,
            "usage_missing_calls": executor.usage_missing_calls,
            "unknown_usage_estimated_tokens": executor.unknown_usage_estimated_tokens,
            "accounted_tokens": executor.budget.used_tokens + executor.unknown_usage_estimated_tokens,
        }, ensure_ascii=False)
        run.finished_at = time.time()
        session.commit()
        session.refresh(run)
        return run

    run.status = "finished"
    run.exit_reason = result.exit_reason.value
    run.final_message = result.final_message
    run.steps_json = json.dumps([_step_to_dict(s) for s in result.steps], ensure_ascii=False)
    run.prompt_tokens = result.total_usage.prompt_tokens
    run.completion_tokens = result.total_usage.completion_tokens
    run.error_detail = result.error_detail
    run.sandbox_files_json = json.dumps(result.sandbox_files, ensure_ascii=False)
    # v1.5 M3B：历史窗口化统计并入 budget_json（keep/压缩条数/省掉字符数）
    budget_dict = dict(result.budget)
    if result.history_window:
        budget_dict["history_window"] = result.history_window
    run.budget_json = json.dumps(budget_dict, ensure_ascii=False)
    run.finished_at = time.time()
    session.commit()
    session.refresh(run)
    return run


def _persist_tool_output(session, run_id, output_id, call_id, name, content) -> None:  # noqa: ANN001
    from app.models import AgentRunToolOutput

    session.add(AgentRunToolOutput(
        output_id=output_id, run_id=run_id, call_id=call_id, tool_name=name,
        content=content, content_length=len(content), created_at=time.time(),
    ))
    # 每个输出独立提交：进程崩溃时，已生成的完整内容仍可恢复。
    session.commit()


def _read_tool_output(session, run_id: str, output_id: str) -> str | None:
    from app.models import AgentRunToolOutput

    item = session.get(AgentRunToolOutput, output_id)
    if item is None or item.run_id != run_id:
        return None
    return item.content


@register("agent_run")
def handle(session, cfg, job, ctx: HandlerContext) -> dict:  # noqa: ANN001
    payload = payload_of(job)
    ctx.event("prepare", "展开技能快照到沙箱")
    run = execute_prompt(
        session,
        cfg,
        ctx,
        project_id=payload["project_id"],
        version_id=payload.get("version_id"),
        prompt=payload["prompt"],
        max_tokens_total=payload.get("max_tokens_total"),
        max_rounds=payload.get("max_rounds"),
    )
    ctx.event(
        "finished",
        f"退出原因: {run.exit_reason}",
        progress={"run_id": run.id, "exit_reason": run.exit_reason},
    )
    return run_view(run)

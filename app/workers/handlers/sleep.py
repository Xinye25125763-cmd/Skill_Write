"""sleep 处理器：测试专用（取消与恢复测试需要可观察的长任务）。"""
import time

from app.workers.handlers import JobCancelled, HandlerContext, payload_of, register


@register("sleep")
def handle(session, cfg, job, ctx: HandlerContext) -> dict:  # noqa: ANN001
    seconds = float(payload_of(job).get("seconds", 1))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ctx.is_cancelled():
            raise JobCancelled()
        ctx.touch()
        time.sleep(0.2)
    return {"slept": seconds}

"""需求前置 skill 与工作台桥接客户端（工程计划 §5 T19）。

设计要点：
- forge_client.submit() 是显式调用入口：传入 request_text + 项目 ID + 后端地址 + 令牌，
  返回 FlowSnapshot 视图；非显式调用（缺 request_text）→ ValueError，不误触发生成；
- 令牌仅走 HTTP 请求头，绝不出现在输出/日志/报告（验收 4）；
- 服务不可达 → ConnectionError 附可读提示（验收 3）；
- 完整需求直接进入提取，无多余轮次（验收 5）。
"""
from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def _http_post(url: str, body: bytes, token: str, *, timeout: int = 30) -> dict[str, Any]:
    """可 patch 的网络层：生产用 urllib，测试可替换为 TestClient。"""
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-session-token", token)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class BridgeError(Exception):
    """桥接错误：服务端拒绝请求（鉴权失败/项目不存在/状态冲突等）。"""


def submit(
    request: dict[str, Any],
    base_url: str,
    token: str,
    project_id: str,
) -> dict[str, Any]:
    """显式调用：交接需求到 skill-forge 工作台，返回 FlowSnapshot 视图。

    - request 必须含 request_text（否则 ValueError，不误触发生成）；
    - token 仅走 x-session-token 头，绝不写入输出/报告/日志；
    - base_url 不可达 → ConnectionError 附可读提示。
    """
    request_text = request.get("request_text")
    if not isinstance(request_text, str) or not request_text.strip():
        raise ValueError(
            "request_text 缺失或为空：显式调用必须提供需求描述，"
            "已有 skill / 一次性任务不误进 skill-forge 创建"
        )

    url = f"{base_url.rstrip('/')}/api/projects/{project_id}/generation-flows"
    body = json.dumps(request).encode("utf-8")

    try:
        data = _http_post(url, body, token)
    except HTTPError as exc:
        # 审计 B6：服务端明确拒绝（401/404/409/422…）≠ 连接失败——
        # 附状态码与响应体片段，报可定位的错误，不误导排查方向
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001 响应体读取失败不影响错误分类
            pass
        hint = {
            401: "会话令牌无效或过期，请重新获取 token",
            403: "无权访问该项目",
            404: "项目或端点不存在，请检查 project_id 与 base_url",
        }.get(exc.code, "请检查请求参数与工作台状态")
        raise BridgeError(
            f"skill-forge 工作台拒绝请求（HTTP {exc.code} {exc.reason}）：{hint}。"
            f"响应：{detail or '（空）'}"
        ) from exc
    except (URLError, OSError) as exc:
        raise ConnectionError(
            f"skill-forge 工作台连接失败（{base_url}）："
            f"请确认后端已启动且端口正确。原始错误：{exc}"
        ) from exc

    return data


class _ForgeClient:
    """模块级单例，方便 `from app.generation.skill_bridge import forge_client`。"""

    submit = staticmethod(submit)


forge_client = _ForgeClient()

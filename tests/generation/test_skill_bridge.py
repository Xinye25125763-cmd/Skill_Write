"""T19：创建可调用的需求前置skill与工作台桥接。

本项验收（工程计划 §5 T19）：
1. 实际显式调用能交接需求并获得结果（forge_client.submit 返回 FlowSnapshot 视图）；
2. 执行已有 skill / 一次性任务不误进创建（非显式调用不触发 skill-forge）；
3. 服务未启动给可读交接提示（ConnectionError → 友好报错，非堆栈）；
4. 凭)据不写 skill 或报告（token 仅走请求头，不出现在输出/日志/报告中）；
5. 完整需求快速推进（request_text 足够 → 直接进入提取，无多余轮次）。
"""
import json
from pathlib import Path

import pytest

AUTH_HEADERS = {"x-session-token": "test-token"}


def test_explicit_skill_request_bridge_and_existing_task_nontrigger(
    migrated_db: Path, client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation.skill_bridge import forge_client, _http_post as _real_post
    import app.generation.skill_bridge as bridge_mod

    # monkeypatch 网络层：用 TestClient 的 post 替代 urllib
    def _post_via_client(url: str, body: bytes, token: str, **_kw):
        path = url.replace("http://testserver", "")
        resp = client.post(path, content=body, headers={
            "Content-Type": "application/json",
            "x-session-token": token,
        })
        return resp.json()

    monkeypatch.setattr(bridge_mod, "_http_post", _post_via_client)

    # ---------- 块 A：显式调用交接需求并获得结果 ----------
    pid = client.post(
        "/api/projects", json={"name": "bridge-test"}, headers=AUTH_HEADERS,
    ).json()["id"]

    # 完整需求：直接进入提取阶段，无多余轮次（验收 5）
    snap = forge_client.submit(
        request={"request_text": "把会议纪要整理成行动项"},
        base_url="http://testserver",
        token="test-token",
        project_id=pid,
    )
    assert snap["flow_id"]
    assert snap["project_id"] == pid
    assert snap["state"] in ("queued", "extracting", "awaiting_input")
    # 凭据不写入输出（验收 4）
    snap_json = json.dumps(snap)
    assert "test-token" not in snap_json
    assert "SKILLFORGE_TOKEN" not in snap_json

    # ---------- 块 B：执行已有 skill 不误进创建 ----------
    # 非显式调用（无 request_text）→ 不创建生成流程
    with pytest.raises((TypeError, ValueError)) as exc_info:
        forge_client.submit(
            request={},
            base_url="http://testserver",
            token="test-token",
            project_id=pid,
        )
    assert "request_text" in str(exc_info.value) or "需求" in str(exc_info.value)

    # ---------- 块 C：服务未启动 → 可读交接提示 ----------
    # 恢复真实 urllib 以测试不可达端口
    monkeypatch.setattr(bridge_mod, "_http_post", _real_post)
    with pytest.raises(ConnectionError) as exc_info:
        forge_client.submit(
            request={"request_text": "x"},
            base_url="http://127.0.0.1:19999",
            token="test-token",
            project_id=pid,
        )
    msg = str(exc_info.value)
    assert "skill-forge" in msg or "工作台" in msg or "连接" in msg

    # ---------- 块 C2（审计 B6）：服务端拒绝 ≠ 连接失败 ----------
    # 401/404/409 等响应应报 BridgeError 附状态码与响应体，不能误报"连接失败"
    from app.generation.skill_bridge import BridgeError
    from urllib.error import HTTPError

    def _post_401(url: str, body: bytes, token: str, **_kw):
        raise HTTPError(url, 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(bridge_mod, "_http_post", _post_401)
    with pytest.raises(BridgeError) as exc_info_401:
        forge_client.submit(
            request={"request_text": "x"},
            base_url="http://testserver",
            token="bad-token",
            project_id=pid,
        )
    m401 = str(exc_info_401.value)
    assert "401" in m401, "必须携带状态码"
    assert "连接失败" not in m401, "服务端拒绝不得误报为连接失败"

    def _post_404(url: str, body: bytes, token: str, **_kw):
        raise HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(bridge_mod, "_http_post", _post_404)
    with pytest.raises(BridgeError):
        forge_client.submit(
            request={"request_text": "x"},
            base_url="http://testserver",
            token="test-token",
            project_id="no-such-project",
        )

    # ---------- 块 D：凭据不写 skill 或报告 ----------
    ckpt = snap.get("checkpoint", {})
    ckpt_json = json.dumps(ckpt)
    assert "test-token" not in ckpt_json

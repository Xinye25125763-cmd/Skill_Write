"""一键启动器：迁移 → 启动自检 → 端口预检 → 启动 API → 打开浏览器。

令牌经 URL 片段（#token=...）传给前端：片段不会随请求发往服务器、
不进服务器日志；前端读取后存 localStorage 并从地址栏清除。
"""
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config as AlembicConfig

from app.core.config import load_config
from app.core.db import run_startup_checks

ROOT = Path(__file__).resolve().parent


class PortInUseError(RuntimeError):
    """目标端口已被占用。"""


def ensure_port_free(host: str, port: int) -> None:
    """启动前预检端口，避免 uvicorn 报晦涩错误。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError as exc:
            msg = f"端口 {host}:{port} 已被占用，请修改 config.yaml 的 server.port。"
            raise PortInUseError(msg) from exc


def run_migrations() -> None:
    """将数据库升到最新迁移版本。"""
    alembic_cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")


def wait_for_api(base_url: str, timeout: float = 30.0) -> None:
    """轮询 /api/health 直到 API 就绪。"""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/api/health", timeout=1.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.3)
    raise TimeoutError(f"API 未在 {timeout:.0f}s 内就绪：{base_url}（{last_error}）")


def main() -> int:
    cfg = load_config()
    base_url = f"http://{cfg.server.host}:{cfg.server.port}"

    try:
        ensure_port_free(cfg.server.host, cfg.server.port)
    except PortInUseError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    run_startup_checks(cfg)
    run_migrations()
    from app.services.projects import reconcile_project_trash
    cleanup = reconcile_project_trash(cfg)
    if cleanup["pending"]:
        print(f"删除暂存区有 {cleanup['pending']} 项待清理；请查看 data_dir/.trash", file=sys.stderr)

    token = secrets.token_urlsafe(32)
    env = {**os.environ, "SKILLFORGE_TOKEN": token}
    api_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.api.main:app",
            "--host", cfg.server.host, "--port", str(cfg.server.port),
        ],
        cwd=ROOT,
        env=env,
    )
    worker_proc = subprocess.Popen(
        [sys.executable, "-m", "app.workers.worker"],
        cwd=ROOT,
        env=env,
    )

    try:
        wait_for_api(base_url)
        webbrowser.open(f"{base_url}/#token={token}")
        print(f"skill-forge 已启动：{base_url}/#token={token}")
        print("API 进程与 Worker 进程已拉起；会话令牌经 URL 片段交给前端；Ctrl+C 退出。")
        api_proc.wait()
    except KeyboardInterrupt:
        print("正在退出…")
        for proc in (worker_proc, api_proc):
            proc.terminate()
        for proc in (worker_proc, api_proc):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        if worker_proc.poll() is None:
            worker_proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

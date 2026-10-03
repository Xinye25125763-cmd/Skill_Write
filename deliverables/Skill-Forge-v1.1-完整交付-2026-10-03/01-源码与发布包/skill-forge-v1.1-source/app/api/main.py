"""FastAPI 应用工厂：本地单用户边界（回环绑定 + Origin 校验 + 会话令牌）。

安全模型（主§13）：
    - /api/health 免鉴权，供启动探活；
    - 其余 /api/* 要求 x-session-token 头，令牌由启动器生成；
    - 携带非本源 Origin 的请求一律 403，防止网页跨站调用本地服务。
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app.api.projects import router as projects_router
from app.api.skills import router as skills_router
from app.api.jobs import router as jobs_router
from app.api.runs import router as runs_router
from app.api.evals import router as evals_router

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

API_VERSION = "1.1.0"


def origin_allowed(origin: str) -> bool:
    """F06：解析 Origin 后精确比较 scheme + 主机名（回环）。

    端口不限（本地服务端口动态）；但主机名必须精确等于回环地址，
    避免字符串前缀把 http://127.0.0.1.evil.com 误判为本源。
    """
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    return host in LOOPBACK_HOSTS


def session_token() -> str:
    """当前会话令牌；由启动器经环境变量注入。"""
    return os.environ.get("SKILLFORGE_TOKEN", "")


def create_app() -> FastAPI:
    """构建应用实例。"""
    app = FastAPI(title="skill-forge", version=API_VERSION)

    @app.middleware("http")
    async def guard_local(request: Request, call_next):  # noqa: ANN202
        origin = request.headers.get("origin")
        if origin and not origin_allowed(origin):
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "forbidden_origin", "message": "origin not allowed"}},
            )

        path = request.url.path
        if path.startswith("/api") and path != "/api/health":
            expected = session_token()
            # SSE：EventSource 不能自定义请求头，令牌走查询参数（同源下可接受）
            supplied = request.headers.get("x-session-token", "")
            if path == "/api/events/stream":
                supplied = supplied or request.query_params.get("token", "")
            if not expected or supplied != expected:
                return JSONResponse(
                    status_code=401,
                    content={"error": {"code": "unauthorized", "message": "invalid session token"}},
                )
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict:
        """启动探活端点（免鉴权）。"""
        return {"status": "ok", "version": API_VERSION}

    app.include_router(projects_router)
    app.include_router(skills_router)
    app.include_router(jobs_router)
    app.include_router(runs_router)
    app.include_router(evals_router)

    @app.get("/api/me")
    def me() -> dict:
        """鉴权连通性端点：前端校验令牌用。"""
        return {"auth": "ok"}

    if (FRONTEND_DIST / "index.html").exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    else:
        @app.get("/")
        def no_frontend() -> dict:
            return {
                "message": "frontend not built",
                "hint": "cd frontend && npm install && npm run build",
            }

    return app


app = create_app()

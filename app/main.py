"""cili-server FastAPI 装配入口。"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.db import connect, init_schema
from app.errors import ApiError
from app.routes.admin import router as admin_router
from app.routes.auth import router as auth_router
from app.routes.config import router as config_router
from app.routes.git_http import router as git_router
from app.routes.public import router as public_router
from app.services.admin_auth import seed_initial_admin
from app.settings import Settings, load_settings
from app.sms import build_provider
from app.sms.base import (
    SmsConfigError,
    SmsError,
    SmsInvalidPhone,
    SmsProvider,
    SmsSendError,
)
from app.sms.verification import (
    SmsCodeAttemptsExceeded,
    SmsCodeExpired,
    SmsRateLimited,
    SmsVerificationError,
    SmsVerificationService,
)

logger = logging.getLogger(__name__)


def create_app(
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    provider: SmsProvider | None = None,
    git_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """创建 app；测试可注入 settings / conn / provider / git_client。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or load_settings()
        db_conn = conn or connect(cfg.database_path)
        init_schema(db_conn)
        seed_initial_admin(db_conn, cfg.admin)
        sms_provider = provider or build_provider(cfg)
        forward_client = git_client or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=False,
        )

        app.state.settings = cfg
        app.state.conn = db_conn
        app.state.sms_provider = sms_provider
        app.state.sms_service = SmsVerificationService(
            db_conn, sms_provider, cfg.verification
        )
        app.state.git_client = forward_client
        try:
            yield
        finally:
            if provider is None and hasattr(sms_provider, "close"):
                sms_provider.close()
            if git_client is None:
                await forward_client.aclose()
            if conn is None:
                db_conn.close()

    app = FastAPI(title="cili-server", version="0.1.0", lifespan=lifespan)
    app.include_router(public_router)
    app.include_router(auth_router)
    app.include_router(config_router)
    app.include_router(admin_router)
    app.include_router(git_router)

    admin_page = Path(__file__).resolve().parent / "templates" / "admin.html"

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_ui():
        return FileResponse(admin_page, media_type="text/html")

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, **exc.extra},
        )

    @app.exception_handler(SmsRateLimited)
    async def _rate_limited(_request: Request, exc: SmsRateLimited):
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "retry_after": exc.retry_after},
        )

    @app.exception_handler(SmsCodeExpired)
    async def _code_expired(_request: Request, exc: SmsCodeExpired):
        return JSONResponse(status_code=400, content={"error": "code_expired"})

    @app.exception_handler(SmsCodeAttemptsExceeded)
    async def _attempts_exceeded(_request: Request, exc: SmsCodeAttemptsExceeded):
        return JSONResponse(status_code=400, content={"error": "code_expired"})

    @app.exception_handler(SmsInvalidPhone)
    async def _invalid_phone(_request: Request, exc: SmsInvalidPhone):
        return JSONResponse(status_code=400, content={"error": "invalid_phone"})

    @app.exception_handler(SmsVerificationError)
    async def _verification_error(_request: Request, exc: SmsVerificationError):
        return JSONResponse(status_code=400, content={"error": "invalid_code"})

    @app.exception_handler(SmsSendError)
    async def _send_error(_request: Request, exc: SmsSendError):
        # 不把上游响应体返回给客户端；只留服务端日志。
        logger.warning("[sms] provider error: %s", exc)
        return JSONResponse(status_code=502, content={"error": "upstream_error"})

    @app.exception_handler(SmsConfigError)
    async def _config_error(_request: Request, exc: SmsConfigError):
        logger.error("[sms] config error: %s", exc)
        return JSONResponse(status_code=500, content={"error": "sms_not_configured"})

    @app.exception_handler(SmsError)
    async def _sms_error(_request: Request, exc: SmsError):
        logger.error("[sms] error: %s", exc)
        return JSONResponse(status_code=500, content={"error": "sms_error"})

    return app


app = create_app()

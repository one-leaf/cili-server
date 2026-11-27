"""cili-server FastAPI 装配入口。"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
from app.services.repos import ensure_shared_gitignores
from app.settings import Settings, load_settings
from app.sms.registry import SmsProviderResolver
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

ACCESS_LOGGER = "cili.access"
ACCESS_LOG_MAX_BYTES = 10 * 1024 * 1024
ACCESS_LOG_BACKUPS = 5


def _client_ip(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _write_access_log(request: Request, status: int, started: float) -> None:
    settings = getattr(request.app.state, "settings", None)
    if settings is None or not settings.access_log_path:
        return
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logging.getLogger(ACCESS_LOGGER).info(
        "%s %s %s %s %d %dms",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        _client_ip(request),
        request.method,
        request.url.path,
        status,
        elapsed_ms,
    )


def create_app(
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    provider: SmsProvider | None = None,
) -> FastAPI:
    """创建 app；测试可注入 settings / conn / provider。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or load_settings()
        db_conn = conn or connect(cfg.database_path)
        init_schema(db_conn)
        seed_initial_admin(db_conn, cfg.admin)
        try:
            updated = ensure_shared_gitignores(db_conn, cfg.git_root)
            if updated:
                logger.info("[git] 已更新共享仓库 .gitignore: %s", ", ".join(updated))
        except Exception:  # noqa: BLE001
            logger.warning("[git] 更新共享仓库 .gitignore 失败", exc_info=True)

        if provider is not None:
            sms_resolver = None
            sms_service = SmsVerificationService(
                db_conn, provider, cfg.verification
            )
        else:
            sms_resolver = SmsProviderResolver(db_conn, cfg)
            sms_service = SmsVerificationService(
                db_conn, config=cfg.verification, provider_for=sms_resolver.get
            )

        access_handler = None
        if cfg.access_log_path:
            log_path = Path(cfg.access_log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            access_handler = RotatingFileHandler(
                log_path,
                maxBytes=ACCESS_LOG_MAX_BYTES,
                backupCount=ACCESS_LOG_BACKUPS,
                encoding="utf-8",
            )
            access_handler.setFormatter(logging.Formatter("%(message)s"))
            access_logger = logging.getLogger(ACCESS_LOGGER)
            access_logger.setLevel(logging.INFO)
            access_logger.propagate = False
            access_logger.addHandler(access_handler)

        app.state.settings = cfg
        app.state.conn = db_conn
        app.state.sms_provider = provider or sms_resolver
        app.state.sms_resolver = sms_resolver
        app.state.sms_service = sms_service
        try:
            yield
        finally:
            if sms_resolver is not None:
                sms_resolver.close()
            if access_handler is not None:
                logging.getLogger(ACCESS_LOGGER).removeHandler(access_handler)
                access_handler.close()
            if conn is None:
                db_conn.close()

    app = FastAPI(title="cili-server", version="0.1.0", lifespan=lifespan)
    app.include_router(public_router)
    app.include_router(auth_router)
    app.include_router(config_router)
    app.include_router(admin_router)
    app.include_router(git_router)

    @app.middleware("http")
    async def _access_log_middleware(request: Request, call_next):
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            _write_access_log(request, 500, started)
            raise
        _write_access_log(request, response.status_code, started)
        return response

    admin_page = Path(__file__).resolve().parent / "templates" / "admin.html"

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_ui():
        return FileResponse(admin_page, media_type="text/html")

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        extra = dict(exc.extra)
        message = str(extra.pop("message", "") or exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "detail": message, **extra},
        )

    @app.exception_handler(SmsRateLimited)
    async def _rate_limited(_request: Request, exc: SmsRateLimited):
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limited",
                "detail": str(exc),
                "retry_after": exc.retry_after,
            },
        )

    @app.exception_handler(SmsCodeExpired)
    async def _code_expired(_request: Request, exc: SmsCodeExpired):
        return JSONResponse(
            status_code=400,
            content={"error": "code_expired", "detail": str(exc)},
        )

    @app.exception_handler(SmsCodeAttemptsExceeded)
    async def _attempts_exceeded(_request: Request, exc: SmsCodeAttemptsExceeded):
        return JSONResponse(
            status_code=400,
            content={"error": "code_expired", "detail": str(exc)},
        )

    @app.exception_handler(SmsInvalidPhone)
    async def _invalid_phone(_request: Request, exc: SmsInvalidPhone):
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_phone", "detail": str(exc)},
        )

    @app.exception_handler(SmsVerificationError)
    async def _verification_error(_request: Request, exc: SmsVerificationError):
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_code", "detail": str(exc)},
        )

    @app.exception_handler(SmsSendError)
    async def _send_error(_request: Request, exc: SmsSendError):
        # 不把上游响应体返回给客户端；只留服务端日志。
        logger.warning("[sms] provider error: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": "upstream_error", "detail": "upstream_error"},
        )

    @app.exception_handler(SmsConfigError)
    async def _config_error(_request: Request, exc: SmsConfigError):
        logger.error("[sms] config error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": "sms_not_configured", "detail": "sms_not_configured"},
        )

    @app.exception_handler(SmsError)
    async def _sms_error(_request: Request, exc: SmsError):
        logger.error("[sms] error: %s", exc)
        return JSONResponse(status_code=500, content={"error": "sms_error"})

    return app


app = create_app()

"""单进程装配业务、管理与认证模块；执行能力由 Runtime 提供。"""

from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import psycopg
from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool, ConnectionPool, PoolTimeout, TooManyRequests

from .auth import Authorization
from .contracts import fail, out
from .interfaces import admin, assets, business, identity
from .settings import Settings

# 本镜像认识的迁移集合：/v1/health 要求库里应用的版本**恰好**等于这个集合，
# 多一条（镜像旧了）少一条（没跑迁移）都直接 503。因此每加一条迁移都必须同步这里——
# 本切片新增 `0003_embedding_index` 时漏掉这一跳，就是被真实集成测试抓出来的。
# `SCHEMA` 仍是最新版本，供 `schema_version` 字段上报。
SCHEMA = "0003_embedding_index"
SCHEMA_VERSIONS = {"0001_initial", "0002_console", SCHEMA}
CAPABILITIES = [
    ("console_metadata", True, ""),
    ("keyword_search", True, ""),
    ("file_storage", True, ""),
    ("media_admission", False, "media_admission_not_attached"),
    ("task_execution", False, "runtime_task_service_not_attached"),
    ("plugin_installation", False, "runtime_plugin_installer_not_attached"),
    ("pipeline_publish", False, "runtime_pipeline_validation_not_attached"),
    ("semantic_search", False, "semantic_index_not_configured"),
]


def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    def configure(conn):
        conn.execute("SET statement_timeout='5s'")
        conn.execute("SET lock_timeout='2s'")
        conn.commit()

    pool = ConnectionPool(
        settings.database_url.get_secret_value(),
        open=False,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        timeout=3,
        max_waiting=32,
        kwargs={"connect_timeout": 3},
        configure=configure,
    )

    async def configure_upload(conn):
        await conn.execute("SET statement_timeout='5s'")
        await conn.execute("SET lock_timeout='2s'")
        await conn.commit()

    upload_pool = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        open=False,
        min_size=1,
        max_size=2,
        timeout=2,
        max_waiting=2,
        kwargs={"connect_timeout": 3},
        configure=configure_upload,
    )

    @asynccontextmanager
    async def lifespan(app):
        pool.open()
        await upload_pool.open()
        try:
            yield
        finally:
            await upload_pool.close()
            pool.close()

    app = FastAPI(title="SensoryPlex Platform API", version="0.1.0", lifespan=lifespan)
    app.state.pool, app.state.settings = pool, settings
    auth = Authorization(pool, settings)

    def error(request, status, reason):
        body = out(
            pb.ApiError(
                detail=reason,
                reason_code=reason,
                trace_id=getattr(request.state, "trace_id", ""),
                retryable=status in {429, 503},
            )
        )
        return JSONResponse(status_code=status, content=body)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        request.state.trace_id = uuid4().hex
        if request.method in {"POST", "PUT", "PATCH"} and not (
            request.method == "PUT"
            and request.url.path.startswith("/v1/uploads/")
            and request.url.path.endswith("/content")
        ):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    return error(request, 413, "request_body_too_large")
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(("/v1", "/admin", "/auth")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(request, 422, "invalid_request")

    @app.exception_handler(psycopg.errors.UniqueViolation)
    async def conflict(request, exc):
        return error(request, 409, "record_already_exists")

    @app.exception_handler(psycopg.Error)
    @app.exception_handler(TooManyRequests)
    @app.exception_handler(PoolTimeout)
    async def database_error(request, exc):
        return error(request, 503, "metadata_store_unavailable")

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        return error(request, 503, "storage_unavailable")

    def schema_version():
        with pool.connection() as conn:
            versions = {
                r[0] for r in conn.execute("SELECT version FROM schema_migration").fetchall()
            }
        if versions != SCHEMA_VERSIONS:
            fail(503, "schema_version_mismatch")
        return SCHEMA

    @app.get("/livez")
    def live():
        return {"status": "alive"}

    @app.get("/v1/health")
    def health():
        return {
            "status": "degraded",
            "metadata_store": "ready",
            "schema_version": schema_version(),
            "capabilities": {
                "keyword_search": True,
                "media_ingestion": False,
                "model_inference": False,
                "semantic_search": False,
            },
        }

    @app.get("/v1/capabilities")
    def capabilities(p: Annotated[object, Depends(auth.require(None))] = None):
        return out(
            pb.ConsoleStatus(
                schema_version=schema_version(),
                capabilities=[
                    pb.Capability(name=n, available=a, reason=r) for n, a, r in CAPABILITIES
                ],
            )
        )

    identity.register(app, pool, auth, settings)
    business.register(app, pool, auth)
    assets.register(app, pool, auth, settings, upload_pool)
    admin.register(app, pool, auth, settings)
    dist = settings.console_dist.resolve()
    if (dist / "static").is_dir():
        app.mount("/static", StaticFiles(directory=dist / "static"), name="console-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def console(path: str):
        if path.startswith(("v1/", "admin/", "auth/")) or path in {"v1", "admin", "auth"}:
            fail(404, "endpoint_not_found")
        if not (dist / "index.html").is_file():
            fail(503, "console_not_built")
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app

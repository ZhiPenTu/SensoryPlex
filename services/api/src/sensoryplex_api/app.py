"""单进程装配业务、管理与认证模块；执行能力由 Runtime 提供。"""

import re
import time
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import psycopg
from edge_material_sdk import (
    TraceContext,
    get_logger,
    get_registry,
    get_trace_recorder,
    get_tracer,
    set_trace_context,
    update_process_memory_metrics,
)
from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool, ConnectionPool, PoolTimeout, TooManyRequests

from .auth import Authorization
from .contracts import RETRYABLE_HEADER, fail, out
from .infrastructure.storage import create_storage_driver
from .interfaces import (
    admin,
    assets,
    business,
    identity,
    nodes,
    orchestration,
    plugin_deploy,
    plugin_registry,
    scenario_packages,
)
from .settings import Settings

# 本镜像认识的迁移集合：/v1/health 要求库里应用的版本**恰好**等于这个集合，
# 多一条（镜像旧了）少一条（没跑迁移）都直接 503。因此每加一条迁移都必须同步这里——
# 本切片新增 `0003_embedding_index` 时漏掉这一跳，就是被真实集成测试抓出来的。
# `SCHEMA` 仍是最新版本，供 `schema_version` 字段上报。
SCHEMA = "0025_w3c_trace_context"
SCHEMA_VERSIONS = {
    "0025_w3c_trace_context",
    "0024_material_revision_lineage",
    "0023_outbox_partitioning_and_archival",
    "0020_enrichment_input_identity",
    "0021_plugin_revision_retirement",
    "0022_scenario_product_packages",
    "0019_enrichment_leases",
    "0017_plugin_task_output",
    "0018_plugin_runtime_pins",
    "0001_initial",
    "0002_console",
    "0003_embedding_index",
    "0004_node_topology",
    "0005_job_dispatch",
    "0006_task_dispatch",
    "0007_pipeline_publish",
    "0008_orchestration_run_task",
    "0009_task_dispatch_failure_reconciliation",
    "0010_plugin_hot_deploy",
    "0011_multimodal_execution_bridge",
    # 全帧判别让"没送模型"分成"计划本来就不刷新"与"计划刷新过却没有观测"两种诚实语义，
    # 因此采样状态的取值集合必须一起前进。
    "0012_timeline_coverage",
    "0013_timeline_coverage_states",
    "0014_vlm_delayed_enrichment",
    "0015_vlm_failure_result_receipt",
    "0016_generic_plugin_platform",
    SCHEMA,
}
# 语义检索不可用时的原因码：检索面未配置就是这个码，不是 501、也不是"没有命中"。
SEMANTIC_UNAVAILABLE_REASON = "semantic_search_unavailable"
LOGGER = get_logger("sensoryplex.api")

CAPABILITIES = [
    ("console_metadata", True, ""),
    ("node_topology", True, ""),
    ("keyword_search", True, ""),
    ("file_storage", True, ""),
    ("media_admission", False, "media_admission_not_attached"),
    # v2 的 Node Agent 已接入受控 Runtime 执行器；每一条完成事实仍须由 assignment 回执
    # 和（对 Timeline 而言）完整覆盖层共同证明，不能仅凭 intent success 伪造完成。
    ("task_execution", True, ""),
    ("plugin_installation", False, "runtime_plugin_installer_not_attached"),
    ("pipeline_publish", True, ""),
    ("semantic_search", False, SEMANTIC_UNAVAILABLE_REASON),
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
        LOGGER.info("Starting SensoryPlex Platform API", schema_target=SCHEMA)
        pool.open()
        await upload_pool.open()
        try:
            yield
        finally:
            LOGGER.info("Shutting down SensoryPlex Platform API")
            await upload_pool.close()
            pool.close()

    app = FastAPI(title="SensoryPlex Platform API", version="0.1.0", lifespan=lifespan)
    app.state.pool, app.state.settings = pool, settings
    storage = create_storage_driver(settings)
    app.state.storage = storage
    auth = Authorization(pool, settings)

    def semantic_capability() -> tuple[bool, str]:
        """能力表只报**配置事实**：配了检索面就报可用，链路是否真的通由检索本身回答。"""
        if settings.index_search_endpoint:
            return True, ""
        return False, SEMANTIC_UNAVAILABLE_REASON

    def error(request, status, reason, retryable=None):
        body = out(
            pb.ApiError(
                detail=reason,
                reason_code=reason,
                trace_id=getattr(request.state, "trace_id", ""),
                retryable=status in {429, 503} if retryable is None else retryable,
                traceparent=getattr(request.state, "traceparent", ""),
            )
        )
        return JSONResponse(status_code=status, content=body)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        # W3C TraceContext 与分布式链路追踪透传接入
        raw_traceparent = request.headers.get("traceparent")
        raw_tracestate = request.headers.get("tracestate", "")
        raw_trace_id = request.headers.get("x-trace-id")

        trace_ctx = TraceContext.from_traceparent(
            raw_traceparent, raw_tracestate, generate_child_span=True
        )
        if trace_ctx is None:
            if raw_trace_id and re.fullmatch(r"[0-9a-f]{32}", raw_trace_id.lower()):
                trace_ctx = TraceContext(
                    trace_id=raw_trace_id.lower(),
                    span_id=uuid4().hex[:16],
                    parent_span_id=None,
                    trace_flags="01",
                    tracestate=raw_tracestate,
                )
            else:
                trace_ctx = TraceContext.new_root(sampled=True, tracestate=raw_tracestate)

        request.state.trace_context = trace_ctx
        request.state.traceparent = trace_ctx.traceparent
        request.state.trace_id = trace_ctx.trace_id
        set_trace_context(trace_ctx)

        # 启动 API 接入层服务端 Span
        tracer = get_tracer("sensoryplex-api")
        span = tracer.start_span(
            name=f"http.{request.method.lower()} {request.url.path}",
            parent=trace_ctx,
            attributes={
                "http.method": request.method,
                "http.target": request.url.path,
                "http.client_ip": request.client.host if request.client else "-",
            },
        )
        request.state.span = span
        start_time = time.perf_counter()

        is_upload = (
            request.method == "PUT"
            and request.url.path.startswith("/v1/uploads/")
            and request.url.path.endswith("/content")
        )
        is_release_import = (
            request.method == "POST" and request.url.path == "/admin/v1/plugin-releases:import"
        )
        if request.method in {"POST", "PUT", "PATCH"} and not is_upload and not is_release_import:
            max_bytes = (
                4_194_304
                if (
                    request.url.path.startswith("/v1/agent/tasks/")
                    and request.url.path.endswith((":timeline", ":output"))
                )
                or (
                    request.url.path.startswith("/v1/agent/enrichments/")
                    and request.url.path.endswith(":result")
                )
                else 65536
            )
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > max_bytes:
                    LOGGER.warning(
                        "Request body too large", path=request.url.path, max_bytes=max_bytes
                    )
                    return error(request, 413, "request_body_too_large")
            request._body = bytes(body)

        response = await call_next(request)
        duration_s = time.perf_counter() - start_time
        duration_ms = round(duration_s * 1000, 2)

        # 结束接入层 Span 并记录耗时与状态
        span.set_attribute("http.status_code", response.status_code)
        span.set_status("OK" if response.status_code < 500 else "ERROR")
        span.end()

        # 采集标准 Prometheus HTTP 请求耗时指标
        reg = get_registry()
        http_metric = reg.get_metric("sensoryplex_http_request_duration_seconds")
        if http_metric:
            http_metric.observe(
                duration_s,
                labels={
                    "service": "api",
                    "method": request.method,
                    "path": request.url.path,
                    "status": str(response.status_code),
                },
            )

        response.headers["traceparent"] = trace_ctx.traceparent
        response.headers["X-Trace-Id"] = trace_ctx.trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(("/v1", "/admin", "/auth")):
            response.headers["Cache-Control"] = "no-store"

        client_ip = request.client.host if request.client else "-"
        if request.url.path == "/livez":
            LOGGER.debug(
                "HTTP probe completed",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        elif response.status_code >= 500:
            LOGGER.error(
                "HTTP request server error",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                client_ip=client_ip,
            )
        elif response.status_code >= 400:
            LOGGER.warning(
                "HTTP request client error",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                client_ip=client_ip,
            )
        else:
            LOGGER.info(
                "HTTP request completed",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                client_ip=client_ip,
            )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        override = {"true": True, "false": False}.get((exc.headers or {}).get(RETRYABLE_HEADER, ""))
        if exc.status_code >= 500:
            LOGGER.error(
                "HTTP error: %s", exc.detail, status=exc.status_code, reason=str(exc.detail)
            )
        else:
            LOGGER.warning(
                "HTTP client error: %s", exc.detail, status=exc.status_code, reason=str(exc.detail)
            )
        return error(request, exc.status_code, str(exc.detail), retryable=override)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        LOGGER.warning("Validation error on %s: %s", request.url.path, exc, status=422)
        return error(request, 422, "invalid_request")

    @app.exception_handler(psycopg.errors.UniqueViolation)
    async def conflict(request, exc):
        LOGGER.warning("Database unique conflict on %s: %s", request.url.path, exc, status=409)
        return error(request, 409, "record_already_exists")

    @app.exception_handler(psycopg.Error)
    @app.exception_handler(TooManyRequests)
    @app.exception_handler(PoolTimeout)
    async def database_error(request, exc):
        LOGGER.error(
            "Metadata store error on %s: %s", request.url.path, exc, status=503, exc_info=True
        )
        return error(request, 503, "metadata_store_unavailable")

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        LOGGER.error("Storage error on %s: %s", request.url.path, exc, status=503, exc_info=True)
        return error(request, 503, "storage_unavailable")

    def schema_version():
        with pool.connection() as conn:
            versions = {
                r[0] for r in conn.execute("SELECT version FROM schema_migration").fetchall()
            }
        if versions != SCHEMA_VERSIONS:
            fail(503, "schema_version_mismatch")
        return SCHEMA

    @app.get("/metrics", response_class=PlainTextResponse)
    def prometheus_metrics():
        """统一暴露 Prometheus 标准指标端点 (Metrics Endpoint)。"""
        update_process_memory_metrics("api")
        return PlainTextResponse(
            get_registry().format_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/admin/v1/traces/{trace_id}")
    @app.get("/v1/traces/{trace_id}")
    def get_trace_waterfall(
        trace_id: str,
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        """端到端链路追踪耗时瀑布图与瓶颈定位分析。"""
        waterfall = get_trace_recorder().get_waterfall(trace_id)
        if waterfall is None:
            fail(404, "trace_not_found")
        return waterfall

    @app.get("/admin/v1/traces/{trace_id}/otel")
    def get_trace_otel_export(
        trace_id: str,
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        """导出 OpenTelemetry ResourceSpans 标准规范 JSON。"""
        otel = get_trace_recorder().export_otel_json(trace_id)
        if otel is None:
            fail(404, "trace_not_found")
        return otel

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
                "semantic_search": semantic_capability()[0],
            },
        }

    @app.get("/v1/capabilities")
    def capabilities(p: Annotated[object, Depends(auth.require(None))] = None):
        return out(
            pb.ConsoleStatus(
                schema_version=schema_version(),
                capabilities=[
                    pb.Capability(name=name, available=available, reason=reason)
                    for name, available, reason in (
                        (n, *semantic_capability()) if n == "semantic_search" else (n, a, r)
                        for n, a, r in CAPABILITIES
                    )
                ],
            )
        )

    identity.register(app, pool, auth, settings)
    business.register(app, pool, auth, settings)
    assets.register(app, pool, auth, settings, upload_pool, storage=storage)
    admin.register(app, pool, auth, settings)
    nodes.register(app, pool, auth, settings, storage=storage)
    plugin_deploy.register(app, pool, auth, settings)
    plugin_registry.register(app, pool, auth, settings)
    orchestration.register(app, pool, auth, settings)
    scenario_packages.register(app, pool, auth, settings)
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

import math
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from edge_material_sdk.generated.gateway.v1.gateway_pb2 import SearchRequest, SearchResponse
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from psycopg_pool import ConnectionPool, PoolTimeout

from . import repository
from .settings import Settings


def proto_json(message):
    return MessageToDict(
        message, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )


def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    def configure_connection(conn):
        conn.execute("SET statement_timeout = '5s'")
        conn.commit()

    pool = ConnectionPool(
        settings.database_url.get_secret_value(),
        open=False,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        timeout=3,
        kwargs={"connect_timeout": 3},
        configure=configure_connection,
    )

    @asynccontextmanager
    async def lifespan(app):
        pool.open()
        yield
        pool.close()

    app = FastAPI(
        title="SensoryPlex Gateway",
        version="0.1.0",
        lifespan=lifespan,
        description="工程底座：鉴权、素材版本与关键词检索；媒体/模型/语义索引待接入。",
    )
    app.state.pool = pool
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or not secrets.compare_digest(
            credentials.credentials, settings.api_token.get_secret_value()
        ):
            raise HTTPException(
                401, "authentication_required", headers={"WWW-Authenticate": "Bearer"}
            )
        return settings.principal

    @app.exception_handler(psycopg.Error)
    @app.exception_handler(PoolTimeout)
    async def database_error(request, error):
        return JSONResponse(status_code=503, content={"detail": "metadata_store_unavailable"})

    @app.get("/livez")
    def liveness():
        return {"status": "alive"}

    @app.get("/v1/health")
    def health():
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT version FROM schema_migration ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if not row or row[0] != "0001_initial":
            raise HTTPException(503, "schema_version_mismatch")
        return {
            "status": "degraded",
            "metadata_store": "ready",
            "schema_version": row[0],
            "capabilities": {
                "keyword_search": True,
                "media_ingestion": False,
                "model_inference": False,
                "semantic_search": False,
            },
        }

    @app.post("/v1/materials:search")
    def search(
        principal: Annotated[str, Depends(authorize)],
        body: Annotated[dict, Body(description="Proto gateway.v1.SearchRequest JSON")],
    ):
        try:
            req = ParseDict(body, SearchRequest(), ignore_unknown_fields=False)
        except (ParseError, TypeError, ValueError):
            raise HTTPException(422, "invalid_search_contract") from None
        if req.mode not in ("", "keyword", "semantic"):
            raise HTTPException(422, "invalid_search_mode")
        if req.mode == "semantic":
            raise HTTPException(501, "semantic_index_not_configured")
        if (
            req.limit > 100
            or len(req.query) > 2000
            or len(req.modalities) > 32
            or len(req.tags) > 32
        ):
            raise HTTPException(422, "search_limits_exceeded")
        if any(req.HasField(f) and getattr(req, f) < 0 for f in ("start_ms", "end_ms")):
            raise HTTPException(422, "invalid_time_range")
        if req.HasField("end_ms") and req.end_ms <= req.start_ms:
            raise HTTPException(422, "invalid_time_range")
        if req.HasField("min_confidence") and (
            not math.isfinite(req.min_confidence) or not 0 <= req.min_confidence <= 1
        ):
            raise HTTPException(422, "invalid_confidence")
        with pool.connection() as conn:
            materials = repository.search_materials(conn, principal, req)
        return proto_json(
            SearchResponse(materials=materials, mode="keyword", index_version="postgres-literal-v1")
        )

    @app.get("/v1/materials/{material_id}")
    def get_material(
        material_id: str,
        principal: Annotated[str, Depends(authorize)],
        revision: Annotated[int | None, Query(ge=1, le=2147483647)] = None,
    ):
        with pool.connection() as conn:
            material = repository.get_material(conn, principal, material_id, revision)
        if material is None:
            raise HTTPException(404, "material_not_found")
        return proto_json(material)

    @app.get("/v1/streams/{stream_id}")
    def get_stream(stream_id: str, principal: Annotated[str, Depends(authorize)]):
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT s.stream_id,s.source_id,s.status,s.clock_offset_ms "
                "FROM stream_session s JOIN media_source m ON m.source_id=s.source_id "
                "WHERE s.stream_id=%s AND m.owner=%s",
                (stream_id, principal),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "stream_not_found")
        return dict(zip(("stream_id", "source_id", "status", "clock_offset_ms"), row, strict=True))

    @app.post("/v1/streams")
    def start_stream(principal: Annotated[str, Depends(authorize)]):
        raise HTTPException(501, "media_worker_not_attached")

    @app.post("/v1/streams/{stream_id}:stop")
    def stop_stream(stream_id: str, principal: Annotated[str, Depends(authorize)]):
        raise HTTPException(501, "media_worker_not_attached")

    return app

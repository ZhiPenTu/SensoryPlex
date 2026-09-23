"""业务准备与真实素材查询；执行链路缺失时保持显式拒绝。"""

import math
from typing import Annotated

from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from edge_material_sdk.generated.gateway.v1.gateway_pb2 import SearchRequest, SearchResponse
from fastapi import Body, Depends, Query

from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field
from ..infrastructure import materials


def register(app, pool, auth):
    @app.post("/v1/materials:search")
    def search(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        req = parse(body, SearchRequest)
        if req.mode not in ("", "keyword", "semantic"):
            fail(422, "invalid_search_mode")
        if req.mode == "semantic":
            fail(501, "semantic_index_not_configured")
        if (
            req.limit > 100
            or len(req.query) > 2000
            or len(req.modalities) > 32
            or len(req.tags) > 32
        ):
            fail(422, "search_limits_exceeded")
        if any(req.HasField(f) and getattr(req, f) < 0 for f in ("start_ms", "end_ms")):
            fail(422, "invalid_time_range")
        if req.HasField("end_ms") and req.end_ms <= req.start_ms:
            fail(422, "invalid_time_range")
        if req.HasField("min_confidence") and (
            not math.isfinite(req.min_confidence) or not 0 <= req.min_confidence <= 1
        ):
            fail(422, "invalid_confidence")
        with pool.connection() as conn:
            result = materials.search_materials(conn, p.name, req)
        return out(
            SearchResponse(materials=result, mode="keyword", index_version="postgres-literal-v1")
        )

    @app.get("/v1/materials/{key}")
    def detail(
        key: str,
        revision: int | None = Query(None, ge=1, le=2147483647),
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        with pool.connection() as conn:
            result = materials.get_material(conn, p.name, key, revision)
        if result is None:
            fail(404, "material_not_found")
        return out(result)

    @app.get("/v1/streams/{key}")
    def stream(key: str, p: Annotated[object, Depends(auth.require("materials:read"))] = None):
        with pool.connection() as conn:
            result = one(
                conn,
                (
                    "SELECT s.stream_id,s.source_id,s.status,s.clock_offset_ms FROM "
                    "stream_session s JOIN media_source m ON m.source_id=s.source_id "
                    "WHERE s.stream_id=%s AND m.owner=%s"
                ),
                (key, p.name),
            )
        if not result:
            fail(404, "stream_not_found")
        return result

    @app.post("/v1/streams")
    @app.post("/v1/streams/{key}:stop")
    def unavailable_stream(
        key: str = "", p: Annotated[object, Depends(auth.require("jobs:write"))] = None
    ):
        fail(501, "media_worker_not_attached")

    @app.post("/v1/jobs")
    def dispatch(p: Annotated[object, Depends(auth.require("jobs:write"))] = None):
        fail(501, "runtime_task_service_not_attached")

    @app.get("/v1/job-drafts")
    def drafts(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
        p: Annotated[object, Depends(auth.require("jobs:read"))] = None,
    ):
        with pool.connection() as conn:
            items = rows(
                conn,
                (
                    "SELECT * FROM console_job_draft WHERE owner=%s ORDER BY "
                    "created_at DESC,id LIMIT %s OFFSET %s"
                ),
                (p.name, limit, offset),
            )
            total = conn.execute(
                "SELECT count(*) FROM console_job_draft WHERE owner=%s", (p.name,)
            ).fetchone()[0]
        for item in items:
            item["reason"] = "runtime_task_service_not_attached"
        return out({"items": items, "total": total}, pb.JobDraftList)

    @app.post("/v1/job-drafts", status_code=201)
    def create_draft(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        req = parse(body, pb.SaveJobDraft)
        text_field(req.name)
        with pool.connection() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("drafts:" + p.name,)
            )
            if (
                conn.execute(
                    "SELECT count(*) FROM console_job_draft WHERE owner=%s AND state='draft'",
                    (p.name,),
                ).fetchone()[0]
                >= 100
            ):
                fail(429, "draft_limit_reached")
            if not one(
                conn,
                (
                    "SELECT id FROM console_upload WHERE id=%s AND owner=%s AND "
                    "state='awaiting_admission'"
                ),
                (req.asset_id, p.name),
            ):
                fail(404, "asset_not_found")
            if not one(
                conn,
                "SELECT id FROM console_pipeline WHERE id=%s AND state='draft' FOR SHARE",
                (req.pipeline_id,),
            ):
                fail(422, "pipeline_not_available")
            key = identifier("draft")
            result = one(
                conn,
                (
                    "INSERT INTO "
                    "console_job_draft(id,owner,asset_id,pipeline_id,name) VALUES "
                    "(%s,%s,%s,%s,%s) RETURNING *"
                ),
                (key, p.name, req.asset_id, req.pipeline_id, req.name),
            )
            audit(conn, p.name, "job.draft.create", key)
        return out({**result, "reason": "runtime_task_service_not_attached"}, pb.JobDraft)

    @app.post("/v1/job-drafts/{key}:archive")
    def archive(key: str, p: Annotated[object, Depends(auth.require("jobs:write"))] = None):
        with pool.connection() as conn:
            result = conn.execute(
                (
                    "UPDATE console_job_draft SET state='archived' WHERE id=%s AND "
                    "owner=%s RETURNING id"
                ),
                (key, p.name),
            ).fetchone()
            if not result:
                fail(404, "draft_not_found")
            audit(conn, p.name, "job.draft.archive", key)
        return out(pb.EmptyResponse())

"""业务准备与真实素材查询；执行链路缺失时保持显式拒绝。

检索有两种模式，各自的语义写清楚，不互相冒充（ADR-023）：

- `keyword`：PostgreSQL 字面子串匹配，**不做相关性排名**，`index_version` 为
  `postgres-literal-v1`，`hits` 为空（"没有排名"与"排名为 0"不同）；
- `semantic`：转发给持有向量索引的检索面，由它编码查询、近邻、回查事实；`hits` 与
  `materials` 同序同长，`index_version` 由检索面给出。**本切片只支持 `query` + `limit`**，
  其余筛选字段显式拒绝（`semantic_filters_not_supported`）——静默忽略筛选条件会给出
  "像是筛过"的结果，那比拒绝更糟。
"""

import math
from typing import Annotated

from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from edge_material_sdk.generated.gateway.v1.gateway_pb2 import (
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from fastapi import Body, Depends, Query
from psycopg.types.json import Jsonb

from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field
from ..infrastructure import materials, semantic

KEYWORD_INDEX_VERSION = "postgres-literal-v1"
DEFAULT_LIMIT = 20


def _has_semantic_filters(req) -> bool:
    return bool(
        req.stream_id
        or req.modalities
        or req.tags
        or req.HasField("start_ms")
        or req.HasField("end_ms")
        or req.HasField("min_confidence")
    )


def _semantic(conn, settings, req, principal: str):
    """语义检索：只转发查询与条数，其余条件显式拒绝。"""
    if _has_semantic_filters(req):
        fail(422, "semantic_filters_not_supported")
    if not req.query:
        # 空查询在这里就被拒绝：它是调用方的输入错误，不该变成一次"上游失败"。
        fail(422, "invalid_query")
    try:
        outcome = semantic.search(
            conn, settings, principal=principal, query=req.query, limit=req.limit or DEFAULT_LIMIT
        )
    except semantic.SemanticSearchError as error:
        # 状态码与 `retryable` 都由 adapter 按"失败发生在哪一环"给出，这里不重新推断；
        # 原因码原样上抛，不再改写（ADR-020 §8 记录过最后一跳改写原因码的缺陷）。
        fail(error.status, error.code, retryable=error.retryable)
    return out(
        SearchResponse(
            materials=outcome.materials,
            mode="semantic",
            index_version=outcome.index_version,
            hits=[SearchHit(**hit) for hit in outcome.hits],
            unindexed_hits=outcome.unindexed_hits,
            vector_index_key=outcome.vector_index_key,
            unresolved_hits=outcome.unresolved_hits,
        )
    )


def register(app, pool, auth, settings):
    @app.post("/v1/materials:search")
    def search(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        req = parse(body, SearchRequest)
        if req.mode not in ("", "keyword", "semantic"):
            fail(422, "invalid_search_mode")
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
            if req.mode == "semantic":
                return _semantic(conn, settings, req, p.name)
            result = materials.search_materials(conn, p.name, req)
        return out(
            SearchResponse(materials=result, mode="keyword", index_version=KEYWORD_INDEX_VERSION)
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

    def _dispatch_job_internal(conn, draft_id: str, owner: str, target_node_id: str | None = None):
        draft = one(
            conn,
            "SELECT * FROM console_job_draft WHERE id=%s AND owner=%s FOR UPDATE",
            (draft_id, owner),
        )
        if not draft:
            fail(404, "draft_not_found")
        if draft["state"] not in {"draft", "failed"}:
            fail(409, "job_not_in_dispatchable_state")

        upload = one(
            conn,
            "SELECT * FROM console_upload WHERE id=%s AND owner=%s",
            (draft["asset_id"], owner),
        )
        if not upload:
            fail(404, "asset_not_found")
        if upload["state"] != "awaiting_admission":
            fail(409, "upload_incomplete")

        blob_path = settings.blob_root.resolve() / upload["sha256"][7:]
        if not blob_path.is_file():
            fail(503, "blob_unavailable")

        # 选定执行节点
        if not target_node_id:
            target_node = one(
                conn,
                (
                    "SELECT * FROM console_node WHERE is_co_located=true AND status='ready' "
                    "ORDER BY enrolled_at ASC LIMIT 1"
                ),
            )
            if not target_node:
                target_node = one(
                    conn,
                    (
                        "SELECT * FROM console_node WHERE status='ready' "
                        "ORDER BY is_co_located DESC, enrolled_at ASC LIMIT 1"
                    ),
                )
            if not target_node:
                fail(503, "no_ready_worker_node_available")
            target_node_id = target_node["node_id"]
        else:
            target_node = one(
                conn,
                "SELECT * FROM console_node WHERE node_id=%s AND status='ready'",
                (target_node_id,),
            )
            if not target_node:
                fail(409, "target_node_not_ready")

        if not target_node["is_co_located"]:
            fail(422, "data_locality_violation")

        task_intent_id = identifier("task")
        task_config = {
            "task_type": "process_video",
            "job_id": draft["id"],
            "asset_id": upload["id"],
            "sha256": upload["sha256"],
            "filename": upload["filename"],
            "owner": owner,
            "blob_path": str(blob_path),
            "pipeline_id": draft["pipeline_id"],
        }

        conn.execute(
            """
            INSERT INTO console_deployment_intent(
                id, node_id, instance_id, action, artifact_digest, rollback_digest,
                config, state, created_by, job_id
            ) VALUES (%s, %s, NULL, 'task_process', %s, NULL, %s, 'pending', %s, %s)
            """,
            (
                task_intent_id,
                target_node_id,
                upload["sha256"],
                Jsonb(task_config),
                owner,
                draft["id"],
            ),
        )

        updated_draft = one(
            conn,
            """
            UPDATE console_job_draft
            SET state='processing', target_node_id=%s, error_code=NULL, error_detail=NULL,
                dispatched_at=now()
            WHERE id=%s
            RETURNING *
            """,
            (target_node_id, draft["id"]),
        )
        audit(conn, owner, "job.dispatch", draft["id"])
        return updated_draft

    @app.post("/v1/job-drafts/{key}:dispatch")
    def dispatch_draft(
        key: str,
        body: Annotated[dict | None, Body()] = None,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        node_id = body.get("node_id") if isinstance(body, dict) else None
        with pool.connection() as conn:
            result = _dispatch_job_internal(conn, key, p.name, target_node_id=node_id)
        return out({**result, "reason": ""}, pb.JobDraft)

    @app.post("/v1/jobs")
    def dispatch_new(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        draft_id = body.get("draft_id")
        node_id = body.get("node_id")
        if draft_id:
            with pool.connection() as conn:
                result = _dispatch_job_internal(conn, draft_id, p.name, target_node_id=node_id)
            return out({**result, "reason": ""}, pb.JobDraft)

        req = parse(body, pb.SaveJobDraft)
        text_field(req.name)
        with pool.connection() as conn:
            draft_key = identifier("draft")
            conn.execute(
                (
                    "INSERT INTO "
                    "console_job_draft(id,owner,asset_id,pipeline_id,name) VALUES "
                    "(%s,%s,%s,%s,%s)"
                ),
                (draft_key, p.name, req.asset_id, req.pipeline_id, req.name),
            )
            result = _dispatch_job_internal(conn, draft_key, p.name, target_node_id=node_id)
        return out({**result, "reason": ""}, pb.JobDraft)

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
            item["reason"] = item.get("error_code") or (
                "" if item.get("state") in {"processing", "completed"} else ""
            )
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
                (
                    "SELECT id FROM console_pipeline "
                    "WHERE id=%s AND state IN ('draft', 'published') FOR SHARE"
                ),
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

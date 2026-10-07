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
from edge_material_sdk.generated.material.v1 import material_pb2
from edge_material_sdk.generated.media.v1 import media_pb2
from fastapi import Body, Depends, Query
from psycopg.types.json import Jsonb

from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field
from ..infrastructure import (
    materials,
    multimodal,
    orchestration,
    second_windows,
    semantic,
    vlm_delayed,
)

KEYWORD_INDEX_VERSION = "postgres-literal-v1"
DEFAULT_LIMIT = 20


def _has_semantic_filters(req) -> bool:
    return bool(
        req.stream_id
        or req.execution_id
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
        execution_id: str = Query("", max_length=128),
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        with pool.connection() as conn:
            result = materials.get_material(conn, p.name, key, revision, execution_id)
        if result is None:
            fail(404, "material_not_found")
        return out(result)

    @app.get("/v1/materials/{key}/lineage")
    def lineage(
        key: str,
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        with pool.connection() as conn:
            history = materials.get_material_lineage(conn, p.name, key)
        if not history:
            fail(404, "material_not_found")
        return {"material_unit_id": key, "lineage": history}

    @app.get("/v1/materials/{key}/index-status")
    def indexing(
        key: str,
        revision: int | None = Query(None, ge=1),
        execution_id: str = Query("", max_length=128),
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        from edge_material_sdk.manifest import declared_text
        from google.protobuf.json_format import MessageToDict

        with pool.connection() as conn:
            unit = materials.get_material(conn, p.name, key, revision, execution_id)
            if unit is None:
                fail(404, "material_not_found")
            items = []
            for observation in unit.observations:
                fields, reason, state = [], "", "not_declared"
                if observation.provenance.processor_release_id:
                    registration = one(
                        conn,
                        "SELECT manifest FROM plugin_registration WHERE release_id=%s",
                        (observation.provenance.processor_release_id,),
                    )
                    declaration = (
                        next(
                            (
                                d
                                for d in registration["manifest"]["spec"]["outputs"]
                                if d["modality"] == observation.modality
                            ),
                            None,
                        )
                        if registration
                        else None
                    )
                    fields = declaration.get("textFields", []) if declaration else []
                    if fields:
                        state = (
                            "pending"
                            if declared_text(MessageToDict(observation.payload), fields)
                            else "empty_text"
                        )
                    else:
                        reason = "index_text_not_declared"
                elif observation.modality in {"ocr_blocks", "vision.scene_description"}:
                    state = "pending"
                records = rows(
                    conn,
                    "SELECT state FROM embedding_record WHERE observation_id=%s AND "
                    "material_unit_id=%s AND material_revision=%s",
                    (observation.observation_id, unit.material_unit_id, unit.revision),
                )
                if records:
                    state = (
                        "ready"
                        if all(r["state"] == "ready" for r in records)
                        else records[0]["state"]
                    )
                items.append(
                    {
                        "observation_id": observation.observation_id,
                        "state": state,
                        "reason_code": reason,
                        "text_fields": fields,
                    }
                )
        return out({"items": items}, pb.MaterialIndexStatus)

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

    def _assert_v2_plugin_instances(conn, *, node_id: str, revision: dict) -> None:
        """派发前核验每个外部处理器都有同节点、同制品、同配置的 active 实例。"""
        for node in revision["definition_json"].get("nodes", []):
            if node["plugin_id"] == multimodal.RUNTIME_TIMELINE_PLUGIN:
                continue
            if node.get("release_id"):
                from ..infrastructure.runtime_bindings import pinned_runtime

                if not pinned_runtime(conn, node_id, node):
                    fail(409, "plugin_pinned_release_unavailable")
                continue
            instance = one(
                conn,
                """
                SELECT runtime.runtime_instance_id
                FROM console_plugin_instance slot
                JOIN plugin_runtime_instance runtime
                  ON runtime.runtime_instance_id=slot.active_runtime_instance_id
                WHERE slot.node_id=%s
                  AND slot.plugin_id=%s
                  AND slot.artifact_digest=%s
                  AND slot.config_hash=%s
                  AND slot.actual_state='ready'
                  AND runtime.node_id=%s
                  AND runtime.plugin_id=%s
                  AND runtime.artifact_digest=%s
                  AND runtime.role='active'
                  AND runtime.state='active'
                  AND runtime.endpoint <> ''
                """,
                (
                    node_id,
                    node["plugin_id"],
                    node["artifact_digest"],
                    node["config_hash"],
                    node_id,
                    node["plugin_id"],
                    node["artifact_digest"],
                ),
            )
            if not instance:
                fail(409, "plugin_instance_unavailable")

    def _dispatch_orchestrated_v2(
        conn, *, draft: dict, upload: dict, pipeline: dict, owner: str, target_node_id: str
    ) -> dict:
        """在同一事务内写入 Console 快照、Run、Task、Assignment 与受控任务意图。"""
        if pipeline["state"] != "published":
            fail(409, "orchestrated_pipeline_not_published")
        revision = one(
            conn,
            """
            SELECT * FROM pipeline_revision
            WHERE pipeline_id=%s AND revision=%s
            """,
            (pipeline["orchestration_pipeline_id"], pipeline["orchestration_revision"]),
        )
        if not revision or revision["graph_digest"] != pipeline["graph_digest"]:
            fail(409, "pipeline_revision_binding_invalid")
        _assert_v2_plugin_instances(conn, node_id=target_node_id, revision=revision)

        input_ref = f"console_upload:{upload['id']}:{upload['sha256']}"
        idempotency_key = f"console_job:{draft['id']}:{pipeline['graph_digest']}"
        run, _, duplicate = orchestration.submit_pipeline_run(
            conn,
            owner=owner,
            pipeline_id=pipeline["orchestration_pipeline_id"],
            revision=int(pipeline["orchestration_revision"]),
            input_ref=input_ref,
            idempotency_key=idempotency_key,
        )
        execution = one(
            conn,
            "SELECT * FROM console_job_execution WHERE run_id=%s",
            (run["run_id"],),
        )
        if duplicate and execution:
            return {**draft, **execution, "pipeline_revision": execution["pipeline_revision"]}

        execution_id = identifier("execution")
        execution = one(
            conn,
            """
            INSERT INTO console_job_execution(
                execution_id,job_id,run_id,pipeline_id,pipeline_revision,graph_digest,
                target_node_id,input_ref,state
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running')
            RETURNING *
            """,
            (
                execution_id,
                draft["id"],
                run["run_id"],
                pipeline["orchestration_pipeline_id"],
                pipeline["orchestration_revision"],
                pipeline["graph_digest"],
                target_node_id,
                input_ref,
            ),
        )
        assignments = orchestration.schedule_ready_tasks(
            conn,
            candidate_node_id=target_node_id,
            is_co_located=True,
            max_tasks=16,
        )
        node_specs = {item["id"]: item for item in revision["definition_json"].get("nodes", [])}
        for assignment in assignments:
            if assignment["run_id"] != run["run_id"]:
                continue
            spec = node_specs[assignment["node_id"]]
            conn.execute(
                """
                INSERT INTO console_deployment_intent(
                    id,node_id,instance_id,action,artifact_digest,rollback_digest,config,
                    state,created_by,job_id,deadline_unix_ms
                ) VALUES (%s,%s,NULL,'task_process',%s,NULL,%s,'pending',%s,%s,%s)
                """,
                (
                    identifier("task"),
                    target_node_id,
                    spec["artifact_digest"],
                    Jsonb(
                        {
                            "execution_mode": "orchestrated_v2",
                            "execution_id": execution_id,
                            "run_id": run["run_id"],
                            "task_id": assignment["task_id"],
                            "assignment_id": assignment["assignment_id"],
                            "attempt": assignment["attempt"],
                            "asset_ref": f"console_upload:{upload['id']}",
                            "content_hash": upload["sha256"],
                            "pipeline_id": pipeline["orchestration_pipeline_id"],
                            "pipeline_revision": pipeline["orchestration_revision"],
                            "graph_digest": pipeline["graph_digest"],
                        }
                    ),
                    owner,
                    draft["id"],
                    run["deadline_unix_ms"],
                ),
            )

        updated = one(
            conn,
            """
            UPDATE console_job_draft
            SET state='processing',target_node_id=%s,error_code=NULL,error_detail=NULL,
                dispatched_at=now(),completed_at=NULL
            WHERE id=%s
            RETURNING *
            """,
            (target_node_id, draft["id"]),
        )
        audit(conn, owner, "job.dispatch.orchestrated_v2", draft["id"])
        return {
            **updated,
            "execution_id": execution_id,
            "run_id": run["run_id"],
            "execution_mode": "orchestrated_v2",
            "pipeline_revision": pipeline["orchestration_revision"],
            "graph_digest": pipeline["graph_digest"],
            "modality_summary": execution["modality_summary"],
        }

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

        pipeline = one(
            conn,
            "SELECT * FROM console_pipeline WHERE id=%s FOR SHARE",
            (draft["pipeline_id"],),
        )
        if not pipeline:
            fail(422, "pipeline_not_available")
        if pipeline.get("execution_mode") == "orchestrated_v2":
            return _dispatch_orchestrated_v2(
                conn,
                draft=draft,
                upload=upload,
                pipeline=pipeline,
                owner=owner,
                target_node_id=target_node_id,
            )

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
                    "SELECT j.*, e.execution_id,e.run_id,e.pipeline_revision,e.graph_digest,"
                    "e.state AS execution_state,e.modality_summary "
                    "FROM console_job_draft j "
                    "LEFT JOIN LATERAL ("
                    "  SELECT * FROM console_job_execution e WHERE e.job_id=j.id "
                    "  ORDER BY e.created_at DESC LIMIT 1"
                    ") e ON true WHERE j.owner=%s "
                    "ORDER BY j.created_at DESC,j.id LIMIT %s OFFSET %s"
                ),
                (p.name, limit, offset),
            )
            total = conn.execute(
                "SELECT count(*) FROM console_job_draft WHERE owner=%s", (p.name,)
            ).fetchone()[0]
        for item in items:
            item["execution_mode"] = (
                "orchestrated_v2" if item.get("execution_id") else "legacy_ocr_v1"
            )
            item["reason"] = item.get("error_code") or (
                "" if item.get("state") in {"processing", "completed"} else ""
            )
        return out({"items": items, "total": total}, pb.JobDraftList)

    @app.get("/v1/jobs/{key}/execution")
    def job_execution(
        key: str,
        execution_id: str = Query("", max_length=128),
        p: Annotated[object, Depends(auth.require("jobs:read"))] = None,
    ):
        """读取任务绑定的 Revision、任务、回执与安全状态摘要，不返回媒体或模型输出正文。"""
        with pool.connection() as conn:
            where_execution = "AND e.execution_id=%s" if execution_id else ""
            params = (key, p.name, execution_id) if execution_id else (key, p.name)
            execution = one(
                conn,
                """
                SELECT e.* FROM console_job_execution e
                JOIN console_job_draft j ON j.id=e.job_id
                WHERE e.job_id=%s AND j.owner=%s
                """
                + where_execution
                + " ORDER BY e.created_at DESC LIMIT 1",
                params,
            )
            if not execution:
                fail(404, "job_execution_not_found")
            tasks = rows(
                conn,
                """
                SELECT task_id,node_id,attempt,max_attempts,required,state,assignment_id,
                       reason_code,error_detail,output_ref,created_at,updated_at
                FROM pipeline_task WHERE run_id=%s ORDER BY created_at,node_id
                """,
                (execution["run_id"],),
            )
            receipts = rows(
                conn,
                """
                SELECT task_id,attempt,assignment_id,plugin_id,artifact_digest,config_hash,
                       input_count,output_count,result_manifest_ref,reason_code,receipt_digest,
                       started_at,completed_at
                FROM task_execution_receipt WHERE run_id=%s
                ORDER BY created_at,task_id
                """,
                (execution["run_id"],),
            )
        return {"execution": execution, "tasks": tasks, "receipts": receipts}

    @app.get("/v1/executions/{execution_id}/timeline")
    def execution_timeline(
        execution_id: str,
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        """读取每秒覆盖事实的最新版本；没有素材的格子照样返回其可解释状态。"""
        with pool.connection() as conn:
            execution = one(
                conn,
                """
                SELECT e.execution_id,e.run_id,e.pipeline_id,e.pipeline_revision,e.graph_digest,
                       e.state,e.modality_summary
                FROM console_job_execution e
                JOIN console_job_draft j ON j.id=e.job_id
                WHERE e.execution_id=%s AND j.owner=%s
                """,
                (execution_id, p.name),
            )
            if not execution:
                fail(404, "execution_not_found")
            windows = rows(
                conn,
                """
                WITH current_windows AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY execution_id,stream_id,start_ms,end_ms
                        ORDER BY state_revision DESC
                    ) AS ordinal
                    FROM timeline_window_state WHERE execution_id=%s
                )
                SELECT execution_id,stream_id,start_ms,end_ms,state_revision,sampling_state,
                       modality_states,reason_codes,observed_at
                FROM current_windows WHERE ordinal=1
                ORDER BY stream_id,start_ms,end_ms
                """,
                (execution_id,),
            )
            materials_for_execution = rows(
                conn,
                """
                SELECT DISTINCT ON (m.material_unit_id)
                       m.material_unit_id,m.revision AS material_revision,m.stream_id,
                       m.start_ms,m.end_ms,m.status,
                       (SELECT count(*) FROM material_observation o
                        WHERE (o.material_unit_id,o.revision)=(m.material_unit_id,m.revision))
                       AS observation_count
                FROM material_execution e JOIN material_unit m
                  ON (m.material_unit_id,m.revision)=(e.material_unit_id,e.material_revision)
                WHERE e.execution_id=%s
                ORDER BY m.material_unit_id,m.revision DESC
                """,
                (execution_id,),
            )
        return {
            "execution": execution,
            "windows": windows,
            "material_references": materials_for_execution,
        }

    @app.get("/v1/executions/{execution_id}/materials")
    def execution_materials(
        execution_id: str,
        offset: int = Query(0, ge=0, le=7200),
        limit: int = Query(100, ge=1, le=100),
        p: Annotated[object, Depends(auth.require("materials:read"))] = None,
    ):
        """按时间分页水合素材；完整时间轴使用轻量摘要，不一次加载全部模型载荷。"""
        with pool.connection() as conn:
            if not one(
                conn,
                """SELECT e.execution_id FROM console_job_execution e
                JOIN console_job_draft j ON j.id=e.job_id
                WHERE e.execution_id=%s AND j.owner=%s""",
                (execution_id, p.name),
            ):
                fail(404, "execution_not_found")
            data = conn.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (m.material_unit_id) m.*
                    FROM material_execution e JOIN material_unit m
                      ON (m.material_unit_id,m.revision)=(e.material_unit_id,e.material_revision)
                    WHERE e.execution_id=%s ORDER BY m.material_unit_id,m.revision DESC
                ) SELECT contract_bytes FROM latest
                ORDER BY stream_id,start_ms,material_unit_id LIMIT %s OFFSET %s
                """,
                (execution_id, limit, offset),
            ).fetchall()
        return {"materials": [out(material_pb2.MaterialUnit.FromString(row[0])) for row in data]}

    @app.post("/v1/executions/{execution_id}:segment-seconds")
    def segment_seconds(
        execution_id: str,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        """显式补齐历史执行：保留原方案和事实，记录本次每秒摘要请求。"""
        with pool.connection() as conn:
            execution = one(
                conn,
                """SELECT e.* FROM console_job_execution e
                JOIN console_job_draft j ON j.id=e.job_id
                WHERE e.execution_id=%s AND j.owner=%s FOR UPDATE OF e""",
                (execution_id, p.name),
            )
            if not execution:
                fail(404, "execution_not_found")
            if execution["state"] not in {
                "ready_for_review",
                "succeeded",
                "succeeded_with_partial_enrichment",
            }:
                fail(409, "execution_fast_path_not_ready")
            revision = one(
                conn,
                "SELECT * FROM pipeline_revision WHERE pipeline_id=%s AND revision=%s",
                (execution["pipeline_id"], execution["pipeline_revision"]),
            )
            delayed = next(
                (
                    n.get("delayed_enrichments")
                    for n in revision["definition_json"]["nodes"]
                    if n.get("id") == "timeline_fusion"
                ),
                None,
            )
            if not delayed:
                fail(409, "execution_delayed_model_missing")
            asset = one(
                conn,
                """SELECT a.*,s.source_id FROM media_asset a
                JOIN stream_session s ON s.stream_id=a.stream_id
                JOIN console_job_draft j ON a.object_uri='upload://' || j.asset_id
                WHERE j.id=%s AND j.owner=%s""",
                (execution["job_id"], p.name),
            )
            if not asset:
                fail(409, "timeline_source_missing")
            try:
                units = second_windows.ensure_materials(
                    conn,
                    execution_id=execution_id,
                    asset_id=asset["asset_id"],
                    pending=[vlm_delayed.VLM_MODALITY],
                )
                description = media_pb2.MediaSourceDescription(
                    source={
                        "stream_id": asset["stream_id"],
                        "source_id": asset["source_id"],
                        "content_hash": asset["sha256"],
                    },
                    duration_ms=asset["duration_ms"],
                )
                queued = vlm_delayed.enqueue_vlm_tasks(
                    conn,
                    execution=execution,
                    revision=revision,
                    description=description,
                    items={},
                    units=units,
                    full_seconds=True,
                )
            except ValueError as error:
                fail(422, str(error))
            conn.execute(
                """UPDATE console_job_execution
                SET state=CASE WHEN %s>0 THEN 'ready_for_review' ELSE state END,
                    completed_at=CASE WHEN %s>0 THEN NULL ELSE completed_at END,
                    modality_summary=modality_summary || %s::jsonb WHERE execution_id=%s""",
                (
                    len(queued),
                    len(queued),
                    Jsonb(
                        {
                            "segmentation": {
                                "window_ms": 1000,
                                "windows": len(units),
                                "vlm_interval_override_ms": 1000,
                            }
                        }
                    ),
                    execution_id,
                ),
            )
            audit(conn, p.name, "execution.segment_seconds", execution_id)
        return {"materials": len(units), "vlm_tasks_enqueued": len(queued)}

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

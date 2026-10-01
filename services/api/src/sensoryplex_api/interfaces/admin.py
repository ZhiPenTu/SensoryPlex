"""插件与处理方案的持久配置，不冒充 Runtime 安装、启动或发布。"""

from typing import Annotated

from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from fastapi import Body, Depends, Query
from google.protobuf.json_format import MessageToDict

from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field
from ..infrastructure import multimodal, orchestration
from ..infrastructure.catalog import catalog, plugin
from ..infrastructure.plugin_configurations import normalize_configuration, save_configuration


def register(app, pool, auth, settings):
    @app.get("/admin/v1/catalog")
    def list_catalog(p: Annotated[object, Depends(auth.require("plugins:manage"))] = None):
        return out({"items": catalog(settings)}, pb.PluginList)

    @app.get("/admin/v1/plugins")
    def installed(p: Annotated[object, Depends(auth.require("plugins:manage"))] = None):
        fail(501, "runtime_plugin_inventory_not_attached")

    @app.post("/admin/v1/plugin-installations")
    def install(p: Annotated[object, Depends(auth.require("plugins:manage"))] = None):
        fail(501, "runtime_plugin_installer_not_attached")

    @app.post("/admin/v1/plugin-installations/{key}:enable")
    @app.post("/admin/v1/plugin-installations/{key}:disable")
    @app.post("/admin/v1/plugin-installations/{key}:uninstall")
    def lifecycle(key: str, p: Annotated[object, Depends(auth.require("plugins:manage"))] = None):
        fail(501, "runtime_plugin_lifecycle_not_attached")

    @app.get("/admin/v1/plugin-configurations")
    def configurations(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            items = rows(
                conn,
                (
                    "SELECT * FROM console_plugin_config ORDER BY created_at DESC,id "
                    "LIMIT %s OFFSET %s"
                ),
                (limit, offset),
            )
            total = conn.execute("SELECT count(*) FROM console_plugin_config").fetchone()[0]
        return out({"items": items, "total": total}, pb.PluginConfigList)

    @app.post("/admin/v1/plugin-configurations", status_code=201)
    def save_config(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        req = parse(body, pb.SavePluginConfig)
        text_field(req.name)
        entry = plugin(settings, req.plugin_id)
        config = normalize_configuration(entry, MessageToDict(req.config))
        with pool.connection() as conn:
            result = save_configuration(
                conn,
                plugin_id=req.plugin_id,
                name=req.name,
                config=config,
                created_by=p.name,
            )
        return out(result, pb.PluginConfig)

    @app.get("/v1/pipelines")
    def pipelines(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
        p: Annotated[object, Depends(auth.require(None))] = None,
    ):
        if not p.scopes.intersection({"jobs:read", "pipelines:manage"}):
            fail(403, "permission_denied")
        with pool.connection() as conn:
            items = rows(
                conn,
                "SELECT * FROM console_pipeline ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                (limit, offset),
            )
            total = conn.execute("SELECT count(*) FROM console_pipeline").fetchone()[0]
        return out({"items": items, "total": total}, pb.PipelineList)

    @app.get("/v1/pipelines/{key}")
    def pipeline_detail(
        key: str,
        p: Annotated[object, Depends(auth.require(None))] = None,
    ):
        if not p.scopes.intersection({"jobs:read", "pipelines:manage"}):
            fail(403, "permission_denied")
        with pool.connection() as conn:
            pipeline = one(
                conn,
                "SELECT * FROM console_pipeline WHERE id=%s",
                (key,),
            )
            if not pipeline:
                fail(404, "pipeline_not_found")

            revision_data = {}
            config_ids = set()

            if pipeline.get("execution_mode") == "orchestrated_v2" and pipeline.get(
                "orchestration_pipeline_id"
            ):
                rev = one(
                    conn,
                    "SELECT pipeline_id, revision, graph_digest, definition_json, "
                    "created_by, created_at "
                    "FROM pipeline_revision WHERE pipeline_id=%s AND revision=%s",
                    (pipeline["orchestration_pipeline_id"], pipeline["orchestration_revision"]),
                )
                if rev:
                    def_json = rev.get("definition_json") or {}
                    nodes = def_json.get("nodes") or []
                    edges = def_json.get("edges") or []
                    for node in nodes:
                        if node.get("config_id"):
                            config_ids.add(node["config_id"])
                        for delayed in node.get("delayed_enrichments") or []:
                            if delayed.get("config_id"):
                                config_ids.add(delayed["config_id"])
                    revision_data = {
                        "pipeline_id": rev["pipeline_id"],
                        "revision": rev["revision"],
                        "graph_digest": rev["graph_digest"],
                        "nodes": nodes,
                        "edges": edges,
                        "created_by": rev["created_by"],
                        "created_at": rev["created_at"].isoformat()
                        if rev.get("created_at")
                        else "",
                    }
            elif pipeline.get("config_id"):
                config_ids.add(pipeline["config_id"])

            configs_dict = {}
            if config_ids:
                cfg_rows = rows(
                    conn,
                    "SELECT id, plugin_id, name, revision, config, config_hash "
                    "FROM console_plugin_config WHERE id = ANY(%s)",
                    (list(config_ids),),
                )
                for cfg in cfg_rows:
                    configs_dict[cfg["id"]] = {
                        "id": cfg["id"],
                        "plugin_id": cfg["plugin_id"],
                        "name": cfg["name"],
                        "revision": cfg["revision"],
                        "config": cfg["config"],
                        "config_hash": cfg["config_hash"],
                    }

            return out(
                {
                    "pipeline": pipeline,
                    "revision": revision_data,
                    "configs": configs_dict,
                },
                pb.PipelineDetail,
            )

    @app.post("/admin/v1/pipelines", status_code=201)
    def save_pipeline(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        req = parse(body, pb.SavePipeline)
        text_field(req.name)
        if len(req.description) > 2000:
            fail(422, "description_too_long")
        entry = plugin(settings, req.plugin_id)
        key = identifier("pipeline")
        with pool.connection() as conn:
            config = one(
                conn,
                "SELECT id FROM console_plugin_config WHERE id=%s AND plugin_id=%s",
                (req.config_id, req.plugin_id),
            )
            if not config:
                fail(422, "plugin_config_not_found")
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("pipeline:" + req.name,)
            )
            revision = conn.execute(
                "SELECT coalesce(max(revision),0)+1 FROM console_pipeline WHERE name=%s",
                (req.name,),
            ).fetchone()[0]
            result = one(
                conn,
                (
                    "INSERT INTO "
                    "console_pipeline(id,name,description,plugin_id,plugin_digest,config_id,"
                    "revision,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *"
                ),
                (
                    key,
                    req.name,
                    req.description,
                    req.plugin_id,
                    entry["digest"],
                    req.config_id,
                    revision,
                    p.name,
                ),
            )
            audit(conn, p.name, "pipeline.draft.save", key)
        return out(result, pb.Pipeline)

    @app.post("/admin/v1/multimodal-pipelines:validate")
    def validate_multimodal_pipeline(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        """校验受限文件多模态图，不产生 Revision 或 Console 方案行。"""
        with pool.connection() as conn:
            nodes, edges, policy = multimodal.build_graph(conn, settings, body)
            valid, errors, graph_digest, topo, _ = orchestration.validate_and_normalize_graph(
                nodes, edges
            )
        return {
            "valid": valid,
            "errors": errors,
            "graph_digest": graph_digest,
            "topological_order": topo,
            "nodes": nodes,
            "edges": edges,
            "policy": policy,
        }

    @app.post("/admin/v1/multimodal-pipelines", status_code=201)
    def save_multimodal_pipeline(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        """创建只可新增的 v2 Console Pipeline，并把图身份绑定到不可变 Revision。"""
        name = text_field(str(body.get("name", "")), 120)
        description = str(body.get("description", ""))
        if len(description) > 2000:
            fail(422, "description_too_long")
        console_id = identifier("pipeline")
        orchestration_id = identifier("pipeline_revision")
        with pool.connection() as conn:
            nodes, edges, _ = multimodal.build_graph(conn, settings, body)
            published = orchestration.publish_pipeline_revision(
                conn,
                owner=p.name,
                pipeline_id=orchestration_id,
                name=name,
                description=description,
                nodes=nodes,
                edges=edges,
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("pipeline:" + name,)
            )
            revision = conn.execute(
                "SELECT coalesce(max(revision),0)+1 FROM console_pipeline WHERE name=%s",
                (name,),
            ).fetchone()[0]
            ocr_config = next(node["config_id"] for node in nodes if node["id"] == "ocr_fast")
            result = one(
                conn,
                """
                INSERT INTO console_pipeline(
                    id,name,description,plugin_id,plugin_digest,config_id,revision,created_by,
                    execution_mode,orchestration_pipeline_id,orchestration_revision,graph_digest
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'orchestrated_v2',%s,%s,%s)
                RETURNING *
                """,
                (
                    console_id,
                    name,
                    description,
                    "org.sensoryplex.multimodal-file",
                    published["graph_digest"],
                    ocr_config,
                    revision,
                    p.name,
                    orchestration_id,
                    published["revision"],
                    published["graph_digest"],
                ),
            )
            audit(conn, p.name, "multimodal_pipeline.draft.save", console_id)
        return out(result, pb.Pipeline)

    @app.post("/admin/v1/pipelines/{key}:archive")
    def archive(key: str, p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None):
        with pool.connection() as conn:
            if not one(conn, "SELECT id FROM console_pipeline WHERE id=%s FOR UPDATE", (key,)):
                fail(404, "pipeline_not_found")
            if one(
                conn,
                "SELECT id FROM console_job_draft WHERE pipeline_id=%s AND state='draft' LIMIT 1",
                (key,),
            ):
                fail(409, "pipeline_referenced_by_drafts")
            conn.execute("UPDATE console_pipeline SET state='archived' WHERE id=%s", (key,))
            audit(conn, p.name, "pipeline.archive", key)
        return out(pb.EmptyResponse())

    @app.post("/admin/v1/pipelines/{key}:publish")
    def publish(key: str, p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None):
        with pool.connection() as conn:
            pl = one(conn, "SELECT * FROM console_pipeline WHERE id=%s FOR UPDATE", (key,))
            if not pl:
                fail(404, "pipeline_not_found")
            cfg = one(
                conn,
                "SELECT id FROM console_plugin_config WHERE id=%s",
                (pl["config_id"],),
            )
            if not cfg:
                fail(422, "pipeline_config_missing")
            if pl.get("execution_mode") == "orchestrated_v2":
                bound = one(
                    conn,
                    """
                    SELECT graph_digest FROM pipeline_revision
                    WHERE pipeline_id=%s AND revision=%s
                    """,
                    (pl["orchestration_pipeline_id"], pl["orchestration_revision"]),
                )
                if not bound or bound["graph_digest"] != pl["graph_digest"]:
                    fail(409, "pipeline_revision_binding_invalid")
            updated = one(
                conn,
                "UPDATE console_pipeline SET state='published' WHERE id=%s RETURNING *",
                (key,),
            )
            audit(conn, p.name, "pipeline.publish", key)
        return out(updated, pb.Pipeline)

    @app.get("/admin/v1/audit-events")
    def events(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
        p: Annotated[object, Depends(auth.require("audit:read"))] = None,
    ):
        with pool.connection() as conn:
            items = rows(
                conn,
                "SELECT * FROM console_audit ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                (limit, offset),
            )
            total = conn.execute("SELECT count(*) FROM console_audit").fetchone()[0]
        return out({"items": items, "total": total}, pb.AuditList)

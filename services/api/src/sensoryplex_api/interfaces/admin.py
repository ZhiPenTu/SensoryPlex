"""插件与处理方案的持久配置，不冒充 Runtime 安装、启动或发布。"""

import hashlib
import json
from typing import Annotated

from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from fastapi import Body, Depends, Query
from google.protobuf.json_format import MessageToDict
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field
from ..infrastructure.catalog import catalog, plugin


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
        config = MessageToDict(req.config)
        # handoff_endpoint 是 Runtime 的传输绑定，草稿阶段不接收宿主内部地址。
        schema = dict(entry["config_schema"])
        schema["properties"] = {
            k: v for k, v in schema["properties"].items() if k != "handoff_endpoint"
        }
        schema["required"] = [k for k in schema.get("required", []) if k != "handoff_endpoint"]
        if list(Draft202012Validator(schema).iter_errors(config)):
            fail(422, "plugin_config_invalid")
        if config.get("endpoint", "http://127.0.0.1:11434") != "http://127.0.0.1:11434":
            fail(422, "only_local_model_endpoint_allowed")
        if (
            not 1 <= config.get("timeout_s", 180) <= 300
            or len(config.get("prompt", "")) > 4000
            or len(config.get("model", "")) > 200
        ):
            fail(422, "plugin_config_limits_exceeded")
        key = identifier("config")
        config_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        with pool.connection() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (req.plugin_id + ":" + req.name,),
            )
            revision = conn.execute(
                (
                    "SELECT coalesce(max(revision),0)+1 FROM console_plugin_config "
                    "WHERE plugin_id=%s AND name=%s"
                ),
                (req.plugin_id, req.name),
            ).fetchone()[0]
            result = one(
                conn,
                (
                    "INSERT INTO "
                    "console_plugin_config(id,plugin_id,name,revision,config,config_hash,"
                    "created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *"
                ),
                (key, req.plugin_id, req.name, revision, Jsonb(config), config_hash, p.name),
            )
            audit(conn, p.name, "plugin.config.save", key)
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
        fail(501, "runtime_pipeline_validation_not_attached")

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

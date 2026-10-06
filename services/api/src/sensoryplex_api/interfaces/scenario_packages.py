"""ADR-029 P3 场景化产品包 (Scenario Product Packages) 控制面 API。

将不可变 Pipeline revision、配置 schema、RBAC、节点调度策略打包为可交付的产品方案。
提供产品包草稿创建、发布、激活、归档、详情查询与便携式 Manifest 导入/导出。
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Body, Depends, Query
from psycopg.types.json import Jsonb

from ..contracts import audit, fail, identifier, one, rows, text_field


def _format_package(pkg: dict[str, Any]) -> dict[str, Any]:
    return {
        "package_id": pkg["package_id"],
        "name": pkg["name"],
        "description": pkg.get("description", ""),
        "version": pkg.get("version", "1.0.0"),
        "pipeline_id": pkg["pipeline_id"],
        "pipeline_revision": int(pkg["pipeline_revision"]),
        "graph_digest": pkg["graph_digest"],
        "config_schema": pkg.get("config_schema") or {},
        "rbac_scopes": pkg.get("rbac_scopes") or [],
        "scheduling_policy": pkg.get("scheduling_policy") or {},
        "state": pkg["state"],
        "created_by": pkg["created_by"],
        "created_at": pkg["created_at"].isoformat() if pkg.get("created_at") else "",
        "updated_at": pkg["updated_at"].isoformat() if pkg.get("updated_at") else "",
    }


def register(app, pool, auth, settings):
    @app.get("/v1/scenario-packages")
    def list_packages(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
        state: str = Query("", max_length=32),
        p: Annotated[object, Depends(auth.require("jobs:read"))] = None,
    ):
        """列出场景产品包列表，支持按生命周期状态过滤。"""
        with pool.connection() as conn:
            where_clause = "WHERE state = %s" if state else ""
            params = (state, limit, offset) if state else (limit, offset)
            query = f"""
                SELECT * FROM scenario_package
                {where_clause}
                ORDER BY created_at DESC, package_id ASC
                LIMIT %s OFFSET %s
            """
            items = rows(conn, query, params)
            count_query = f"SELECT count(*) FROM scenario_package {where_clause}"
            count_params = (state,) if state else ()
            total = conn.execute(count_query, count_params).fetchone()[0]

        return {
            "items": [_format_package(item) for item in items],
            "total": total,
        }

    @app.get("/v1/scenario-packages/{package_id}")
    def get_package(
        package_id: str,
        p: Annotated[object, Depends(auth.require("jobs:read"))] = None,
    ):
        """获取场景产品包详情，包含底层不可变 Pipeline 拓扑定义与 Schema。"""
        with pool.connection() as conn:
            pkg = one(
                conn,
                "SELECT * FROM scenario_package WHERE package_id=%s",
                (package_id,),
            )
            if not pkg:
                fail(404, "scenario_package_not_found")
            rev = one(
                conn,
                "SELECT definition_json FROM pipeline_revision "
                "WHERE pipeline_id=%s AND revision=%s",
                (pkg["pipeline_id"], pkg["pipeline_revision"]),
            )
            definition = rev["definition_json"] if rev else {}

        res = _format_package(pkg)
        res["pipeline_definition"] = definition
        return res

    @app.post("/admin/v1/scenario-packages", status_code=201)
    def create_package(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        """创建场景产品包草稿，绑定已验证的不可变 Pipeline Revision。"""
        name = text_field(str(body.get("name", "")), 120)
        description = str(body.get("description", ""))
        version = str(body.get("version", "1.0.0")).strip() or "1.0.0"
        pipeline_id = str(body.get("pipeline_id", "")).strip()
        pipeline_revision = int(body.get("pipeline_revision", 0))
        config_schema = body.get("config_schema", {})
        rbac_scopes = body.get("rbac_scopes", ["jobs:read", "jobs:write", "materials:read"])
        scheduling_policy = body.get("scheduling_policy", {})

        if not pipeline_id or pipeline_revision <= 0:
            fail(422, "pipeline_revision_required")
        if not isinstance(config_schema, dict):
            fail(422, "invalid_config_schema")
        if not isinstance(rbac_scopes, list):
            fail(422, "invalid_rbac_scopes")
        if not isinstance(scheduling_policy, dict):
            fail(422, "invalid_scheduling_policy")

        package_id = identifier("pkg")
        with pool.connection() as conn:
            rev = one(
                conn,
                "SELECT graph_digest FROM pipeline_revision WHERE pipeline_id=%s AND revision=%s",
                (pipeline_id, pipeline_revision),
            )
            if not rev:
                fail(404, "pipeline_revision_not_found")
            graph_digest = rev["graph_digest"]

            result = one(
                conn,
                """
                INSERT INTO scenario_package (
                    package_id, name, description, version, pipeline_id, pipeline_revision,
                    graph_digest, config_schema, rbac_scopes, scheduling_policy, state, created_by
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s
                ) RETURNING *
                """,
                (
                    package_id,
                    name,
                    description,
                    version,
                    pipeline_id,
                    pipeline_revision,
                    graph_digest,
                    Jsonb(config_schema),
                    Jsonb(rbac_scopes),
                    Jsonb(scheduling_policy),
                    p.name,
                ),
            )
            audit(conn, p.name, "scenario_package.create", package_id)

        return _format_package(result)

    @app.post("/admin/v1/scenario-packages/{package_id}:publish")
    def publish_package(
        package_id: str,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        """发布场景产品包：锁定版本与配置，允许在生产环境中激活与派发。"""
        with pool.connection() as conn:
            pkg = one(
                conn,
                "SELECT * FROM scenario_package WHERE package_id=%s FOR UPDATE",
                (package_id,),
            )
            if not pkg:
                fail(404, "scenario_package_not_found")
            if pkg["state"] == "archived":
                fail(409, "package_already_archived")

            updated = one(
                conn,
                "UPDATE scenario_package SET state='published', updated_at=now() "
                "WHERE package_id=%s RETURNING *",
                (package_id,),
            )
            audit(conn, p.name, "scenario_package.publish", package_id)

        return _format_package(updated)

    @app.post("/admin/v1/scenario-packages/{package_id}:activate")
    def activate_package(
        package_id: str,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        """激活场景产品包：设置为活跃产品方案。"""
        with pool.connection() as conn:
            pkg = one(
                conn,
                "SELECT * FROM scenario_package WHERE package_id=%s FOR UPDATE",
                (package_id,),
            )
            if not pkg:
                fail(404, "scenario_package_not_found")
            if pkg["state"] == "archived":
                fail(409, "package_already_archived")

            updated = one(
                conn,
                "UPDATE scenario_package SET state='active', updated_at=now() "
                "WHERE package_id=%s RETURNING *",
                (package_id,),
            )
            audit(conn, p.name, "scenario_package.activate", package_id)

        return _format_package(updated)

    @app.post("/admin/v1/scenario-packages/{package_id}:archive")
    def archive_package(
        package_id: str,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        """归档历史场景产品包。"""
        with pool.connection() as conn:
            pkg = one(
                conn,
                "SELECT * FROM scenario_package WHERE package_id=%s FOR UPDATE",
                (package_id,),
            )
            if not pkg:
                fail(404, "scenario_package_not_found")

            updated = one(
                conn,
                "UPDATE scenario_package SET state='archived', updated_at=now() "
                "WHERE package_id=%s RETURNING *",
                (package_id,),
            )
            audit(conn, p.name, "scenario_package.archive", package_id)

        return _format_package(updated)

    @app.get("/v1/scenario-packages/{package_id}/manifest")
    def export_manifest(
        package_id: str,
        p: Annotated[object, Depends(auth.require("jobs:read"))] = None,
    ):
        """导出场景产品包便携式 JSON Manifest，供离线分发或跨集群导入。"""
        with pool.connection() as conn:
            pkg = one(
                conn,
                "SELECT * FROM scenario_package WHERE package_id=%s",
                (package_id,),
            )
            if not pkg:
                fail(404, "scenario_package_not_found")
            rev = one(
                conn,
                "SELECT definition_json FROM pipeline_revision "
                "WHERE pipeline_id=%s AND revision=%s",
                (pkg["pipeline_id"], pkg["pipeline_revision"]),
            )
            if not rev:
                fail(404, "pipeline_revision_not_found")

        return {
            "manifest_version": "sensoryplex.scenario_package/v1",
            "package_id": pkg["package_id"],
            "name": pkg["name"],
            "description": pkg["description"],
            "version": pkg["version"],
            "graph_digest": pkg["graph_digest"],
            "pipeline": {
                "pipeline_id": pkg["pipeline_id"],
                "revision": pkg["pipeline_revision"],
                "definition": rev["definition_json"],
            },
            "config_schema": pkg["config_schema"],
            "rbac_scopes": pkg["rbac_scopes"],
            "scheduling_policy": pkg["scheduling_policy"],
            "exported_at": datetime.now(UTC).isoformat(),
        }

    @app.post("/admin/v1/scenario-packages:import", status_code=201)
    def import_manifest(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        """导入场景产品包 Manifest 并校验不可变图身份。"""
        if body.get("manifest_version") != "sensoryplex.scenario_package/v1":
            fail(422, "unsupported_manifest_version")

        name = text_field(str(body.get("name", "")), 120)
        description = str(body.get("description", ""))
        version = str(body.get("version", "1.0.0")).strip() or "1.0.0"
        pipeline_data = body.get("pipeline", {})
        pipeline_id = str(pipeline_data.get("pipeline_id", "")).strip()
        pipeline_revision = int(pipeline_data.get("revision", 0))
        definition = pipeline_data.get("definition", {})
        config_schema = body.get("config_schema", {})
        rbac_scopes = body.get("rbac_scopes", [])
        scheduling_policy = body.get("scheduling_policy", {})

        if not pipeline_id or pipeline_revision <= 0:
            fail(422, "pipeline_identity_missing")

        from ..infrastructure import orchestration as orch

        package_id = identifier("pkg")
        with pool.connection() as conn:
            existing_rev = one(
                conn,
                "SELECT graph_digest FROM pipeline_revision WHERE pipeline_id=%s AND revision=%s",
                (pipeline_id, pipeline_revision),
            )
            if not existing_rev:
                existing_def = one(
                    conn,
                    "SELECT pipeline_id FROM pipeline_definition WHERE pipeline_id=%s",
                    (pipeline_id,),
                )
                if not existing_def:
                    conn.execute(
                        "INSERT INTO pipeline_definition(pipeline_id, name, description, owner) "
                        "VALUES (%s, %s, %s, %s)",
                        (pipeline_id, name, description, p.name),
                    )
                nodes = definition.get("nodes", [])
                edges = definition.get("edges", [])
                valid, errors, graph_digest, _, _ = orch.validate_and_normalize_graph(nodes, edges)
                if not valid:
                    fail(422, "imported_pipeline_graph_invalid")
                conn.execute(
                    """
                    INSERT INTO pipeline_revision(
                        pipeline_id, revision, graph_digest, definition_json, created_by
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (pipeline_id, pipeline_revision, graph_digest, Jsonb(definition), p.name),
                )
            else:
                graph_digest = existing_rev["graph_digest"]

            result = one(
                conn,
                """
                INSERT INTO scenario_package (
                    package_id, name, description, version, pipeline_id, pipeline_revision,
                    graph_digest, config_schema, rbac_scopes, scheduling_policy, state, created_by
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'published', %s
                ) RETURNING *
                """,
                (
                    package_id,
                    name,
                    description,
                    version,
                    pipeline_id,
                    pipeline_revision,
                    graph_digest,
                    Jsonb(config_schema),
                    Jsonb(rbac_scopes),
                    Jsonb(scheduling_policy),
                    p.name,
                ),
            )
            audit(conn, p.name, "scenario_package.import", package_id)

        return _format_package(result)

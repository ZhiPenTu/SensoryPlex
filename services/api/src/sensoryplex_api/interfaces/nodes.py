"""ADR-026 Web 主节点与局域网插件 worker 拓扑 API 接口。

包含管理侧节点 Registry、预检、部署意图下发、回滚，以及 Agent 注册与心跳通道。
"""

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from edge_material_sdk.generated.material.v1 import material_pb2
from edge_material_sdk.generated.media.v1 import media_pb2
from edge_material_sdk.generated.node.v1 import node_pb2 as pb
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as orchestration_pb
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime_pb
from fastapi import Body, Depends, Header, Query
from fastapi.responses import FileResponse, RedirectResponse
from google.protobuf.json_format import MessageToDict
from psycopg.types.json import Jsonb

from ..contracts import (
    audit,
    fail,
    hash_token,
    identifier,
    one,
    out,
    parse,
    rows,
    text_field,
    validate_processor_reason,
)
from ..infrastructure import materials, timeline_ingest, vlm_delayed
from ..infrastructure import orchestration as orchestrator
from ..infrastructure.catalog import plugin
from ..infrastructure.preflight import check_preflight
from .plugin_deploy import (
    apply_hot_report,
    intent_proto,
    purge_node_deployment_rows,
    record_runtime_observations,
)

ROOT = Path(__file__).resolve().parents[5]


def to_proto_node_status(status_str: str) -> str:
    mapping = {
        "candidate": "NODE_STATUS_CANDIDATE",
        "enrolling": "NODE_STATUS_ENROLLING",
        "ready": "NODE_STATUS_READY",
        "draining": "NODE_STATUS_DRAINING",
        "offline": "NODE_STATUS_OFFLINE",
        "revoked": "NODE_STATUS_REVOKED",
    }
    return mapping.get(status_str, "NODE_STATUS_UNSPECIFIED")


def register(app, pool, auth, settings, storage=None):
    if storage is None:
        from ..infrastructure.storage import create_storage_driver

        storage = getattr(app.state, "storage", None) or create_storage_driver(settings)

    def authenticated_node(conn, authorization: str | None, expected_node_id: str):
        """认证 Agent 会话并锁定到意图所属节点，不能只信请求体里的 node_id。"""
        token = ""
        if authorization and authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]
        if not token:
            fail(401, "missing_node_session_token")
        node = one(
            conn,
            "SELECT * FROM console_node WHERE session_token_hash=%s FOR SHARE",
            (hash_token(token),),
        )
        if not node:
            fail(401, "invalid_node_credentials")
        if node["node_id"] != expected_node_id:
            fail(403, "agent_node_mismatch")
        if node["status"] in {"revoked", "offline", "draining"}:
            fail(409, "agent_node_not_schedulable")
        return node

    from . import enrichments as enrichment_routes

    enrichment_routes.register(app, pool, settings, authenticated_node)

    def v2_intent_context(conn, intent_id: str, authorization: str | None, *, lock: bool = False):
        """读取一条 v2 任务意图的最小可信上下文。

        这里刻意不返回 blob 路径、插件 endpoint、命令或密钥。前两者分别只属于 API
        数据面下载与 Agent 本机热部署台账，不能借由控制面消息泄漏。
        """
        suffix = " FOR UPDATE" if lock else ""
        intent = one(
            conn,
            "SELECT * FROM console_deployment_intent WHERE id=%s" + suffix,
            (intent_id,),
        )
        if not intent:
            fail(404, "deployment_intent_not_found")
        if intent["action"] != "task_process":
            fail(409, "agent_intent_not_task_process")
        authenticated_node(conn, authorization, intent["node_id"])
        config = intent.get("config") or {}
        if config.get("execution_mode") != "orchestrated_v2":
            fail(409, "agent_legacy_task_execution_unsupported")
        required = (
            "execution_id",
            "run_id",
            "task_id",
            "assignment_id",
            "asset_ref",
            "content_hash",
        )
        if any(not isinstance(config.get(field), str) or not config[field] for field in required):
            fail(409, "task_intent_config_invalid")
        if intent["state"] not in {"dispatched", "completed"}:
            fail(409, "task_intent_not_active")

        execution = one(
            conn,
            "SELECT * FROM console_job_execution WHERE execution_id=%s AND run_id=%s",
            (config["execution_id"], config["run_id"]),
        )
        if not execution:
            fail(409, "task_execution_binding_missing")
        task = one(
            conn,
            "SELECT * FROM pipeline_task WHERE task_id=%s AND run_id=%s",
            (config["task_id"], config["run_id"]),
        )
        assignment = one(
            conn,
            "SELECT * FROM scheduler_assignment WHERE assignment_id=%s AND task_id=%s",
            (config["assignment_id"], config["task_id"]),
        )
        if not task or not assignment:
            fail(409, "task_assignment_missing")
        if (
            task["assignment_id"] != assignment["assignment_id"]
            or task["attempt"] != assignment["attempt"]
            or assignment["actual_node_id"] != intent["node_id"]
            or task["attempt"] != int(config.get("attempt") or 0)
        ):
            fail(409, "task_assignment_binding_invalid")
        if task["state"] not in {"assigned", "running", "succeeded", "failed", "cancelled"}:
            fail(409, "task_not_executable")

        revision = one(
            conn,
            "SELECT * FROM pipeline_revision WHERE pipeline_id=%s AND revision=%s",
            (execution["pipeline_id"], execution["pipeline_revision"]),
        )
        if not revision or revision["graph_digest"] != execution["graph_digest"]:
            fail(409, "task_revision_binding_invalid")
        node = next(
            (
                item
                for item in revision["definition_json"].get("nodes", [])
                if item.get("id") == task["node_id"]
            ),
            None,
        )
        if not node:
            fail(409, "task_node_missing_from_revision")
        upload = one(
            conn,
            """
            SELECT upload.id,upload.filename,upload.size_bytes,upload.content_type,upload.sha256
            FROM console_upload upload
            JOIN console_job_draft job ON job.asset_id=upload.id
            WHERE job.id=%s AND upload.sha256=%s AND upload.state='awaiting_admission'
            """,
            (execution["job_id"], config["content_hash"]),
        )
        if not upload:
            fail(409, "task_asset_binding_missing")
        return intent, config, execution, task, assignment, revision, node, upload

    def task_intent_payload(conn, assignment: dict, *, actor: str = "scheduler") -> dict | None:
        """把已分配 Task 收敛成一条受控 Agent 意图。

        同一 assignment 最多存在一条 task_process 意图；重复调度不会产生两个消费者。
        """
        execution = one(
            conn,
            "SELECT * FROM console_job_execution WHERE run_id=%s",
            (assignment["run_id"],),
        )
        if not execution:
            return None
        task = one(conn, "SELECT * FROM pipeline_task WHERE task_id=%s", (assignment["task_id"],))
        run = one(
            conn,
            "SELECT deadline_unix_ms FROM pipeline_run WHERE run_id=%s",
            (assignment["run_id"],),
        )
        revision = one(
            conn,
            "SELECT definition_json FROM pipeline_revision WHERE pipeline_id=%s AND revision=%s",
            (execution["pipeline_id"], execution["pipeline_revision"]),
        )
        job = one(
            conn, "SELECT asset_id,owner FROM console_job_draft WHERE id=%s", (execution["job_id"],)
        )
        upload = one(
            conn,
            "SELECT id,sha256 FROM console_upload WHERE id=%s AND state='awaiting_admission'",
            (job["asset_id"],),
        )
        node = next(
            item
            for item in revision["definition_json"].get("nodes", [])
            if item.get("id") == task["node_id"]
        )
        existing = one(
            conn,
            """
            SELECT id FROM console_deployment_intent
            WHERE action='task_process'
              AND config->>'assignment_id'=%s
              AND state IN ('pending','dispatched','completed')
            """,
            (assignment["assignment_id"],),
        )
        if existing:
            return None
        intent_id = identifier("task")
        conn.execute(
            """
            INSERT INTO console_deployment_intent(
                id,node_id,instance_id,action,artifact_digest,rollback_digest,config,
                state,created_by,job_id,deadline_unix_ms
            ) VALUES (%s,%s,NULL,'task_process',%s,NULL,%s,'pending',%s,%s,%s)
            """,
            (
                intent_id,
                assignment["actual_node_id"],
                node["artifact_digest"],
                Jsonb(
                    {
                        "execution_mode": "orchestrated_v2",
                        "execution_id": execution["execution_id"],
                        "run_id": assignment["run_id"],
                        "task_id": assignment["task_id"],
                        "assignment_id": assignment["assignment_id"],
                        "attempt": assignment["attempt"],
                        "asset_ref": f"console_upload:{upload['id']}",
                        "content_hash": upload["sha256"],
                        "pipeline_id": execution["pipeline_id"],
                        "pipeline_revision": execution["pipeline_revision"],
                        "graph_digest": execution["graph_digest"],
                    }
                ),
                actor,
                execution["job_id"],
                run["deadline_unix_ms"],
            ),
        )
        return {"intent_id": intent_id, "execution_id": execution["execution_id"]}

    def enqueue_ready_v2_tasks(conn, node_id: str, *, actor: str = "scheduler") -> list[dict]:
        """领取并写入刚解锁的同机 v2 Task；只处理这个节点，绝不静默改派。"""
        orchestrator.release_retry_wait_tasks(conn)
        assignments = orchestrator.schedule_ready_tasks(
            conn, candidate_node_id=node_id, is_co_located=True, max_tasks=16
        )
        return [
            item
            for assignment in assignments
            if (item := task_intent_payload(conn, assignment, actor=actor)) is not None
        ]

    # ── 管理员接口：节点管理与预检 ─────────────────────────────────────────

    @app.get("/admin/v1/nodes")
    def list_nodes(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            conn.execute(
                """
                UPDATE console_node
                SET status='offline', status_reason='heartbeat_timeout'
                WHERE status='ready' AND last_heartbeat_at < now() - interval '60 seconds'
                """
            )
            node_rows = rows(
                conn,
                (
                    "SELECT * FROM console_node "
                    "ORDER BY is_co_located DESC, enrolled_at DESC "
                    "LIMIT %s OFFSET %s"
                ),
                (limit, offset),
            )
            total = conn.execute("SELECT count(*) FROM console_node").fetchone()[0]

            items = []
            for nr in node_rows:
                inst_rows = rows(
                    conn,
                    (
                        "SELECT * FROM console_plugin_instance "
                        "WHERE node_id=%s ORDER BY created_at DESC"
                    ),
                    (nr["node_id"],),
                )
                item = dict(nr)
                item["status"] = to_proto_node_status(nr["status"])
                item["capabilities"] = {
                    "platform": nr["platform"],
                    "arch": nr["arch"],
                    "cpu_cores": nr["cpu_cores"],
                    "memory_bytes": nr["memory_bytes"],
                    "unified_memory_bytes": nr["unified_memory_bytes"],
                    "accelerators": nr["accelerators"],
                    "supported_artifacts": nr["supported_artifacts"],
                    "labels": nr["labels"],
                }
                item["instances"] = inst_rows
                items.append(item)

        return out({"items": items, "total": total}, pb.NodeList)

    @app.post("/admin/v1/nodes/enrollment-tokens", status_code=201)
    def create_enrollment_token(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        node_id = text_field(body.get("node_id", ""))
        expires_minutes = max(1, min(int(body.get("expires_in_minutes", 60)), 1440))
        token = "sp_enroll_" + secrets.token_hex(24)
        thash = hash_token(token)
        expires_at = datetime.now(UTC) + timedelta(minutes=expires_minutes)

        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO console_node_enrollment_token(
                    token_hash, node_id, created_by, expires_at
                ) VALUES (%s, %s, %s, %s)
                """,
                (thash, node_id, p.name, expires_at),
            )
            audit(conn, p.name, "node.token.create", node_id)

        return out(
            {
                "token": token,
                "node_id": node_id,
                "expires_at": expires_at.isoformat(),
                "created_at": datetime.now(UTC).isoformat(),
            },
            pb.EnrollmentToken,
        )

    @app.get("/admin/v1/nodes/{node_id}")
    def get_node(
        node_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            nr = one(conn, "SELECT * FROM console_node WHERE node_id=%s", (node_id,))
            if not nr:
                fail(404, "node_not_found")
            inst_rows = rows(
                conn,
                ("SELECT * FROM console_plugin_instance WHERE node_id=%s ORDER BY created_at DESC"),
                (node_id,),
            )
            item = dict(nr)
            item["status"] = to_proto_node_status(nr["status"])
            item["capabilities"] = {
                "platform": nr["platform"],
                "arch": nr["arch"],
                "cpu_cores": nr["cpu_cores"],
                "memory_bytes": nr["memory_bytes"],
                "unified_memory_bytes": nr["unified_memory_bytes"],
                "accelerators": nr["accelerators"],
                "supported_artifacts": nr["supported_artifacts"],
                "labels": nr["labels"],
            }
            item["instances"] = inst_rows
        return out(item, pb.NodeInfo)

    @app.post("/admin/v1/nodes/{node_id}:drain")
    def drain_node(
        node_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            nr = one(
                conn,
                "SELECT status FROM console_node WHERE node_id=%s FOR UPDATE",
                (node_id,),
            )
            if not nr:
                fail(404, "node_not_found")
            if nr["status"] == "revoked":
                fail(409, "node_already_revoked")
            conn.execute(
                (
                    "UPDATE console_node SET status='draining', "
                    "status_reason='Admin drain requested', updated_at=now() "
                    "WHERE node_id=%s"
                ),
                (node_id,),
            )
            audit(conn, p.name, "node.drain", node_id)
        return get_node(node_id, p)

    @app.post("/admin/v1/nodes/{node_id}:revoke")
    def revoke_node(
        node_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            nr = one(
                conn,
                "SELECT status FROM console_node WHERE node_id=%s FOR UPDATE",
                (node_id,),
            )
            if not nr:
                fail(404, "node_not_found")
            conn.execute(
                (
                    "UPDATE console_node SET status='revoked', "
                    "status_reason='Revoked by administrator', session_token_hash=NULL, "
                    "updated_at=now() WHERE node_id=%s"
                ),
                (node_id,),
            )
            audit(conn, p.name, "node.revoke", node_id)
        return get_node(node_id, p)

    @app.post("/admin/v1/nodes/{node_id}/preflight")
    def preflight(
        node_id: str,
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        req = parse(body, pb.PreflightRequest)
        with pool.connection() as conn:
            p_entry = plugin(settings, req.plugin_id, conn)
            nr = one(conn, "SELECT * FROM console_node WHERE node_id=%s", (node_id,))
            cfg = MessageToDict(req.config) if req.config else None
            if req.config_id and not cfg:
                cfg_row = one(
                    conn,
                    "SELECT config FROM console_plugin_config WHERE id=%s",
                    (req.config_id,),
                )
                if cfg_row:
                    cfg = cfg_row["config"]

            target_dp = req.data_plane_node_id or (
                nr["node_id"] if nr and nr["is_co_located"] else None
            )
            result = check_preflight(nr, p_entry, config=cfg, data_plane_node_id=target_dp)
            action = "node.preflight.pass" if result["eligible"] else "node.preflight.reject"
            audit(conn, p.name, action, f"{node_id}:{req.plugin_id}:{result['reason_code']}")

        return out(result, pb.PreflightResponse)

    @app.post("/admin/v1/nodes/{node_id}/plugins/{plugin_id}:deploy", status_code=201)
    def deploy_plugin(
        node_id: str,
        plugin_id: str,
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        config_id = body.get("config_id")
        config = body.get("config", {})

        with pool.connection() as conn:
            p_entry = plugin(settings, plugin_id, conn)
            nr = one(conn, "SELECT * FROM console_node WHERE node_id=%s FOR UPDATE", (node_id,))
            if not nr:
                fail(404, "node_not_found")

            if config_id and not config:
                cfg_row = one(
                    conn,
                    "SELECT config FROM console_plugin_config WHERE id=%s",
                    (config_id,),
                )
                if cfg_row:
                    config = cfg_row["config"]

            pre_res = check_preflight(nr, p_entry, config=config)
            if not pre_res["eligible"]:
                audit(
                    conn,
                    p.name,
                    "node.preflight.reject",
                    f"{node_id}:{plugin_id}:{pre_res['reason_code']}",
                )
                fail(422, pre_res["reason_code"])

            audit(conn, p.name, "node.preflight.pass", f"{node_id}:{plugin_id}")

            existing_inst = one(
                conn,
                "SELECT * FROM console_plugin_instance WHERE node_id=%s AND plugin_id=%s",
                (node_id, plugin_id),
            )
            previous_digest = existing_inst["artifact_digest"] if existing_inst else None
            inst_id = existing_inst["instance_id"] if existing_inst else identifier("inst")
            config_hash = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )

            inst = one(
                conn,
                """
                INSERT INTO console_plugin_instance(
                    instance_id, node_id, plugin_id, plugin_version, artifact_digest,
                    previous_digest, desired_state, actual_state, config_hash, config,
                    created_by, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'ready', 'installing', %s, %s, %s, now())
                ON CONFLICT (node_id, plugin_id) DO UPDATE SET
                    plugin_version=EXCLUDED.plugin_version,
                    previous_digest=console_plugin_instance.artifact_digest,
                    artifact_digest=EXCLUDED.artifact_digest,
                    desired_state='ready',
                    actual_state='installing',
                    config_hash=EXCLUDED.config_hash,
                    config=EXCLUDED.config,
                    error_code=NULL,
                    error_detail=NULL,
                    updated_at=now()
                RETURNING *
                """,
                (
                    inst_id,
                    node_id,
                    plugin_id,
                    p_entry["version"],
                    p_entry["digest"],
                    previous_digest,
                    config_hash,
                    Jsonb(config),
                    p.name,
                ),
            )

            intent_id = identifier("intent")
            conn.execute(
                """
                INSERT INTO console_deployment_intent(
                    id, node_id, instance_id, action, artifact_digest, rollback_digest,
                    config, state, created_by
                ) VALUES (%s, %s, %s, 'install', %s, %s, %s, 'pending', %s)
                """,
                (
                    intent_id,
                    node_id,
                    inst["instance_id"],
                    p_entry["digest"],
                    previous_digest,
                    Jsonb(config),
                    p.name,
                ),
            )
            audit(
                conn,
                p.name,
                "plugin.instance.deploy",
                f"{node_id}:{plugin_id}:{p_entry['digest']}",
            )

        return out(inst, pb.PluginInstance)

    @app.post("/admin/v1/nodes/{node_id}/plugins/{plugin_id}:rollback")
    def rollback_plugin(
        node_id: str,
        plugin_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            inst = one(
                conn,
                (
                    "SELECT * FROM console_plugin_instance "
                    "WHERE node_id=%s AND plugin_id=%s FOR UPDATE"
                ),
                (node_id, plugin_id),
            )
            if not inst:
                fail(404, "plugin_instance_not_found")
            if not inst["previous_digest"]:
                fail(422, "no_previous_digest_for_rollback")

            target_digest = inst["previous_digest"]
            intent_id = identifier("intent")
            conn.execute(
                """
                INSERT INTO console_deployment_intent(
                    id, node_id, instance_id, action, artifact_digest, rollback_digest,
                    config, state, created_by
                ) VALUES (%s, %s, %s, 'rollback', %s, NULL, %s, 'pending', %s)
                """,
                (
                    intent_id,
                    node_id,
                    inst["instance_id"],
                    target_digest,
                    Jsonb(inst["config"]),
                    p.name,
                ),
            )
            updated = one(
                conn,
                """
                UPDATE console_plugin_instance
                SET desired_state='ready', actual_state='rolled_back',
                    artifact_digest=%s, previous_digest=NULL, updated_at=now()
                WHERE instance_id=%s
                RETURNING *
                """,
                (target_digest, inst["instance_id"]),
            )
            audit(
                conn,
                p.name,
                "plugin.instance.rollback",
                f"{node_id}:{plugin_id}:{target_digest}",
            )
        return out(updated, pb.PluginInstance)

    @app.post("/admin/v1/nodes/{node_id}/plugins/{plugin_id}:start")
    def start_plugin(
        node_id: str,
        plugin_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            inst = one(
                conn,
                (
                    "SELECT * FROM console_plugin_instance "
                    "WHERE node_id=%s AND plugin_id=%s FOR UPDATE"
                ),
                (node_id, plugin_id),
            )
            if not inst:
                fail(404, "plugin_instance_not_found")
            intent_id = identifier("intent")
            conn.execute(
                """
                INSERT INTO console_deployment_intent(
                    id, node_id, instance_id, action, artifact_digest, config, state, created_by
                ) VALUES (%s, %s, %s, 'start', %s, %s, 'pending', %s)
                """,
                (
                    intent_id,
                    node_id,
                    inst["instance_id"],
                    inst["artifact_digest"],
                    Jsonb(inst["config"]),
                    p.name,
                ),
            )
            updated = one(
                conn,
                (
                    "UPDATE console_plugin_instance "
                    "SET desired_state='ready', updated_at=now() "
                    "WHERE instance_id=%s RETURNING *"
                ),
                (inst["instance_id"],),
            )
            audit(conn, p.name, "plugin.instance.start", f"{node_id}:{plugin_id}")
        return out(updated, pb.PluginInstance)

    @app.post("/admin/v1/nodes/{node_id}/plugins/{plugin_id}:stop")
    def stop_plugin(
        node_id: str,
        plugin_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            inst = one(
                conn,
                (
                    "SELECT * FROM console_plugin_instance "
                    "WHERE node_id=%s AND plugin_id=%s FOR UPDATE"
                ),
                (node_id, plugin_id),
            )
            if not inst:
                fail(404, "plugin_instance_not_found")
            intent_id = identifier("intent")
            conn.execute(
                """
                INSERT INTO console_deployment_intent(
                    id, node_id, instance_id, action, artifact_digest, config, state, created_by
                ) VALUES (%s, %s, %s, 'stop', %s, %s, 'pending', %s)
                """,
                (
                    intent_id,
                    node_id,
                    inst["instance_id"],
                    inst["artifact_digest"],
                    Jsonb(inst["config"]),
                    p.name,
                ),
            )
            updated = one(
                conn,
                (
                    "UPDATE console_plugin_instance "
                    "SET desired_state='stopped', actual_state='stopped', updated_at=now() "
                    "WHERE instance_id=%s RETURNING *"
                ),
                (inst["instance_id"],),
            )
            audit(conn, p.name, "plugin.instance.stop", f"{node_id}:{plugin_id}")
        return out(updated, pb.PluginInstance)

    @app.post("/admin/v1/nodes/{node_id}/plugins/{plugin_id}:uninstall")
    def uninstall_plugin(
        node_id: str,
        plugin_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            inst = one(
                conn,
                (
                    "SELECT * FROM console_plugin_instance "
                    "WHERE node_id=%s AND plugin_id=%s FOR UPDATE"
                ),
                (node_id, plugin_id),
            )
            if not inst:
                fail(404, "plugin_instance_not_found")
            intent_id = identifier("intent")
            conn.execute(
                """
                INSERT INTO console_deployment_intent(
                    id, node_id, instance_id, action, artifact_digest, config, state, created_by
                ) VALUES (%s, %s, %s, 'uninstall', %s, %s, 'pending', %s)
                """,
                (
                    intent_id,
                    node_id,
                    inst["instance_id"],
                    inst["artifact_digest"],
                    Jsonb(inst["config"]),
                    p.name,
                ),
            )
            updated = one(
                conn,
                (
                    "UPDATE console_plugin_instance "
                    "SET desired_state='uninstalled', actual_state='uninstalled', "
                    "updated_at=now() WHERE instance_id=%s RETURNING *"
                ),
                (inst["instance_id"],),
            )
            audit(conn, p.name, "plugin.instance.uninstall", f"{node_id}:{plugin_id}")
        return out(updated, pb.PluginInstance)

    # ── Agent 控制面通道：Enroll, Heartbeat, Report ───────────────────────

    @app.post("/v1/agent/enroll")
    def agent_enroll(body: Annotated[dict, Body()] = ...):
        req = parse(body, pb.EnrollNodeRequest)
        thash = hash_token(req.enrollment_token)

        with pool.connection() as conn:
            tok = one(
                conn,
                "SELECT * FROM console_node_enrollment_token WHERE token_hash=%s FOR UPDATE",
                (thash,),
            )
            if not tok:
                fail(401, "enrollment_token_invalid")
            if tok["used"]:
                fail(401, "enrollment_token_already_used")
            if tok["expires_at"] < datetime.now(UTC):
                fail(401, "enrollment_token_expired")
            if tok["node_id"] != req.node_id:
                fail(401, "enrollment_token_node_mismatch")

            conn.execute(
                "UPDATE console_node_enrollment_token SET used=true WHERE token_hash=%s",
                (thash,),
            )

            session_token = "sp_node_" + secrets.token_hex(32)
            shash = hash_token(session_token)

            caps = req.capabilities
            accels = (
                [MessageToDict(a, preserving_proto_field_name=True) for a in caps.accelerators]
                if caps and caps.accelerators
                else []
            )
            labels = dict(caps.labels) if caps and caps.labels else {}
            supported_arts = (
                list(caps.supported_artifacts)
                if caps and caps.supported_artifacts
                else ["local_native"]
            )

            conn.execute(
                """
                INSERT INTO console_node(
                    node_id, display_name, status, status_reason, platform, arch,
                    cpu_cores, memory_bytes, unified_memory_bytes, accelerators,
                    supported_artifacts, labels, is_co_located, session_token_hash,
                    last_heartbeat_at, enrolled_at, updated_at
                ) VALUES (
                    %s, %s, 'ready', '', %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    now(), now(), now()
                )
                ON CONFLICT (node_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    status='ready',
                    status_reason='',
                    platform=EXCLUDED.platform,
                    arch=EXCLUDED.arch,
                    cpu_cores=EXCLUDED.cpu_cores,
                    memory_bytes=EXCLUDED.memory_bytes,
                    unified_memory_bytes=EXCLUDED.unified_memory_bytes,
                    accelerators=EXCLUDED.accelerators,
                    supported_artifacts=EXCLUDED.supported_artifacts,
                    labels=EXCLUDED.labels,
                    is_co_located=EXCLUDED.is_co_located,
                    session_token_hash=EXCLUDED.session_token_hash,
                    last_heartbeat_at=now(),
                    updated_at=now()
                """,
                (
                    req.node_id,
                    req.display_name or req.node_id,
                    caps.platform if caps else "unknown",
                    caps.arch if caps else "unknown",
                    int(caps.cpu_cores) if caps else 1,
                    int(caps.memory_bytes) if caps else 0,
                    int(caps.unified_memory_bytes) if caps else 0,
                    Jsonb(accels),
                    supported_arts,
                    Jsonb(labels),
                    req.is_co_located,
                    shash,
                ),
            )
            audit(conn, req.node_id, "node.enroll.success", req.node_id)

        return out(
            {
                "success": True,
                "node_id": req.node_id,
                "status": "NODE_STATUS_READY",
                "session_token": session_token,
                "message": "Node successfully enrolled in cluster",
            },
            pb.EnrollNodeResponse,
        )

    @app.get("/v1/agent/task-intents/{intent_id}/manifest")
    def task_manifest(
        intent_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        """发放一个已分配 v2 Task 的受控执行清单。

        清单只含任务身份和不可变策略。媒体只能从相邻的受认证数据面端点读取，插件
        endpoint 只能从 Agent 的本机热部署台账读取，避免控制消息泄漏路径、端口或密钥。
        """
        with pool.connection() as conn:
            intent, _config, execution, task, assignment, revision, node, upload = (
                v2_intent_context(conn, intent_id, authorization, lock=True)
            )
            if intent["state"] != "dispatched":
                fail(409, "task_intent_not_active")
            if task["state"] == "assigned":
                conn.execute(
                    "UPDATE pipeline_task SET state='running',updated_at=now() WHERE task_id=%s",
                    (task["task_id"],),
                )
                task["state"] = "running"

            runtime_instance_id = ""
            if node["plugin_id"] != "org.sensoryplex.runtime.timeline-fusion":
                from ..infrastructure.runtime_bindings import pinned_runtime

                runtime = (
                    pinned_runtime(conn, intent["node_id"], node)
                    if node.get("release_id")
                    else one(
                        conn,
                        """
                    SELECT runtime.runtime_instance_id
                    FROM console_plugin_instance slot
                    JOIN plugin_runtime_instance runtime
                      ON runtime.runtime_instance_id=slot.active_runtime_instance_id
                    WHERE slot.node_id=%s AND slot.plugin_id=%s
                      AND slot.artifact_digest=%s AND slot.config_hash=%s
                      AND slot.actual_state='ready' AND runtime.node_id=%s
                      AND runtime.plugin_id=%s AND runtime.artifact_digest=%s
                      AND runtime.role='active' AND runtime.state='active'
                      AND runtime.endpoint <> ''
                    """,
                        (
                            intent["node_id"],
                            node["plugin_id"],
                            node["artifact_digest"],
                            node["config_hash"],
                            intent["node_id"],
                            node["plugin_id"],
                            node["artifact_digest"],
                        ),
                    )
                )
                if not runtime:
                    fail(409, "plugin_instance_unavailable")
                runtime_instance_id = runtime["runtime_instance_id"]
                if node.get("release_id") and not one(
                    conn,
                    "SELECT 1 FROM plugin_runtime_instance WHERE runtime_instance_id=%s AND "
                    "release_id=%s",
                    (runtime_instance_id, node["release_id"]),
                ):
                    fail(409, "plugin_pinned_release_unavailable")

            run = one(
                conn,
                "SELECT deadline_unix_ms FROM pipeline_run WHERE run_id=%s",
                (task["run_id"],),
            )
            timeline = next(
                (
                    item
                    for item in revision["definition_json"].get("nodes", [])
                    if item.get("id") == "timeline_fusion"
                ),
                {},
            )
            return {
                "intent_id": intent["id"],
                "execution_id": execution["execution_id"],
                "run": {
                    "run_id": task["run_id"],
                    "pipeline_id": execution["pipeline_id"],
                    "pipeline_revision": execution["pipeline_revision"],
                    "graph_digest": execution["graph_digest"],
                },
                "task": {
                    "task_id": task["task_id"],
                    "node_id": task["node_id"],
                    "attempt": task["attempt"],
                    "assignment_id": assignment["assignment_id"],
                    "deadline_unix_ms": run["deadline_unix_ms"],
                    "required": bool(task["required"]),
                    "state": task["state"],
                },
                "assignment": {
                    "node_id": assignment["actual_node_id"],
                    "data_plane_node_id": assignment["data_plane_node_id"],
                    "lease_expires_at_unix_ms": int(
                        assignment["lease_expires_at"].timestamp() * 1000
                    ),
                },
                "plugin": {
                    "plugin_id": node["plugin_id"],
                    "plugin_version": node["plugin_version"],
                    "artifact_digest": node["artifact_digest"],
                    "config_hash": node["config_hash"],
                    "runtime_instance_id": runtime_instance_id,
                    "consumes": node["consumes"],
                    "produces": node["produces"],
                    "deadline_ms": node["deadline_ms"],
                    "max_attempts": node["max_attempts"],
                    "release_id": node.get("release_id", ""),
                    "input_selector": node.get("input_selector", "media"),
                    "input_contracts": node.get("input_contracts", []),
                    "output_contracts": node.get("output_contracts", []),
                },
                "policy": timeline.get("execution_policy") or {},
                "asset": {
                    "asset_id": upload["id"],
                    "filename": upload["filename"],
                    "size_bytes": upload["size_bytes"],
                    "content_type": upload["content_type"],
                    "content_hash": upload["sha256"],
                },
            }

    @app.post("/v1/agent/tasks/{task_id}:output")
    def stage_plugin_output(
        task_id: str,
        body: Annotated[dict, Body()],
        authorization: Annotated[str | None, Header()] = None,
    ):
        from edge_material_sdk.validation import validate_observation

        intent_id = body.get("intent_id", "")
        try:
            output = orchestration_pb.PluginTaskOutput.FromString(
                base64.b64decode(body["output_b64"], validate=True)
            )
        except (ValueError, KeyError, TypeError):
            fail(422, "plugin_task_output_invalid")
        if len(output.observations) > 8192 or len(output.processing_receipts) > 8192:
            fail(413, "plugin_task_output_limit_exceeded")
        with pool.connection() as conn:
            _intent, _config, _execution, task, assignment, _revision, node, _upload = (
                v2_intent_context(conn, intent_id, authorization, lock=True)
            )
            if (
                output.task_id != task_id
                or task_id != task["task_id"]
                or output.assignment_id != assignment["assignment_id"]
                or output.attempt != task["attempt"]
            ):
                fail(403, "plugin_task_output_assignment_mismatch")
            if task["state"] not in {"assigned", "running"} or _intent["state"] != "dispatched":
                fail(409, "plugin_task_output_not_active")
            if not node.get("release_id"):
                fail(422, "plugin_task_output_v2_required")
            if not output.processing_receipts:
                if (
                    output.observations
                    or output.skipped_reason != "upstream_has_no_matching_observations"
                    or not node.get("input_selector", "").startswith("node:")
                ):
                    fail(422, "plugin_task_output_empty_without_reason")
                upstream = one(
                    conn,
                    "SELECT o.contract_bytes FROM pipeline_task t JOIN plugin_task_output o "
                    "USING(task_id) "
                    "WHERE t.run_id=%s AND t.node_id=%s AND t.state='succeeded'",
                    (task["run_id"], node["input_selector"][5:]),
                )
                if not upstream or any(
                    o.modality in node["consumes"]
                    for o in orchestration_pb.PluginTaskOutput.FromString(
                        upstream["contract_bytes"]
                    ).observations
                ):
                    fail(422, "plugin_task_output_skip_not_authorized")
            elif output.skipped_reason:
                fail(422, "plugin_task_output_ambiguous_skip")
            try:
                ids = set()
                for observation in output.observations:
                    validate_observation(observation)
                    materials._registered_observation(conn, observation)
                    if (
                        observation.provenance.processor_release_id != node["release_id"]
                        or observation.provenance.config_hash != node["config_hash"]
                        or observation.observation_id in ids
                    ):
                        raise ValueError("plugin_task_output_identity_mismatch")
                    ids.add(observation.observation_id)
                for receipt in output.processing_receipts:
                    validate_processor_reason(
                        receipt.reason_code,
                        required=receipt.outcome == runtime_pb.PROCESS_OUTCOME_NO_OBSERVATIONS,
                    )
                    if (
                        not receipt.input_id
                        or receipt.time_range.end_ms <= receipt.time_range.start_ms
                        or receipt.outcome
                        not in {
                            runtime_pb.PROCESS_OUTCOME_OBSERVED,
                            runtime_pb.PROCESS_OUTCOME_NO_OBSERVATIONS,
                        }
                    ):
                        raise ValueError("plugin_task_output_receipt_invalid")
                    if receipt.outcome == runtime_pb.PROCESS_OUTCOME_NO_OBSERVATIONS and (
                        receipt.observation_count or not receipt.reason_code
                    ):
                        raise ValueError("plugin_task_output_receipt_invalid")
                if sum(receipt.observation_count for receipt in output.processing_receipts) != len(
                    output.observations
                ):
                    raise ValueError("plugin_task_output_count_mismatch")
            except ValueError as error:
                fail(422, str(error))
            data = output.SerializeToString(deterministic=True)
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            saved = one(conn, "SELECT * FROM plugin_task_output WHERE task_id=%s", (task_id,))
            if saved:
                # 重派可沿用第一次输出；时钟字段与 assignment 变化不改变算法语义。
                previous = orchestration_pb.PluginTaskOutput.FromString(saved["contract_bytes"])
                current = orchestration_pb.PluginTaskOutput.FromString(data)
                for value in (previous, current):
                    value.ClearField("assignment_id")
                    value.ClearField("attempt")
                    for observation in value.observations:
                        observation.ClearField("created_at_unix_ms")
                if previous != current:
                    fail(409, "plugin_task_output_conflict")
                return {
                    "content_digest": saved["content_digest"],
                    "replayed": True,
                    "output_b64": base64.b64encode(saved["contract_bytes"]).decode(),
                }
            conn.execute(
                "INSERT INTO "
                "plugin_task_output(task_id,assignment_id,contract_bytes,content_digest) VALUES "
                "(%s,%s,%s,%s)",
                (task_id, assignment["assignment_id"], data, digest),
            )
            return {
                "content_digest": digest,
                "replayed": False,
                "output_b64": base64.b64encode(data).decode(),
            }

    @app.get("/v1/agent/task-intents/{intent_id}/upstream")
    def read_plugin_upstream(intent_id: str, authorization: Annotated[str | None, Header()] = None):
        with pool.connection() as conn:
            intent, _config, _execution, task, _assignment, revision, node, _upload = (
                v2_intent_context(conn, intent_id, authorization)
            )
            if intent["state"] != "dispatched" or task["state"] not in {"assigned", "running"}:
                fail(409, "plugin_task_input_not_active")
            upstream_id = node.get("input_selector", "").removeprefix("node:")
            upstream = one(
                conn,
                "SELECT t.task_id,o.contract_bytes FROM pipeline_task t JOIN plugin_task_output o"
                " USING(task_id) WHERE t.run_id=%s AND t.node_id=%s AND t.state='succeeded'",
                (task["run_id"], upstream_id),
            )
            if not upstream:
                fail(409, "plugin_task_upstream_unavailable")
            return {"output_b64": base64.b64encode(upstream["contract_bytes"]).decode()}

    @app.get("/v1/agent/task-intents/{intent_id}/asset")
    def task_asset(
        intent_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        """只向持有该 assignment 的节点返回原始媒体字节。"""
        with pool.connection() as conn:
            intent, _config, _execution, _task, _assignment, _revision, _node, upload = (
                v2_intent_context(conn, intent_id, authorization)
            )
            if intent["state"] != "dispatched":
                fail(409, "task_intent_not_active")
        active_storage = getattr(app.state, "storage", None) or storage
        local_path = active_storage.get_blob_path(upload["sha256"])
        if local_path is not None and local_path.is_file():
            return FileResponse(
                local_path,
                media_type=upload["content_type"],
                filename=upload["filename"],
                headers={"X-Content-SHA256": upload["sha256"]},
            )
        if not active_storage.exists(upload["sha256"]):
            fail(503, "blob_unavailable")
        presigned_url = active_storage.presign_get_url(
            key=upload["sha256"],
            expires_in_s=settings.s3_presigned_expire_s,
            filename=upload["filename"],
            content_type=upload["content_type"],
        )
        return RedirectResponse(
            url=presigned_url,
            status_code=307,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-SHA256": upload["sha256"],
            },
        )

    @app.post("/v1/agent/tasks/{task_id}:result")
    def agent_task_result(
        task_id: str,
        body: Annotated[dict, Body()] = ...,
        authorization: Annotated[str | None, Header()] = None,
    ):
        """节点侧结果的唯一写入口：先核 assignment，再落不可变回执和 Task 状态。"""
        intent_id = str(body.get("intent_id", ""))
        raw_receipt = body.get("receipt")
        if not intent_id:
            fail(422, "task_intent_id_required")
        if not isinstance(raw_receipt, dict):
            fail(422, "task_execution_receipt_invalid")
        with pool.connection() as conn:
            intent, config, execution, task, assignment, _revision, _node, _upload = (
                v2_intent_context(conn, intent_id, authorization, lock=True)
            )
            if task_id != config["task_id"] or task_id != task["task_id"]:
                fail(403, "agent_task_result_mismatch")
            if intent["state"] != "dispatched":
                fail(409, "task_intent_not_active")
            if task["node_id"] == "timeline_fusion" and bool(body.get("success", False)):
                # Timeline 成功不是一个空回执：覆盖层必须已经由专用写入口在同一 execution
                # 下落库，才能证明最后一个节点真的按 1 秒网格完成了事实登记。
                coverage = one(
                    conn,
                    "SELECT 1 FROM timeline_window_state WHERE execution_id=%s LIMIT 1",
                    (execution["execution_id"],),
                )
                if not coverage:
                    fail(409, "timeline_coverage_required_before_success")
            try:
                started_ms = int(raw_receipt["started_at_unix_ms"])
                completed_ms = int(raw_receipt["completed_at_unix_ms"])
                if started_ms <= 0 or completed_ms < started_ms:
                    raise ValueError
                receipt = {
                    "run_id": str(raw_receipt.get("run_id", task["run_id"])),
                    "task_id": str(raw_receipt.get("task_id", task_id)),
                    "attempt": int(raw_receipt.get("attempt", task["attempt"])),
                    "assignment_id": str(
                        raw_receipt.get("assignment_id", assignment["assignment_id"])
                    ),
                    "plugin_id": str(raw_receipt["plugin_id"]),
                    "artifact_digest": str(raw_receipt["artifact_digest"]),
                    "config_hash": str(raw_receipt["config_hash"]),
                    "input_count": int(raw_receipt["input_count"]),
                    "output_count": int(raw_receipt["output_count"]),
                    "result_manifest_ref": str(raw_receipt.get("result_manifest_ref", "")),
                    "reason_code": str(raw_receipt.get("reason_code", body.get("reason_code", ""))),
                    "receipt_digest": str(raw_receipt["receipt_digest"]),
                    "started_at": datetime.fromtimestamp(started_ms / 1000, tz=UTC),
                    "completed_at": datetime.fromtimestamp(completed_ms / 1000, tz=UTC),
                }
            except (KeyError, TypeError, ValueError, OverflowError):
                fail(422, "task_execution_receipt_invalid")
            result = orchestrator.report_task_result(
                conn,
                task_id=task_id,
                run_id=str(body.get("run_id", task["run_id"])),
                attempt=int(body.get("attempt", task["attempt"])),
                assignment_id=str(body.get("assignment_id", assignment["assignment_id"])),
                success=bool(body.get("success", False)),
                output_ref=str(body.get("output_ref", "")),
                retryable=bool(body.get("retryable", False)),
                reason_code=str(body.get("reason_code", "")),
                error_detail=str(body.get("error_detail", "")),
                receipt=receipt,
            )
            enqueued = enqueue_ready_v2_tasks(conn, intent["node_id"], actor="agent-result")
            audit(
                conn,
                intent["node_id"],
                "task.execution.result",
                f"{task_id}:{assignment['assignment_id']}",
            )
        return {
            "task": result["task"],
            "unlocked_task_ids": result.get("unlocked_task_ids", []),
            "enqueued_intents": enqueued,
            "retry_scheduled": result.get("retry_scheduled", False),
            "discarded": result.get("discarded", False),
        }

    @app.post("/v1/agent/tasks/{task_id}:timeline")
    def ingest_timeline(
        task_id: str,
        body: Annotated[dict, Body()] = ...,
        authorization: Annotated[str | None, Header()] = None,
    ):
        """受控写入 Timeline 融合出的派生事实与完整 1 秒覆盖层。

        这不是通用素材上传接口：它只接受已分配的 `timeline_fusion` Task，所有素材都必须
        是 protobuf，且源摘要、Runtime item 帐本、窗口网格和执行批次会在同一事务中复核。
        """
        intent_id = str(body.get("intent_id", ""))
        source_b64 = body.get("source_description_b64")
        raw_units = body.get("materials_b64")
        raw_items = body.get("timeline_items")
        raw_coverage = body.get("coverage")
        if (
            not intent_id
            or not isinstance(source_b64, str)
            or not isinstance(raw_units, list)
            or not isinstance(raw_items, list)
            or not isinstance(raw_coverage, list)
        ):
            fail(422, "timeline_ingest_input_invalid")
        if len(raw_units) > 1024 or len(raw_items) > 8192 or len(raw_coverage) > 7200:
            fail(413, "timeline_ingest_limit_exceeded")
        encoded_size = len(source_b64) + sum(
            len(value) for value in raw_units if isinstance(value, str)
        )
        if encoded_size > 8_000_000 or any(not isinstance(value, str) for value in raw_units):
            fail(413, "timeline_ingest_limit_exceeded")
        try:
            description = media_pb2.MediaSourceDescription()
            description.ParseFromString(base64.b64decode(source_b64, validate=True))
            units: list[material_pb2.MaterialUnit] = []
            for encoded in raw_units:
                unit = material_pb2.MaterialUnit()
                unit.ParseFromString(base64.b64decode(encoded, validate=True))
                units.append(unit)
        except (ValueError, TypeError):
            fail(422, "timeline_ingest_protobuf_invalid")

        items: dict[str, tuple[str, int, int]] = {}
        try:
            for entry in raw_items:
                if not isinstance(entry, dict):
                    raise ValueError
                item_id = str(entry["item_id"])
                kind = str(entry["kind"])
                start_ms, end_ms = int(entry["start_ms"]), int(entry["end_ms"])
                if (
                    not item_id
                    or len(item_id) > 256
                    or kind not in {"video_frame", "audio_segment"}
                    or start_ms < 0
                    or end_ms <= start_ms
                    or item_id in items
                ):
                    raise ValueError
                items[item_id] = (kind, start_ms, end_ms)
        except (KeyError, TypeError, ValueError, OverflowError):
            fail(422, "timeline_ingest_items_invalid")

        allowed_sampling = {
            "sampled",
            "not_sampled_by_policy",
            "not_applicable",
            "queued",
            "running",
            "failed",
            # 语义覆盖：逐帧判别过但这一段不需要重新送模型；以及计划刷新了却没有观测。
            "covered_without_model_refresh",
            "semantic_refresh_without_observation",
        }
        allowed_modality = {
            "queued",
            "running",
            "observed",
            "not_applicable",
            "failed",
            "not_sampled_by_policy",
            "not_scheduled",
            "covered_without_model_refresh",
            "not_observed",
        }
        try:
            coverage: list[tuple[int, int, str, dict, dict]] = []
            for entry in raw_coverage:
                if not isinstance(entry, dict):
                    raise ValueError
                start_ms, end_ms = int(entry["start_ms"]), int(entry["end_ms"])
                sampling = str(entry["sampling_state"])
                modalities = entry["modality_states"]
                reasons = entry.get("reason_codes", {})
                if (
                    start_ms < 0
                    or start_ms % 1000
                    or end_ms <= start_ms
                    or end_ms > start_ms + 1000
                    or sampling not in allowed_sampling
                    or not isinstance(modalities, dict)
                    or not isinstance(reasons, dict)
                    or any(value not in allowed_modality for value in modalities.values())
                    or any(len(str(value)) > 160 for value in reasons.values())
                ):
                    raise ValueError
                coverage.append((start_ms, end_ms, sampling, modalities, reasons))
            if coverage != sorted(coverage, key=lambda item: item[:2]):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            fail(422, "timeline_ingest_coverage_invalid")

        with pool.connection() as conn:
            intent, config, execution, task, assignment, revision, node, upload = v2_intent_context(
                conn, intent_id, authorization, lock=True
            )
            if task_id != task["task_id"] or task["node_id"] != "timeline_fusion":
                fail(403, "timeline_ingest_task_mismatch")
            if node["plugin_id"] != "org.sensoryplex.runtime.timeline-fusion":
                fail(409, "timeline_ingest_plugin_mismatch")
            if intent["state"] != "dispatched" or task["state"] not in {"assigned", "running"}:
                fail(409, "timeline_ingest_task_not_active")
            if description.source.content_hash != config["content_hash"]:
                fail(422, "timeline_ingest_content_hash_mismatch")
            expected = []
            duration = int(description.duration_ms)
            for start_ms in range(0, duration, 1000):
                expected.append((start_ms, min(start_ms + 1000, duration)))
            if [(item[0], item[1]) for item in coverage] != expected:
                fail(422, "timeline_coverage_grid_invalid")
            try:
                references = timeline_ingest.register_references(
                    conn,
                    description=description,
                    owner=one(
                        conn,
                        "SELECT owner FROM console_job_draft WHERE id=%s",
                        (execution["job_id"],),
                    )["owner"],
                    units=units,
                    timeline_items=items,
                    upload_id=upload["id"],
                )
                appended, replayed = 0, 0
                for unit in units:
                    previous = one(
                        conn,
                        """
                        SELECT revision,content_hash FROM material_unit
                        WHERE material_unit_id=%s ORDER BY revision DESC LIMIT 1
                        """,
                        (unit.material_unit_id,),
                    )
                    if previous:
                        digest = (
                            "sha256:"
                            + hashlib.sha256(unit.SerializeToString(deterministic=True)).hexdigest()
                        )
                        unit.revision = (
                            previous["revision"]
                            if digest == previous["content_hash"]
                            else previous["revision"] + 1
                        )
                    inserted = materials.append_material(
                        conn,
                        unit,
                        trace_id=f"execution:{execution['execution_id']}",
                        execution_id=execution["execution_id"],
                    )
                    appended += int(inserted)
                    replayed += int(not inserted)
                for start_ms, end_ms, sampling, modalities, reasons in coverage:
                    next_revision = conn.execute(
                        """
                        SELECT coalesce(max(state_revision),0)+1 FROM timeline_window_state
                        WHERE execution_id=%s AND stream_id=%s AND start_ms=%s AND end_ms=%s
                        """,
                        (execution["execution_id"], description.source.stream_id, start_ms, end_ms),
                    ).fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO timeline_window_state(
                            execution_id,stream_id,start_ms,end_ms,state_revision,sampling_state,
                            modality_states,reason_codes
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            execution["execution_id"],
                            description.source.stream_id,
                            start_ms,
                            end_ms,
                            next_revision,
                            sampling,
                            Jsonb(modalities),
                            Jsonb(reasons),
                        ),
                    )
                # OCR/ASR 与完整覆盖层已经作为快路径事实落库；在同一事务内仅创建 VLM
                # 时间锚点任务和 outbox，不连 NATS、更不解压任何帧。提交之后用户即可审阅，
                # 发布器/Consumer 的可用性不再阻塞 L1。
                from ..infrastructure import enrichments, second_windows

                delayed = node.get("delayed_enrichments") or []
                generic_delayed = [entry for entry in delayed if entry.get("release_id")]
                pending = sorted(
                    {modality for entry in generic_delayed for modality in entry["produces"]}
                )
                if any(not entry.get("release_id") for entry in delayed):
                    pending.append(vlm_delayed.VLM_MODALITY)

                units = second_windows.ensure_materials(
                    conn,
                    execution_id=execution["execution_id"],
                    asset_id=f"asset-{description.source.content_hash[7:19]}",
                    pending=pending,
                    empty_status="no_observations"
                    if node.get("execution_policy", {}).get("generic_plugin_graph")
                    else "failed",
                )
                generic_task_ids = enrichments.enqueue(
                    conn,
                    execution=execution,
                    revision=revision,
                    description=description,
                    units=units,
                )
                vlm_task_ids = vlm_delayed.enqueue_vlm_tasks(
                    conn,
                    execution=execution,
                    revision=revision,
                    description=description,
                    items=items,
                    units=units,
                )
                conn.execute(
                    """
                    UPDATE console_job_execution
                    SET state='ready_for_review',modality_summary=modality_summary || %s::jsonb,
                        completed_at=NULL
                    WHERE execution_id=%s
                    """,
                    (
                        Jsonb(
                            {
                                "fast_path": "ready_for_review",
                                "enrichments": {
                                    "queued": len(generic_task_ids),
                                    "mode": "jetstream_workqueue",
                                },
                                "vlm_enrichment": {
                                    "queued": len(vlm_task_ids),
                                    "mode": "jetstream_workqueue",
                                },
                            }
                        ),
                        execution["execution_id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE console_job_draft
                    SET state='ready_for_review',error_code=NULL,error_detail=NULL,completed_at=NULL
                    WHERE id=%s
                    """,
                    (execution["job_id"],),
                )
            except (
                timeline_ingest.TimelineIngestError,
                materials.RevisionConflict,
                vlm_delayed.VlmDelayedError,
                ValueError,
            ) as error:
                fail(422, str(error).split(":", 1)[0])
            audit(
                conn,
                intent["node_id"],
                "timeline.ingested",
                f"{execution['execution_id']}:{task_id}",
            )
        return {
            "execution_id": execution["execution_id"],
            "references": references,
            "materials": len(units),
            "appended": appended,
            "replayed": replayed,
            "coverage_windows": len(coverage),
            "vlm_tasks_enqueued": len(vlm_task_ids),
            "enrichment_tasks_enqueued": len(generic_task_ids),
        }

    @app.post("/v1/agent/heartbeat")
    def agent_heartbeat(
        body: Annotated[dict, Body()] = ...,
        authorization: Annotated[str | None, Header()] = None,
    ):
        req = parse(body, pb.NodeHeartbeatRequest)
        token = req.session_token
        if not token and authorization and authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]
        if not token:
            fail(401, "missing_node_session_token")
        shash = hash_token(token)

        with pool.connection() as conn:
            nr = one(
                conn,
                "SELECT * FROM console_node WHERE node_id=%s FOR UPDATE",
                (req.node_id,),
            )
            if not nr:
                fail(404, "node_not_found")
            if nr["session_token_hash"] != shash:
                fail(401, "invalid_node_credentials")
            if nr["status"] == "revoked":
                return out(
                    {
                        "status": "NODE_STATUS_REVOKED",
                        "heartbeat_interval_ms": 0,
                        "pending_intents": [],
                    },
                    pb.NodeHeartbeatResponse,
                )

            new_status = nr["status"]
            if new_status == "offline":
                new_status = "ready"

            conn.execute(
                (
                    "UPDATE console_node SET last_heartbeat_at=now(), status=%s, "
                    "status_reason='', updated_at=now() WHERE node_id=%s"
                ),
                (new_status, req.node_id),
            )

            enqueue_ready_v2_tasks(conn, req.node_id, actor="heartbeat")

            pending_rows = rows(
                conn,
                """
                SELECT * FROM console_deployment_intent
                WHERE node_id=%s AND state='pending'
                ORDER BY created_at ASC
                FOR UPDATE
                """,
                (req.node_id,),
            )
            pending_intents = []
            for pr in pending_rows:
                conn.execute(
                    (
                        "UPDATE console_deployment_intent "
                        "SET state='dispatched', dispatched_at=now() WHERE id=%s"
                    ),
                    (pr["id"],),
                )
                inst = one(
                    conn,
                    (
                        "SELECT plugin_id, plugin_version, config_hash "
                        "FROM console_plugin_instance "
                        "WHERE instance_id=%s"
                    ),
                    (pr["instance_id"],),
                )
                # 蓝绿/回滚的具体实例可能与当前逻辑槽位版本和配置不同。
                # 下发候选或旧实例的身份，避免把槽位最新摘要写入另一进程的注册表。
                if pr["runtime_instance_id"]:
                    inst = one(
                        conn,
                        "SELECT i.plugin_id,r.plugin_version,i.config_hash "
                        "FROM plugin_runtime_instance i JOIN plugin_release r USING(release_id) "
                        "WHERE i.runtime_instance_id=%s AND i.instance_id=%s",
                        (pr["runtime_instance_id"], pr["instance_id"]),
                    )
                # 热部署意图（有 operation_id）必须携带 operation/generation/release/bundle
                # 身份与截止时间；ADR-026 的历史意图走同一条通道但字段为空。
                pending_intents.append(
                    intent_proto(
                        pr,
                        inst["plugin_id"] if inst else "",
                        inst["plugin_version"] if inst else "",
                        inst["config_hash"] if inst else "",
                    )
                )

            # Agent 用自己的平台服务状态对账；未知状态只报 reconciliation_required。
            observations = record_runtime_observations(conn, req.node_id, req.runtime_observations)
            reconciliation_required = [
                item["runtime_instance_id"]
                for item in observations
                if item["reconciliation"] == "reconciliation_required"
            ]
            proto_status = to_proto_node_status(new_status)

        return out(
            {
                "status": proto_status,
                "heartbeat_interval_ms": 5000,
                "pending_intents": pending_intents,
                "reconciliation_required": reconciliation_required,
            },
            pb.NodeHeartbeatResponse,
        )

    @app.post("/v1/agent/report")
    def agent_report(
        body: Annotated[dict, Body()] = ...,
        authorization: Annotated[str | None, Header()] = None,
    ):
        req = parse(body, pb.ReportDeploymentRequest)

        with pool.connection() as conn:
            intent = one(
                conn,
                "SELECT * FROM console_deployment_intent WHERE id=%s FOR UPDATE",
                (req.intent_id,),
            )
            if not intent:
                fail(404, "deployment_intent_not_found")

            if intent["operation_id"]:
                # 热部署回报必须来自**该节点自己的**认证会话；否则任何人都能拿
                # 一个 intent_id 去推进别人的蓝绿切换。
                token = ""
                if authorization and authorization.startswith("Bearer "):
                    token = authorization.split(" ", 1)[1]
                if not token:
                    fail(401, "missing_node_session_token")
                node = one(
                    conn,
                    "SELECT * FROM console_node WHERE session_token_hash=%s",
                    (hash_token(token),),
                )
                if not node:
                    fail(401, "invalid_node_credentials")
                if node["node_id"] != intent["node_id"]:
                    fail(403, "deployment_report_node_mismatch")
                return apply_hot_report(conn, node["node_id"], req, intent)

            is_task_process = intent["action"] == "task_process"
            task_config = intent.get("config") or {}
            is_v2_task = is_task_process and task_config.get("execution_mode") == "orchestrated_v2"
            if is_v2_task:
                # v2 任务是否“执行完毕”只取决于专用 result 入口已验真的 assignment 回执，
                # 不取决于 Agent 在 ReportDeployment 里填了 success。业务失败也可以有合法
                # 回执，此时意图已经交付完成，Job 终态由编排器投影。
                authenticated_node(conn, authorization, intent["node_id"])
                receipt = one(
                    conn,
                    """
                    SELECT receipt_digest FROM task_execution_receipt
                    WHERE task_id=%s AND attempt=%s AND assignment_id=%s
                    """,
                    (
                        task_config.get("task_id", ""),
                        int(task_config.get("attempt") or 0),
                        task_config.get("assignment_id", ""),
                    ),
                )
                if receipt:
                    conn.execute(
                        """
                        UPDATE console_deployment_intent
                        SET state='completed',error_code=NULL,error_detail=NULL,completed_at=now()
                        WHERE id=%s AND state='dispatched'
                        """,
                        (intent["id"],),
                    )
                    audit(
                        conn,
                        intent["node_id"],
                        "job.task.intent.completed",
                        f"{intent['id']}:{receipt['receipt_digest']}",
                    )
                    return {"status": "recorded"}

                # Agent 无法把任何回执提交到 result API 时，不能让 v2 意图永久停留在
                # dispatched。保持与 legacy 一样的显式失败，但绝不把它伪装为 Task 成功。
                task_error_code = req.error_code or "task_execution_receipt_required"
                task_error_detail = req.error_detail or "task execution receipt was not recorded"
                conn.execute(
                    """
                    UPDATE console_deployment_intent
                    SET state='failed',error_code=%s,error_detail=%s,completed_at=now()
                    WHERE id=%s
                    """,
                    (task_error_code, task_error_detail, intent["id"]),
                )
                conn.execute(
                    """
                    UPDATE console_job_draft
                    SET state='failed',error_code=%s,error_detail=%s,completed_at=now()
                    WHERE id=%s AND state='processing'
                    """,
                    (task_error_code, task_error_detail, intent["job_id"]),
                )
                audit(
                    conn,
                    intent["node_id"],
                    "job.task.intent.failed_without_receipt",
                    f"{intent['id']}:{task_error_code}",
                )
                return {"status": "recorded"}

            # legacy 任务没有 Runtime 回执协议。即使某个 Agent 错报 success，也不能在没有
            # 可核验执行事实时把业务任务写成完成；该行为保留给历史兼容任务。
            task_error_code = req.error_code or "runtime_task_service_not_attached"
            task_error_detail = req.error_detail or "runtime_task_service_not_attached"
            if is_task_process and req.success:
                task_error_code = "task_execution_receipt_required"
                task_error_detail = "task execution success requires a verified runtime receipt"

            intent_success = req.success and not is_task_process
            intent_state = "completed" if intent_success else "failed"
            conn.execute(
                """
                UPDATE console_deployment_intent
                SET state=%s, error_code=%s, error_detail=%s, completed_at=now()
                WHERE id=%s
                """,
                (
                    intent_state,
                    None
                    if intent_success
                    else (task_error_code if is_task_process else req.error_code or None),
                    None
                    if intent_success
                    else (task_error_detail if is_task_process else req.error_detail or None),
                    req.intent_id,
                ),
            )

            if is_task_process:
                conn.execute(
                    """
                    UPDATE console_job_draft
                    SET state='failed', error_code=%s, error_detail=%s, completed_at=now()
                    WHERE id=%s AND state='processing'
                    """,
                    (task_error_code, task_error_detail, intent["job_id"]),
                )
                audit(
                    conn,
                    req.node_id,
                    "job.task.failed",
                    f"{req.node_id}:{intent['job_id']}:{task_error_code}",
                )
                return {"status": "recorded"}

            actual = req.actual_state.lower().replace("plugin_instance_state_", "")
            if not actual:
                actual = "ready" if req.success else "failed"

            conn.execute(
                """
                UPDATE console_plugin_instance
                SET actual_state=%s, error_code=%s, error_detail=%s, updated_at=now()
                WHERE instance_id=%s
                """,
                (actual, req.error_code or None, req.error_detail or None, req.instance_id),
            )

            action = "plugin.instance.ready" if intent_success else "plugin.instance.failed"
            audit(
                conn,
                req.node_id,
                action,
                f"{req.node_id}:{req.instance_id}:{req.action}:{intent_state}",
            )

        return {"status": "recorded"}

    @app.post("/v1/agent/deregister")
    def agent_deregister(
        body: Annotated[dict, Body()] = ...,
        authorization: Annotated[str | None, Header()] = None,
    ):
        """子节点主动反注册并下线：撤销会话令牌，标记为 revoked，并清理所有插件实例。"""
        node_id = body.get("node_id", "")
        token = body.get("session_token", "")
        if not token and authorization:
            token = authorization.replace("Bearer ", "").strip()
        if not node_id or not token:
            fail(401, "invalid_credentials")
        thash = hash_token(token)
        with pool.connection() as conn:
            node = one(
                conn,
                "SELECT * FROM console_node WHERE node_id=%s AND session_token_hash=%s",
                (node_id, thash),
            )
            if not node:
                fail(401, "node_not_found_or_invalid_token")
            conn.execute(
                """
                UPDATE console_node
                SET status='revoked', status_reason='agent_deregistered',
                    session_token_hash=NULL, updated_at=now()
                WHERE node_id=%s
                """,
                (node_id,),
            )
            conn.execute(
                """
                UPDATE console_plugin_instance
                SET desired_state='uninstalled', actual_state='uninstalled', updated_at=now()
                WHERE node_id=%s
                """,
                (node_id,),
            )
            audit(conn, f"agent:{node_id}", "node.agent.deregister", node_id)
        return {
            "node_id": node_id,
            "status": "NODE_STATUS_REVOKED",
            "message": "Node successfully deregistered",
        }

    register_install_endpoints(app, pool, settings)
    register_lifecycle_convenience_endpoints(app, pool, auth, settings)


# ── 一键式安装脚本与自纳管分发 ─────────────────────────────────────────────


def register_install_endpoints(app, pool, settings):
    from fastapi import Request
    from fastapi.responses import PlainTextResponse

    @app.get("/v1/agent/install.sh", response_class=PlainTextResponse)
    def download_install_script():
        script_path = ROOT / "tools/install_agent.sh"
        if not script_path.is_file():
            fail(404, "install_script_not_found")
        return PlainTextResponse(script_path.read_text(), media_type="text/x-shellscript")

    @app.get("/v1/agent/node_agent.py", response_class=PlainTextResponse)
    def download_agent_script():
        agent_path = ROOT / "tools/node_agent.py"
        if not agent_path.is_file():
            fail(404, "agent_script_not_found")
        return PlainTextResponse(agent_path.read_text(), media_type="text/x-python")

    @app.post("/v1/agent/bootstrap-local")
    def bootstrap_local_node(
        request: Request,
        body: Annotated[dict, Body()] = ...,
    ):
        """为同机数据面节点提供零摩擦自注册（仅限同机回环网络或携带本地凭据）。"""
        client_host = request.client.host if request.client else ""
        auth_header = request.headers.get("authorization", "")
        token = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else ""
        is_local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
        has_token = settings.api_token and secrets.compare_digest(
            token, settings.api_token.get_secret_value()
        )

        if not (is_local or has_token):
            fail(403, "local_bootstrap_forbidden_from_remote")

        req = parse(body, pb.EnrollNodeRequest)
        session_token = "sp_node_" + secrets.token_hex(32)
        shash = hash_token(session_token)

        caps = req.capabilities
        accels = (
            [MessageToDict(a, preserving_proto_field_name=True) for a in caps.accelerators]
            if caps and caps.accelerators
            else []
        )
        labels = dict(caps.labels) if caps and caps.labels else {}
        labels["auto_bootstrap"] = "true"
        supported_arts = (
            list(caps.supported_artifacts)
            if caps and caps.supported_artifacts
            else ["local_native", "container"]
        )

        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO console_node(
                    node_id, display_name, status, status_reason, platform, arch,
                    cpu_cores, memory_bytes, unified_memory_bytes, accelerators,
                    supported_artifacts, labels, is_co_located, session_token_hash,
                    last_heartbeat_at, enrolled_at, updated_at
                ) VALUES (
                    %s, %s, 'ready', '', %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, true, %s,
                    now(), now(), now()
                )
                ON CONFLICT (node_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    status='ready',
                    status_reason='',
                    platform=EXCLUDED.platform,
                    arch=EXCLUDED.arch,
                    cpu_cores=EXCLUDED.cpu_cores,
                    memory_bytes=EXCLUDED.memory_bytes,
                    unified_memory_bytes=EXCLUDED.unified_memory_bytes,
                    accelerators=EXCLUDED.accelerators,
                    supported_artifacts=EXCLUDED.supported_artifacts,
                    labels=EXCLUDED.labels,
                    is_co_located=true,
                    session_token_hash=EXCLUDED.session_token_hash,
                    last_heartbeat_at=now(),
                    updated_at=now()
                """,
                (
                    req.node_id,
                    req.display_name or "Local Host (Co-located)",
                    caps.platform if caps else "unknown",
                    caps.arch if caps else "unknown",
                    int(caps.cpu_cores) if caps else 1,
                    int(caps.memory_bytes) if caps else 0,
                    int(caps.unified_memory_bytes) if caps else 0,
                    Jsonb(accels),
                    supported_arts,
                    Jsonb(labels),
                    shash,
                ),
            )
            audit(conn, req.node_id, "node.bootstrap_local.success", req.node_id)

        return out(
            {
                "success": True,
                "node_id": req.node_id,
                "status": "NODE_STATUS_READY",
                "session_token": session_token,
                "message": "Local node auto-bootstrapped successfully",
            },
            pb.EnrollNodeResponse,
        )


# ── 候选节点接纳、下线节点清理与批量装配流水线 ──────────────────────────────


def register_lifecycle_convenience_endpoints(app, pool, auth, settings):
    @app.post("/admin/v1/nodes/{node_id}:accept")
    def accept_node(
        node_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """管理员一键接纳候选子节点进入集群。"""
        with pool.connection() as conn:
            nr = one(
                conn,
                "SELECT * FROM console_node WHERE node_id=%s FOR UPDATE",
                (node_id,),
            )
            if not nr:
                fail(404, "node_not_found")
            if nr["status"] not in {"candidate", "enrolling", "offline"}:
                fail(409, "node_not_in_candidate_state")

            conn.execute(
                (
                    "UPDATE console_node SET status='ready', status_reason='', "
                    "updated_at=now() WHERE node_id=%s"
                ),
                (node_id,),
            )
            audit(conn, p.name, "node.accept", node_id)
        return get_node_by_id(pool, node_id)

    @app.post("/admin/v1/nodes/{node_id}:reject")
    def reject_node(
        node_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """管理员拒绝候选节点。"""
        with pool.connection() as conn:
            nr = one(
                conn,
                "SELECT * FROM console_node WHERE node_id=%s FOR UPDATE",
                (node_id,),
            )
            if not nr:
                fail(404, "node_not_found")
            conn.execute(
                (
                    "UPDATE console_node SET status='revoked', "
                    "status_reason='Rejected by administrator', "
                    "session_token_hash=NULL, updated_at=now() WHERE node_id=%s"
                ),
                (node_id,),
            )
            audit(conn, p.name, "node.reject", node_id)
        return get_node_by_id(pool, node_id)

    @app.delete("/admin/v1/nodes/{node_id}")
    def delete_node(
        node_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """清理已下线、已撤销或被拒绝的节点。"""
        with pool.connection() as conn:
            nr = one(
                conn,
                "SELECT * FROM console_node WHERE node_id=%s FOR UPDATE",
                (node_id,),
            )
            if not nr:
                fail(404, "node_not_found")
            if nr["status"] == "ready":
                fail(409, "cannot_delete_ready_node")

            purge_node_deployment_rows(conn, node_id)
            conn.execute(
                "DELETE FROM console_node_enrollment_token WHERE node_id=%s",
                (node_id,),
            )
            conn.execute("DELETE FROM console_node WHERE node_id=%s", (node_id,))
            audit(conn, p.name, "node.delete", node_id)
        return {"status": "deleted", "node_id": node_id}

    @app.post("/admin/v1/nodes:purge-stale")
    def purge_stale_nodes(
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """一键清理所有离线或撤销的非同机测试节点。"""
        with pool.connection() as conn:
            stale_nodes = rows(
                conn,
                """
                SELECT node_id FROM console_node
                WHERE (status IN ('offline', 'revoked')) AND node_id != 'local-host'
                """,
            )
            purged = []
            for sn in stale_nodes:
                nid = sn["node_id"]
                purge_node_deployment_rows(conn, nid)
                conn.execute(
                    "DELETE FROM console_node_enrollment_token WHERE node_id=%s",
                    (nid,),
                )
                conn.execute("DELETE FROM console_node WHERE node_id=%s", (nid,))
                purged.append(nid)
            audit(conn, p.name, "node.purge_stale", f"count={len(purged)}")
        return {"purged": purged, "total": len(purged)}

    @app.post("/admin/v1/nodes/{node_id}:prune")
    def prune_node_installations(
        node_id: str,
        body: Annotated[dict | None, Body()] = None,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """物理或预检清理算力节点上的旧版本插件、废弃 runtime 与临时缓存。"""
        with pool.connection() as conn:
            nr = one(
                conn,
                "SELECT * FROM console_node WHERE node_id=%s FOR SHARE",
                (node_id,),
            )
            if not nr:
                fail(404, "node_not_found")
            if not nr["is_co_located"] and node_id != "local-host":
                fail(400, "remote_node_prune_unsupported")

            payload = body or {}
            keep = int(payload.get("keep", 1))
            include_tasks = bool(payload.get("include_tasks", False))
            include_bundles = bool(payload.get("include_bundles", False))
            dry_run = bool(payload.get("dry_run", False))

            from tools.prune_installations import DEFAULT_AGENT_BASE, format_bytes, prune_all

            freed_bytes, actions = prune_all(
                base_dir=DEFAULT_AGENT_BASE,
                keep_releases=keep,
                include_tasks=include_tasks,
                include_bundles=include_bundles,
                dry_run=dry_run,
            )

            audit_action = "node.prune_dry_run" if dry_run else "node.prune"
            audit(
                conn,
                p.name,
                audit_action,
                f"{node_id}:keep={keep}:freed={freed_bytes}:count={len(actions)}",
            )

            return {
                "success": True,
                "node_id": node_id,
                "dry_run": dry_run,
                "keep": keep,
                "include_tasks": include_tasks,
                "include_bundles": include_bundles,
                "freed_bytes": freed_bytes,
                "freed_human": format_bytes(freed_bytes),
                "actions": actions,
                "action_count": len(actions),
            }

    @app.post("/v1/agent/candidate-register")
    def candidate_register(body: Annotated[dict, Body()] = ...):
        """子节点零配置自报到，初始进入待接纳状态（等待管理员在网页一键批准）。"""
        req = parse(body, pb.EnrollNodeRequest)
        session_token = "sp_cand_" + secrets.token_hex(24)
        shash = hash_token(session_token)

        caps = req.capabilities
        accels = (
            [MessageToDict(a, preserving_proto_field_name=True) for a in caps.accelerators]
            if caps and caps.accelerators
            else []
        )
        labels = dict(caps.labels) if caps and caps.labels else {}
        labels["candidate"] = "true"
        supported_arts = (
            list(caps.supported_artifacts)
            if caps and caps.supported_artifacts
            else ["local_native", "container"]
        )

        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO console_node(
                    node_id, display_name, status, status_reason, platform, arch,
                    cpu_cores, memory_bytes, unified_memory_bytes, accelerators,
                    supported_artifacts, labels, is_co_located, session_token_hash,
                    last_heartbeat_at, enrolled_at, updated_at
                ) VALUES (
                    %s, %s, 'candidate', 'Waiting for admin approval', %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    now(), now(), now()
                )
                ON CONFLICT (node_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    platform=EXCLUDED.platform,
                    arch=EXCLUDED.arch,
                    cpu_cores=EXCLUDED.cpu_cores,
                    memory_bytes=EXCLUDED.memory_bytes,
                    unified_memory_bytes=EXCLUDED.unified_memory_bytes,
                    accelerators=EXCLUDED.accelerators,
                    supported_artifacts=EXCLUDED.supported_artifacts,
                    labels=EXCLUDED.labels,
                    session_token_hash=EXCLUDED.session_token_hash,
                    last_heartbeat_at=now(),
                    updated_at=now()
                """,
                (
                    req.node_id,
                    req.display_name or f"Candidate ({req.node_id})",
                    caps.platform if caps else "unknown",
                    caps.arch if caps else "unknown",
                    int(caps.cpu_cores) if caps else 1,
                    int(caps.memory_bytes) if caps else 0,
                    int(caps.unified_memory_bytes) if caps else 0,
                    Jsonb(accels),
                    supported_arts,
                    Jsonb(labels),
                    req.is_co_located,
                    shash,
                ),
            )
            audit(conn, req.node_id, "node.candidate.registered", req.node_id)

        return out(
            {
                "success": True,
                "node_id": req.node_id,
                "status": "NODE_STATUS_CANDIDATE",
                "session_token": session_token,
                "message": "Node registered as candidate, pending administrator approval",
            },
            pb.EnrollNodeResponse,
        )


def get_node_by_id(pool, node_id: str):
    with pool.connection() as conn:
        nr = one(conn, "SELECT * FROM console_node WHERE node_id=%s", (node_id,))
        if not nr:
            fail(404, "node_not_found")
        inst_rows = rows(
            conn,
            ("SELECT * FROM console_plugin_instance WHERE node_id=%s ORDER BY created_at DESC"),
            (node_id,),
        )
        item = dict(nr)
        item["status"] = to_proto_node_status(nr["status"])
        item["capabilities"] = {
            "platform": nr["platform"],
            "arch": nr["arch"],
            "cpu_cores": nr["cpu_cores"],
            "memory_bytes": nr["memory_bytes"],
            "unified_memory_bytes": nr["unified_memory_bytes"],
            "accelerators": nr["accelerators"],
            "supported_artifacts": nr["supported_artifacts"],
            "labels": nr["labels"],
        }
        item["instances"] = inst_rows
    return out(item, pb.NodeInfo)

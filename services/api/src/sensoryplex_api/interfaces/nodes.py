"""ADR-026 Web 主节点与局域网插件 worker 拓扑 API 接口。

包含管理侧节点 Registry、预检、部署意图下发、回滚，以及 Agent 注册与心跳通道。
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from edge_material_sdk.generated.node.v1 import node_pb2 as pb
from fastapi import Body, Depends, Header, Query
from google.protobuf.json_format import MessageToDict
from psycopg.types.json import Jsonb

from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field
from ..infrastructure.catalog import plugin
from ..infrastructure.preflight import check_preflight


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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


def register(app, pool, auth, settings):
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
        p_entry = plugin(settings, req.plugin_id)
        with pool.connection() as conn:
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
        p_entry = plugin(settings, plugin_id)
        config_id = body.get("config_id")
        config = body.get("config", {})

        with pool.connection() as conn:
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
            config_hash = "sha256:" + hashlib.sha256(str(config).encode()).hexdigest()

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
            if pending_rows:
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
                            "SELECT plugin_id, plugin_version FROM console_plugin_instance "
                            "WHERE instance_id=%s"
                        ),
                        (pr["instance_id"],),
                    )
                    action_enum = {
                        "install": "DEPLOYMENT_ACTION_INSTALL",
                        "start": "DEPLOYMENT_ACTION_START",
                        "stop": "DEPLOYMENT_ACTION_STOP",
                        "uninstall": "DEPLOYMENT_ACTION_UNINSTALL",
                        "rollback": "DEPLOYMENT_ACTION_ROLLBACK",
                        "drain": "DEPLOYMENT_ACTION_DRAIN",
                    }.get(pr["action"], "DEPLOYMENT_ACTION_UNSPECIFIED")

                    pending_intents.append(
                        {
                            "intent_id": pr["id"],
                            "instance_id": pr["instance_id"],
                            "node_id": pr["node_id"],
                            "plugin_id": inst["plugin_id"] if inst else "",
                            "plugin_version": inst["plugin_version"] if inst else "",
                            "action": action_enum,
                            "artifact_digest": pr["artifact_digest"],
                            "rollback_digest": pr["rollback_digest"] or "",
                            "config": pr["config"],
                            "created_at": pr["created_at"].isoformat() if pr["created_at"] else "",
                        }
                    )

            proto_status = to_proto_node_status(new_status)

        return out(
            {
                "status": proto_status,
                "heartbeat_interval_ms": 5000,
                "pending_intents": pending_intents,
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

            intent_state = "completed" if req.success else "failed"
            conn.execute(
                """
                UPDATE console_deployment_intent
                SET state=%s, error_code=%s, error_detail=%s, completed_at=now()
                WHERE id=%s
                """,
                (intent_state, req.error_code or None, req.error_detail or None, req.intent_id),
            )

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

            action = "plugin.instance.ready" if req.success else "plugin.instance.failed"
            audit(
                conn,
                req.node_id,
                action,
                f"{req.node_id}:{req.instance_id}:{req.action}:{intent_state}",
            )

        return {"status": "recorded"}

"""插件热部署：受控 release、部署操作与蓝绿切换（ADR-030）。

这里只做控制面该做的事：维护不可变 release、创建/推进部署操作、以事务 + generation CAS
切换槽位 active 指针、把旧实例排空意图下发出去。**不在 HTTP 请求里执行任何插件命令**——真实
下载、校验、解包、离线安装、平台服务托管与候选验证都由节点 Agent 完成并回报。

只有受控制品仓里 `trust=first_party` 且通过落盘字节摘要复算的 release 才能被激活；Agent 只按
release_id 从受认证端点取 bundle，意图内不携带 URL、命令、宿主路径或密钥。
"""

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Annotated

from edge_material_sdk.generated.node.v1 import node_pb2 as pb
from fastapi import Body, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from psycopg.types.json import Jsonb

from ..contracts import audit, fail, hash_token, identifier, one, out, parse, rows, text_field
from ..infrastructure.catalog import plugin as catalog_plugin
from ..infrastructure.plugin_configurations import (
    MULTIMODAL_CONFIG_DEFAULTS,
    normalize_configuration,
    save_deployment_configuration,
)
from ..infrastructure.preflight import check_preflight

# 候选实例从 accepted 到 candidate_ready 的默认总时限（ADR-030 §2.8）。
OPERATION_DEADLINE_MS = 300_000
# 排空旧实例的 grace period：真正的下限是**插件自己声明的最大请求 deadline**
# （`default_deadline_ms`），所以取值就是"声明的 deadline 夹在 [兜底值, 上限] 之间"。
# 兜底值只用来挡住明显不合法的声明（0/负值）；上限防止插件声明一个荒谬的大 deadline 后
# 让操作永远挂着。声明得小就该排空得快——不强加一个与声明无关的固定下限。
DRAIN_GRACE_FLOOR_MS = 1_000
DRAIN_GRACE_CEILING_MS = 300_000
# 指标聚合：窗口是显式的，避免把历史操作当成"当前状态"；上限 30 天。
METRICS_WINDOW_HOURS_DEFAULT = 24
METRICS_WINDOW_HOURS_MAX = 720
# 每阶段的耗时统计（真实台账里的毫秒值，不做估算、不补零）。
METRIC_PHASES = ("staging", "starting", "validating", "draining")
# 仍然"在飞行中"、尚未进入终态的意图与操作。
ACTIVE_INTENT_STATES = ("pending", "dispatched")
TERMINAL_OPERATION_STAGES = ("succeeded", "failed", "cancelled")
PRE_CUTOVER_STAGES = ("accepted", "staging", "starting", "validating", "candidate_ready")

STAGE_TO_PROTO = {
    "accepted": "PLUGIN_OPERATION_STAGE_ACCEPTED",
    "staging": "PLUGIN_OPERATION_STAGE_STAGING",
    "starting": "PLUGIN_OPERATION_STAGE_STARTING",
    "validating": "PLUGIN_OPERATION_STAGE_VALIDATING",
    "candidate_ready": "PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
    "cutting_over": "PLUGIN_OPERATION_STAGE_CUTTING_OVER",
    "draining_old": "PLUGIN_OPERATION_STAGE_DRAINING_OLD",
    "succeeded": "PLUGIN_OPERATION_STAGE_SUCCEEDED",
    "failed": "PLUGIN_OPERATION_STAGE_FAILED",
    "cancelled": "PLUGIN_OPERATION_STAGE_CANCELLED",
}
# 上报里的 `stage` 是 proto 枚举字段：`parse()` 之后 `req.stage` 拿到的是**枚举数值**，
# 不是枚举名。这里按数值建反向表，否则任何回报都会退化成 `invalid_deployment_stage`。
PROTO_TO_STAGE = {
    getattr(pb, value): key for key, value in STAGE_TO_PROTO.items() if hasattr(pb, value)
}

# 回报阶段 → 运行实例状态。两者**不是同一套词汇**：`stage=staging` 表示"取制品/离线安装
# 这一步完成了"，此时实例的真实状态是"已解包装好、还没交给平台服务"，也就是 `staged`。
# 直接把 stage 名写进 `plugin_runtime_instance.state` 会撞上 CHECK 约束（没有 `staging`）。
STAGE_TO_RUNTIME_STATE = {
    "staging": "staged",
    "starting": "starting",
    "validating": "validating",
    "candidate_ready": "candidate_ready",
}

ROLE_TO_PROTO = {
    "candidate": "PLUGIN_RUNTIME_ROLE_CANDIDATE",
    "active": "PLUGIN_RUNTIME_ROLE_ACTIVE",
    "previous": "PLUGIN_RUNTIME_ROLE_PREVIOUS",
    "failed": "PLUGIN_RUNTIME_ROLE_FAILED",
}
STATE_TO_PROTO = {
    "planned": "PLUGIN_RUNTIME_STATE_PLANNED",
    "staged": "PLUGIN_RUNTIME_STATE_STAGED",
    "starting": "PLUGIN_RUNTIME_STATE_STARTING",
    "validating": "PLUGIN_RUNTIME_STATE_VALIDATING",
    "candidate_ready": "PLUGIN_RUNTIME_STATE_CANDIDATE_READY",
    "active": "PLUGIN_RUNTIME_STATE_ACTIVE",
    "draining": "PLUGIN_RUNTIME_STATE_DRAINING",
    "stopped": "PLUGIN_RUNTIME_STATE_STOPPED",
    "failed": "PLUGIN_RUNTIME_STATE_FAILED",
    "uninstalled": "PLUGIN_RUNTIME_STATE_UNINSTALLED",
}


def metric_label(value: str) -> str:
    """Prometheus label 值转义：反斜杠、双引号与换行必须转义，否则抓取端解析失败。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metrics_labels(bucket: dict, extra: str = "") -> str:
    """指标 label 只取 node / plugin / release / kind / stage / reason 六个维度。

    `kind` 是操作类别（`provision` / `upgrade` / `rollback`，取值有界），让"按操作聚合"能回答
    "失败的是升级还是回滚"，而不是只按阶段看。`operation_id` 是高基数维度，按
    `docs/design/plugin-orchestration.md` §8「避免把高基数 ID 放进 Prometheus label」**故意不进
    label**：逐操作耗时由部署操作资源（`GET /admin/v1/plugin-deployments/{operation_id}`）与
    审计行承担。
    """
    base = ",".join(
        f'{name}="{metric_label(str(bucket[column]))}"'
        for name, column in (
            ("node_id", "node_id"),
            ("plugin_id", "plugin_id"),
            ("release_id", "release_id"),
            ("kind", "kind"),
            ("stage", "stage"),
            ("reason", "reason"),
        )
    )
    return f"{{{base}{extra}}}"


def config_hash(config: dict) -> str:
    """配置摘要：与配置内容绑定，用于判断"同一配置是否已生效"。"""
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def release_proto(row: dict) -> dict:
    return {
        "release_id": row["release_id"],
        "plugin_id": row["plugin_id"],
        "plugin_version": row["plugin_version"],
        "platform": row["platform"],
        "arch": row["arch"],
        "form": row["form"],
        "artifact_digest": row["artifact_digest"],
        "bundle_digest": row["bundle_digest"],
        "manifest_digest": row["manifest_digest"],
        "config_schema_digest": row["config_schema_digest"],
        "sbom_digest": row["sbom_digest"],
        "bundle_bytes": row["bundle_bytes"],
        "entrypoint": row["entrypoint"],
        "runtime_requirements": row["runtime_requirements"],
        "trust": row["trust"],
        "authenticated": row["authenticated"],
        "authentication_method": row["authentication_method"],
        "signature_status": row["signature_status"],
        "sbom_components": row["sbom_components"],
        "declared_memory_bytes": row["declared_memory_bytes"],
        "declared_cpu_millicores": row["declared_cpu_millicores"],
        "default_deadline_ms": row["default_deadline_ms"],
        "published_at": row["published_at"].isoformat() if row["published_at"] else "",
        "created_by": row["created_by"],
    }


def runtime_proto(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "runtime_instance_id": row["runtime_instance_id"],
        "instance_id": row["instance_id"],
        "node_id": row["node_id"],
        "plugin_id": row["plugin_id"],
        "release_id": row["release_id"],
        "artifact_digest": row["artifact_digest"],
        "bundle_digest": row["bundle_digest"],
        "generation": row["generation"],
        "role": ROLE_TO_PROTO.get(row["role"], "PLUGIN_RUNTIME_ROLE_UNSPECIFIED"),
        "state": STATE_TO_PROTO.get(row["state"], "PLUGIN_RUNTIME_STATE_UNSPECIFIED"),
        "endpoint": row["endpoint"],
        "supervisor_id": row["supervisor_id"],
        "unit_name": row["unit_name"],
        "install_dir": row["install_dir"],
        "verified_plugin_id": row["verified_plugin_id"],
        "verified_artifact_digest": row["verified_artifact_digest"],
        "launch_ms": row["launch_ms"] or 0,
        "drain_ms": row["drain_ms"] or 0,
        "error_code": row["error_code"] or "",
        "error_detail": row["error_detail"] or "",
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else "",
    }


def operation_proto(conn, row: dict) -> dict:
    """把部署操作行转成下发给 Console 的契约。

    `active` 取的是**读取时槽位的 active 指针**，不是本次操作自己的历史：切换成功后
    它就是本次 candidate，切换前失败时仍是本次操作开始前在服务的旧实例（等于
    `from_runtime_instance_id`）。本次操作替换掉的实例恒由 `from_runtime_instance_id`
    表达，两者语义不能混用，否则已成功的升级会把 STOPPED 的旧实例显示成"当前 active"。
    """
    candidate = one(
        conn,
        "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s",
        (row["candidate_runtime_instance_id"],),
    )
    slot = one(
        conn,
        "SELECT * FROM console_plugin_instance WHERE instance_id=%s",
        (row["instance_id"],),
    )
    active = None
    if slot and slot["active_runtime_instance_id"]:
        active = one(
            conn,
            "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s",
            (slot["active_runtime_instance_id"],),
        )
    previous = None
    if slot and slot["previous_runtime_instance_id"]:
        previous = one(
            conn,
            "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s",
            (slot["previous_runtime_instance_id"],),
        )
    return {
        "operation_id": row["operation_id"],
        "kind": row["kind"],
        "node_id": row["node_id"],
        "instance_id": row["instance_id"],
        "plugin_id": row["plugin_id"],
        "release_id": row["release_id"],
        "from_runtime_instance_id": row["from_runtime_instance_id"] or "",
        "candidate_runtime_instance_id": row["candidate_runtime_instance_id"],
        "rollback_of_operation_id": row["rollback_of_operation_id"] or "",
        "generation": row["generation"],
        "stage": STAGE_TO_PROTO.get(row["stage"], "PLUGIN_OPERATION_STAGE_UNSPECIFIED"),
        "cancellable": row["stage"] in PRE_CUTOVER_STAGES,
        "deadline_unix_ms": row["deadline_unix_ms"],
        "error_code": row["error_code"] or "",
        "error_detail": row["error_detail"] or "",
        "staging_ms": row["staging_ms"] or 0,
        "starting_ms": row["starting_ms"] or 0,
        "validating_ms": row["validating_ms"] or 0,
        "draining_ms": row["draining_ms"] or 0,
        "config": row["config"],
        "candidate": runtime_proto(candidate),
        "active": runtime_proto(active),
        "previous": runtime_proto(previous),
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else "",
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else "",
    }


def intent_proto(row: dict, plugin_id: str, plugin_version: str, config_hash: str = "") -> dict:
    """把部署意图行转成下发给 Agent 的契约。意图内不出现 URL、命令、宿主路径或密钥。

    `console_deployment_intent` 只存 `instance_id`，插件身份要由调用方从逻辑槽位
    （`console_plugin_instance`）解出来传进来，避免在这里再查一次库。
    """
    action_enum = {
        "install": "DEPLOYMENT_ACTION_INSTALL",
        "start": "DEPLOYMENT_ACTION_START",
        "stop": "DEPLOYMENT_ACTION_STOP",
        "uninstall": "DEPLOYMENT_ACTION_UNINSTALL",
        "rollback": "DEPLOYMENT_ACTION_ROLLBACK",
        "drain": "DEPLOYMENT_ACTION_DRAIN",
        "task_process": "DEPLOYMENT_ACTION_TASK_PROCESS",
        "stage_release": "DEPLOYMENT_ACTION_STAGE_RELEASE",
        "reconcile": "DEPLOYMENT_ACTION_RECONCILE",
    }.get(row["action"], "DEPLOYMENT_ACTION_UNSPECIFIED")
    return {
        "intent_id": row["id"],
        "instance_id": row["instance_id"],
        "node_id": row["node_id"],
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "action": action_enum,
        "artifact_digest": row["artifact_digest"],
        "rollback_digest": row["rollback_digest"] or "",
        "config": row["config"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        "deadline_unix_ms": row["deadline_unix_ms"] or 0,
        "operation_id": row["operation_id"] or "",
        "generation": row["generation"] or 0,
        "release_id": row["release_id"] or "",
        "bundle_digest": row["bundle_digest"] or "",
        "runtime_instance_id": row["runtime_instance_id"] or "",
        "grace_period_ms": row["grace_period_ms"] or 0,
        "config_hash": config_hash,
    }


def insert_intent(conn, **fields) -> str:
    intent_id = identifier("intent")
    conn.execute(
        """
        INSERT INTO console_deployment_intent(
            id, node_id, instance_id, action, artifact_digest, rollback_digest, config,
            state, created_by, operation_id, generation, release_id, bundle_digest,
            runtime_instance_id, grace_period_ms, deadline_unix_ms
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            intent_id,
            fields["node_id"],
            fields["instance_id"],
            fields["action"],
            fields["artifact_digest"],
            fields.get("rollback_digest"),
            Jsonb(fields.get("config") or {}),
            fields["created_by"],
            fields.get("operation_id"),
            fields.get("generation") or 0,
            fields.get("release_id"),
            fields.get("bundle_digest"),
            fields.get("runtime_instance_id"),
            fields.get("grace_period_ms"),
            fields.get("deadline_unix_ms"),
        ),
    )
    return intent_id


def node_headroom_bytes(conn, node_row, exclude_instance_id: str = "") -> tuple[int, int]:
    """返回 (节点总内存, 本节点**其它槽位**已由 active/draining/candidate_ready 声明的内存)。

    余量按**旧实例声明资源加候选资源**计算：本槽位当前旧实例的声明由调用方单独减一次，
    所以这里必须把本槽位整个排掉。否则同一份声明被减两次，内存紧张的节点上合法升级会被误拒
    （旧实例既算进"其它槽位占用"，又被当成"旧实例声明"再减一遍）。
    """
    occupied = conn.execute(
        """
        SELECT COALESCE(sum(release.declared_memory_bytes), 0)
        FROM plugin_runtime_instance AS instance
        JOIN plugin_release AS release ON release.release_id = instance.release_id
        WHERE instance.node_id = %s AND instance.instance_id <> %s
          AND instance.state IN (
              'planned', 'staged', 'starting', 'validating', 'candidate_ready', 'active', 'draining'
          )
        """,
        (node_row["node_id"], exclude_instance_id),
    ).fetchone()[0]
    return int(node_row["memory_bytes"]), int(occupied)


def purge_node_deployment_rows(conn, node_id: str) -> None:
    """删除某个节点自己的热部署台账（节点下线/撤销后的清理动作）。

    `plugin_deployment_operation` / `plugin_runtime_instance` 都带 `console_node` 外键，
    ADR-030 引入这两张表之后，"清理已下线/已撤销节点"必须先按 FK 顺序清掉台账，否则删节点
    会撞外键、节点从此清不掉。删除顺序：意图 → 部署操作 → 运行实例 → 逻辑槽位。

    只清**这个节点自己的台账**：`plugin_release` 是不可变制品仓，任何清理都不动它；
    Agent 本机 `releases/` 目录由 Agent 自己保留（ADR-030 要求留作已验证回滚版本）。
    """
    # 自引用外键（回滚操作指回被回滚的操作）先断开，避免同一语句里删父子行。
    conn.execute(
        "UPDATE plugin_deployment_operation SET rollback_of_operation_id=NULL WHERE node_id=%s",
        (node_id,),
    )
    conn.execute("DELETE FROM console_deployment_intent WHERE node_id=%s", (node_id,))
    conn.execute("DELETE FROM plugin_deployment_operation WHERE node_id=%s", (node_id,))
    conn.execute("DELETE FROM plugin_runtime_instance WHERE node_id=%s", (node_id,))
    conn.execute("DELETE FROM console_plugin_instance WHERE node_id=%s", (node_id,))


def register(app, pool, auth, settings):
    @app.post("/admin/v1/nodes/{node_id}/plugins/{plugin_id}/runtimes/{runtime_id}:retire")
    def retire_runtime(
        node_id: str,
        plugin_id: str,
        runtime_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        from ..infrastructure.runtime_bindings import runtime_is_pinned

        with pool.connection() as conn:
            node = one(conn, "SELECT * FROM console_node WHERE node_id=%s FOR UPDATE", (node_id,))
            if not node or node["status"] != "ready":
                fail(409, "target_node_not_ready")
            slot = one(
                conn,
                "SELECT * FROM console_plugin_instance WHERE node_id=%s AND plugin_id=%s "
                "FOR UPDATE",
                (node_id, plugin_id),
            )
            runtime = one(
                conn,
                "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s "
                "AND node_id=%s AND plugin_id=%s FOR UPDATE",
                (runtime_id, node_id, plugin_id),
            )
            if not slot or not runtime:
                fail(404, "plugin_runtime_not_found")
            if slot["active_runtime_instance_id"] == runtime_id or runtime["role"] != "previous":
                fail(409, "plugin_active_runtime_cannot_retire")
            if runtime["state"] != "active":
                fail(409, "plugin_runtime_not_active")
            if runtime_is_pinned(conn, runtime):
                fail(409, "plugin_runtime_revision_pinned")
            if one(
                conn,
                "SELECT 1 FROM plugin_deployment_operation WHERE instance_id=%s "
                "AND stage NOT IN ('succeeded','failed','cancelled')",
                (slot["instance_id"],),
            ):
                fail(409, "plugin_deployment_in_progress")
            release = one(
                conn, "SELECT * FROM plugin_release WHERE release_id=%s", (runtime["release_id"],)
            )
            grace = min(
                max(release["default_deadline_ms"], DRAIN_GRACE_FLOOR_MS), DRAIN_GRACE_CEILING_MS
            )
            deadline = int(datetime.now(UTC).timestamp() * 1000) + grace + 60000
            operation = one(
                conn,
                "INSERT INTO plugin_deployment_operation(operation_id,kind,node_id,"
                "instance_id,plugin_id,release_id,from_runtime_instance_id,"
                "candidate_runtime_instance_id,generation,stage,deadline_unix_ms,"
                "config,created_by) "
                "VALUES (%s,'retire',%s,%s,%s,%s,%s,%s,%s,'draining_old',%s,%s,%s) RETURNING *",
                (
                    identifier("op"),
                    node_id,
                    slot["instance_id"],
                    plugin_id,
                    runtime["release_id"],
                    runtime_id,
                    runtime_id,
                    slot["generation"],
                    deadline,
                    Jsonb(runtime["config"]),
                    p.name,
                ),
            )
            conn.execute(
                "UPDATE plugin_runtime_instance SET state='draining',updated_at=now() "
                "WHERE runtime_instance_id=%s",
                (runtime_id,),
            )
            insert_intent(
                conn,
                node_id=node_id,
                instance_id=slot["instance_id"],
                action="drain",
                artifact_digest=runtime["artifact_digest"],
                config=runtime["config"],
                created_by=p.name,
                operation_id=operation["operation_id"],
                generation=slot["generation"],
                release_id=runtime["release_id"],
                bundle_digest=runtime["bundle_digest"],
                runtime_instance_id=runtime_id,
                grace_period_ms=grace,
                deadline_unix_ms=deadline,
            )
            audit(conn, p.name, "plugin.runtime.retire", runtime_id)
            return out(operation_proto(conn, operation), pb.PluginDeploymentOperation)

    # ── 受控制品仓：release 列表与导入 ─────────────────────────────────────

    @app.get("/admin/v1/plugin-releases")
    def list_releases(
        plugin_id: str = Query("", max_length=120),
        platform: str = Query("", max_length=32),
        arch: str = Query("", max_length=32),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        clauses, params = [], []
        if plugin_id:
            clauses.append("plugin_id=%s")
            params.append(plugin_id)
        if platform:
            clauses.append("platform=%s")
            params.append(platform)
        if arch:
            clauses.append("arch=%s")
            params.append(arch)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with pool.connection() as conn:
            items = rows(
                conn,
                f"SELECT * FROM plugin_release {where} ORDER BY published_at DESC "
                "LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
            total = conn.execute(
                f"SELECT count(*) FROM plugin_release {where}", tuple(params)
            ).fetchone()[0]
        return out(
            {"items": [release_proto(row) for row in items], "total": total},
            pb.PluginReleaseList,
        )

    @app.post("/admin/v1/plugin-releases:sync")
    def sync_releases(
        body: Annotated[dict | None, Body()] = None,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """从受控制品仓导入首方 release 描述符。

        关键点：**摘要由 API 重新计算**。描述符里的 `bundle_digest` 只是声明，
        真正的判据是落盘 bundle 文件的字节摘要；声明与实测不一致就拒绝导入，
        因此调用方无法通过提交 JSON 让一个未认证制品变成"已认证 release"。

        `body` 可省略：`sync` 的默认形态就是"用默认选项扫一遍制品仓"，缺 body 应当是
        `{}`（显式走默认），而不是未处理异常。
        """
        repository = settings.release_repository.resolve()
        if not repository.is_dir():
            fail(503, "release_repository_unavailable")
        dry_run = bool((body or {}).get("dry_run"))
        imported, unchanged, rejected = [], [], []

        with pool.connection() as conn:
            for descriptor_path in sorted(repository.glob("*/*/*/release.json")):
                label = descriptor_path.relative_to(repository).as_posix()
                try:
                    descriptor = json.loads(descriptor_path.read_text())
                except (OSError, ValueError) as error:
                    rejected.append(f"{label}:descriptor_unreadable:{error}")
                    continue
                problem = _descriptor_problem(descriptor)
                if problem:
                    rejected.append(f"{label}:{problem}")
                    continue
                bundle_path = (repository / descriptor["bundle_path"]).resolve()
                if not bundle_path.is_relative_to(repository) or not bundle_path.is_file():
                    rejected.append(f"{label}:bundle_path_outside_repository")
                    continue
                if bundle_path.stat().st_size != int(descriptor["bundle_bytes"]):
                    rejected.append(f"{label}:bundle_bytes_mismatch")
                    continue
                actual_digest = sha256_file(bundle_path)
                if actual_digest != descriptor["bundle_digest"]:
                    rejected.append(f"{label}:bundle_digest_mismatch")
                    continue

                existing = one(
                    conn,
                    "SELECT * FROM plugin_release WHERE plugin_id=%s AND plugin_version=%s "
                    "AND platform=%s AND arch=%s",
                    (
                        descriptor["plugin_id"],
                        descriptor["plugin_version"],
                        descriptor["platform"],
                        descriptor["arch"],
                    ),
                )
                if existing:
                    if existing["bundle_digest"] != descriptor["bundle_digest"]:
                        rejected.append(f"{label}:plugin_release_content_conflict")
                    else:
                        unchanged.append(descriptor["release_id"])
                    continue
                if dry_run:
                    imported.append(descriptor["release_id"])
                    continue

                conn.execute(
                    """
                    INSERT INTO plugin_release(
                        release_id, plugin_id, plugin_version, platform, arch, form,
                        artifact_digest, bundle_digest, manifest_digest, config_schema_digest,
                        sbom_digest, bundle_bytes, entrypoint, runtime_requirements, trust,
                        authenticated, authentication_method, signature_status, sbom_components,
                        declared_memory_bytes, declared_cpu_millicores, default_deadline_ms,
                        bundle_path, created_by
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s
                    )
                    """,
                    (
                        descriptor["release_id"],
                        descriptor["plugin_id"],
                        descriptor["plugin_version"],
                        descriptor["platform"],
                        descriptor["arch"],
                        descriptor["form"],
                        descriptor["artifact_digest"],
                        descriptor["bundle_digest"],
                        descriptor["manifest_digest"],
                        descriptor["config_schema_digest"],
                        descriptor["sbom_digest"],
                        int(descriptor["bundle_bytes"]),
                        Jsonb(descriptor["entrypoint"]),
                        Jsonb(descriptor["runtime_requirements"]),
                        descriptor["trust"],
                        bool(descriptor["authenticated"]),
                        descriptor["authentication_method"],
                        descriptor["signature_status"],
                        int(descriptor.get("sbom_components", 0)),
                        int(descriptor.get("declared_memory_bytes", 0)),
                        int(descriptor.get("declared_cpu_millicores", 0)),
                        int(descriptor.get("default_deadline_ms", 120000)),
                        descriptor["bundle_path"],
                        p.name,
                    ),
                )
                audit(
                    conn,
                    p.name,
                    "plugin.release.import",
                    f"{descriptor['release_id']}:{descriptor['bundle_digest']}",
                )
                imported.append(descriptor["release_id"])

        return out(
            {
                "imported": imported,
                "unchanged": unchanged,
                "rejected": rejected,
                # total 是"这一轮制品仓里被看到的 release 数"，包含被拒绝的，便于对账。
                "total": len(imported) + len(unchanged) + len(rejected),
            },
            pb.SyncPluginReleasesResponse,
        )

    # ── 部署操作：创建、详情、取消、回滚 ───────────────────────────────────

    @app.post("/admin/v1/nodes/{node_id}/plugins/{plugin_id}:provision", status_code=201)
    def provision_plugin(
        node_id: str,
        plugin_id: str,
        body: Annotated[dict | None, Body()] = None,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        return create_operation(node_id, plugin_id, body or {}, p, kind="provision")

    @app.post("/admin/v1/nodes/{node_id}/plugins/{plugin_id}:upgrade", status_code=201)
    def upgrade_plugin(
        node_id: str,
        plugin_id: str,
        body: Annotated[dict | None, Body()] = None,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        return create_operation(node_id, plugin_id, body or {}, p, kind="upgrade")

    def create_operation(node_id, plugin_id, body, principal, kind, connection=None):
        # 缺 body 等于空意图：release_id 缺失会在 `text_field` 处显式失败（422），
        # 而不是在 `body.get` 上炸出未处理异常。
        body = body or {}
        release_id = text_field(str(body.get("release_id", "")), 120)
        config = body.get("config") or {}
        config_id = body.get("config_id")

        with nullcontext(connection) if connection is not None else pool.connection() as conn:
            node = one(conn, "SELECT * FROM console_node WHERE node_id=%s FOR UPDATE", (node_id,))
            if not node:
                fail(404, "node_not_found")
            if node["status"] in {"revoked", "offline"}:
                fail(409, f"node_{node['status']}")
            if node["status"] == "draining":
                fail(409, "node_draining")
            if node["status"] == "candidate":
                fail(409, "candidate_node_not_admitted")

            release = one(
                conn, "SELECT * FROM plugin_release WHERE release_id=%s FOR UPDATE", (release_id,)
            )
            if not release:
                fail(404, "plugin_release_not_found")
            if release["plugin_id"] != plugin_id:
                fail(422, "plugin_release_plugin_mismatch")
            if (
                release["trust"] not in {"first_party", "trusted_publisher"}
                or not release["authenticated"]
            ):
                fail(422, "plugin_release_not_authenticated")
            from ..infrastructure.plugin_registry import require_release_trust

            require_release_trust(conn, release)
            if release["platform"] != node["platform"] or release["arch"] != node["arch"]:
                fail(422, "plugin_release_platform_mismatch")

            entry = catalog_plugin(
                settings,
                plugin_id,
                conn,
                release_id if release["trust"] == "trusted_publisher" else "",
            )
            if config_id and not config:
                cfg_row = one(
                    conn,
                    "SELECT config FROM console_plugin_config WHERE id=%s AND plugin_id=%s",
                    (config_id, plugin_id),
                )
                if cfg_row:
                    config = cfg_row["config"]
                else:
                    fail(422, "plugin_config_not_found")
            if release["trust"] == "trusted_publisher":
                config = normalize_configuration(entry, config)

            preflight = check_preflight(node, entry, config=config)
            if not preflight["eligible"]:
                audit(
                    conn,
                    principal.name,
                    "node.preflight.reject",
                    f"{node_id}:{plugin_id}:{preflight['reason_code']}",
                )
                fail(422, preflight["reason_code"])

            slot = one(
                conn,
                "SELECT * FROM console_plugin_instance WHERE node_id=%s AND plugin_id=%s "
                "FOR UPDATE",
                (node_id, plugin_id),
            )
            if kind == "upgrade" and (not slot or not slot["active_runtime_instance_id"]):
                fail(409, "plugin_not_active_for_upgrade")
            if slot and slot["endpoint"] and kind == "provision":
                fail(409, "plugin_already_active")

            active_runtime = None
            if slot and slot["active_runtime_instance_id"]:
                active_runtime = one(
                    conn,
                    "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s",
                    (slot["active_runtime_instance_id"],),
                )
            occupied = 0
            if slot:
                count, occupied = conn.execute(
                    "SELECT count(*),coalesce(sum(r.declared_memory_bytes),0) "
                    "FROM plugin_runtime_instance i JOIN plugin_release r USING(release_id) "
                    "WHERE i.instance_id=%s AND i.state NOT IN ('stopped','failed','uninstalled')",
                    (slot["instance_id"],),
                ).fetchone()
                if release["trust"] == "trusted_publisher" and count >= 4:
                    fail(422, "plugin_resident_version_limit")
            total, occupied_by_others = node_headroom_bytes(
                conn, node, slot["instance_id"] if slot else ""
            )
            # 升级余量 = 节点总内存 - 其它槽位已声明 - 旧实例声明；候选必须还能放得下。
            headroom = total - occupied_by_others - int(occupied)
            if headroom < int(release["declared_memory_bytes"]):
                audit(
                    conn,
                    principal.name,
                    "plugin.deploy.reject",
                    f"{node_id}:{plugin_id}:upgrade_headroom_insufficient",
                )
                fail(422, "upgrade_headroom_insufficient")

            if slot and one(
                conn,
                "SELECT operation_id FROM plugin_deployment_operation WHERE instance_id=%s "
                "AND stage NOT IN ('succeeded', 'failed', 'cancelled') LIMIT 1",
                (slot["instance_id"],),
            ):
                fail(409, "plugin_deployment_in_progress")
            generation = (slot["generation"] if slot else 0) + 1
            if slot is None:
                slot = one(
                    conn,
                    """
                    INSERT INTO console_plugin_instance(
                        instance_id, node_id, plugin_id, plugin_version, artifact_digest,
                        generation, desired_state, actual_state, config_hash, config, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'ready', 'planned', %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        identifier("inst"),
                        node_id,
                        plugin_id,
                        release["plugin_version"],
                        release["artifact_digest"],
                        generation,
                        config_hash(config),
                        Jsonb(config),
                        principal.name,
                    ),
                )
            else:
                slot = one(
                    conn,
                    "UPDATE console_plugin_instance SET generation=%s, plugin_version=%s, "
                    "config=%s, config_hash=%s, updated_at=now() WHERE instance_id=%s RETURNING *",
                    (
                        generation,
                        release["plugin_version"],
                        Jsonb(config),
                        config_hash(config),
                        slot["instance_id"],
                    ),
                )

            candidate = one(
                conn,
                """
                INSERT INTO plugin_runtime_instance(
                    runtime_instance_id, instance_id, node_id, plugin_id, release_id,
                    artifact_digest, bundle_digest, generation, role, state
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'candidate', 'planned')
                RETURNING *
                """,
                (
                    identifier("rti"),
                    slot["instance_id"],
                    node_id,
                    plugin_id,
                    release_id,
                    release["artifact_digest"],
                    release["bundle_digest"],
                    generation,
                ),
            )

            grace_period_ms = min(
                max(int(release["default_deadline_ms"]), DRAIN_GRACE_FLOOR_MS),
                DRAIN_GRACE_CEILING_MS,
            )
            conn.execute(
                "UPDATE plugin_runtime_instance SET config_hash=%s,config=%s "
                "WHERE runtime_instance_id=%s",
                (config_hash(config), Jsonb(config), candidate["runtime_instance_id"]),
            )
            deadline = datetime.now(UTC) + timedelta(
                milliseconds=OPERATION_DEADLINE_MS + grace_period_ms
            )
            operation = one(
                conn,
                """
                INSERT INTO plugin_deployment_operation(
                    operation_id, kind, node_id, instance_id, plugin_id, release_id,
                    from_runtime_instance_id, candidate_runtime_instance_id, generation,
                    stage, deadline_unix_ms, config, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'accepted', %s, %s, %s)
                RETURNING *
                """,
                (
                    identifier("op"),
                    kind,
                    node_id,
                    slot["instance_id"],
                    plugin_id,
                    release_id,
                    active_runtime["runtime_instance_id"] if active_runtime else None,
                    candidate["runtime_instance_id"],
                    generation,
                    int(deadline.timestamp() * 1000),
                    Jsonb(config),
                    principal.name,
                ),
            )
            insert_intent(
                conn,
                node_id=node_id,
                instance_id=slot["instance_id"],
                action="stage_release",
                artifact_digest=release["artifact_digest"],
                config=config,
                created_by=principal.name,
                operation_id=operation["operation_id"],
                generation=generation,
                release_id=release_id,
                bundle_digest=release["bundle_digest"],
                runtime_instance_id=candidate["runtime_instance_id"],
                grace_period_ms=grace_period_ms,
                deadline_unix_ms=operation["deadline_unix_ms"],
            )
            audit(
                conn,
                principal.name,
                "plugin.deploy.accepted",
                f"{node_id}:{plugin_id}:{operation['operation_id']}:{release_id}",
            )
            return out(operation_proto(conn, operation), pb.PluginDeploymentOperation)

    @app.post("/admin/v1/nodes/{node_id}/plugins:batch-deploy")
    def batch_deploy_plugins(
        node_id: str,
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """批量装配复用受控热部署；每项独立事务，拒绝项不掩盖也不撤销其它项。"""
        req = parse(body, pb.BatchDeployPluginsRequest)
        plugin_ids = list(req.plugin_ids) or [
            "org.sensoryplex.vlm-moondream",
            "org.sensoryplex.asr-whisper-mlx",
            "org.sensoryplex.ocr-rapidocr",
            "org.sensoryplex.embed-bge-onnx",
        ]
        if len(plugin_ids) > 16:
            fail(422, "batch_deployment_limit_exceeded")
        operations, already_ready, rejected = [], [], []
        for plugin_id in dict.fromkeys(plugin_ids):
            try:
                with pool.connection() as conn:
                    node = one(
                        conn, "SELECT * FROM console_node WHERE node_id=%s FOR UPDATE", (node_id,)
                    )
                    if not node:
                        fail(404, "node_not_found")
                    if not node["session_token_hash"]:
                        fail(409, "node_agent_not_enrolled")
                    entry = catalog_plugin(settings, plugin_id)
                    preflight = check_preflight(node, entry)
                    if not preflight["eligible"]:
                        fail(422, preflight["reason_code"])
                    pending = one(
                        conn,
                        "SELECT * FROM plugin_deployment_operation WHERE node_id=%s "
                        "AND plugin_id=%s AND stage NOT IN ('succeeded', 'failed', 'cancelled') "
                        "ORDER BY created_at DESC LIMIT 1",
                        (node_id, plugin_id),
                    )
                    if pending:
                        save_deployment_configuration(conn, entry, pending["config"], p.name)
                        operations.append(operation_proto(conn, pending))
                        continue
                    slot = one(
                        conn,
                        "SELECT * FROM console_plugin_instance WHERE node_id=%s AND plugin_id=%s",
                        (node_id, plugin_id),
                    )
                    if (
                        slot
                        and slot["active_runtime_instance_id"]
                        and slot["actual_state"] == "ready"
                    ):
                        save_deployment_configuration(conn, entry, slot["config"], p.name)
                        already_ready.append(plugin_id)
                        continue
                    release = one(
                        conn,
                        "SELECT * FROM plugin_release WHERE plugin_id=%s AND platform=%s "
                        "AND arch=%s AND trust IN ('first_party','trusted_publisher') AND "
                        "authenticated "
                        "AND form='local_native' AND plugin_version=%s AND artifact_digest=%s",
                        (
                            plugin_id,
                            node["platform"],
                            node["arch"],
                            entry["version"],
                            entry["digest"],
                        ),
                    )
                    if not release:
                        fail(422, "plugin_release_not_found")
                    # 配置来自已保存方案或已有槽位；模型目录等必填值不得猜测。
                    saved = one(
                        conn,
                        "SELECT config FROM console_plugin_config WHERE plugin_id=%s "
                        "ORDER BY created_at DESC, id DESC LIMIT 1",
                        (plugin_id,),
                    )
                    if saved:
                        config = saved["config"]
                    elif slot:
                        config = slot["config"]
                    else:
                        config = MULTIMODAL_CONFIG_DEFAULTS.get(plugin_id, {})
                    from jsonschema import Draft202012Validator

                    if list(Draft202012Validator(entry["config_schema"]).iter_errors(config)):
                        fail(422, "plugin_configuration_required")
                    if plugin_id in MULTIMODAL_CONFIG_DEFAULTS:
                        config = normalize_configuration(entry, config)
                    result = create_operation(
                        node_id,
                        plugin_id,
                        {"release_id": release["release_id"], "config": config},
                        p,
                        "upgrade" if slot and slot["active_runtime_instance_id"] else "provision",
                        connection=conn,
                    )
                    save_deployment_configuration(conn, entry, config, p.name)
                    operations.append(result)
            except HTTPException as error:
                rejected.append({"plugin_id": plugin_id, "reason": str(error.detail)})
        with pool.connection() as conn:
            audit(
                conn,
                p.name,
                "node.batch_deploy",
                f"{node_id}:accepted={len(operations)}:ready={len(already_ready)}:rejected={len(rejected)}",
            )
        return out(
            {
                "node_id": node_id,
                "operations": operations,
                "already_ready": already_ready,
                "rejected": rejected,
            },
            pb.BatchDeployPluginsResponse,
        )

    @app.get("/admin/v1/plugin-deployments")
    def list_operations(
        node_id: str = Query("", max_length=120),
        stage: str = Query("", max_length=32),
        limit: int = Query(20, ge=1, le=100),
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        clauses, params = [], []
        if node_id:
            clauses.append("node_id=%s")
            params.append(node_id)
        if stage:
            clauses.append("stage=%s")
            params.append(stage)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with pool.connection() as conn:
            items = rows(
                conn,
                f"SELECT * FROM plugin_deployment_operation {where} "
                "ORDER BY created_at DESC LIMIT %s",
                (*params, limit),
            )
            total = conn.execute(
                f"SELECT count(*) FROM plugin_deployment_operation {where}", tuple(params)
            ).fetchone()[0]
            payload = [operation_proto(conn, row) for row in items]
        return out({"items": payload, "total": total}, pb.PluginDeploymentOperationList)

    @app.get("/admin/v1/plugin-deployments/metrics")
    def deployment_metrics(
        node_id: str = Query("", max_length=120),
        plugin_id: str = Query("", max_length=120),
        window_hours: int = Query(METRICS_WINDOW_HOURS_DEFAULT, ge=1, le=METRICS_WINDOW_HOURS_MAX),
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """可观测性：把部署操作台账按 node/plugin/release/stage/reason 聚合成 Prometheus 文本。

        只做真实台账的 `GROUP BY`，**不合成数据**：每条 series 都对应台账里真实存在的操作；
        在飞行中的操作按**当前阶段**计入（快照语义，HELP 里写明）。响应里只有 id、阶段、reason
        与耗时统计，**不含** bundle 内容、命令行、配置值、原始媒体或模型输入。

        `operation_id` 故意不进 label（高基数，见 `docs/design/plugin-orchestration.md` §8）；
        逐操作耗时由 `GET /admin/v1/plugin-deployments/{operation_id}` 与审计行承担。
        """
        clauses = ["created_at > now() - make_interval(hours => %s)"]
        params: list[object] = [window_hours]
        if node_id:
            clauses.append("node_id=%s")
            params.append(node_id)
        if plugin_id:
            clauses.append("plugin_id=%s")
            params.append(plugin_id)
        where = " AND ".join(clauses)
        with pool.connection() as conn:
            buckets = rows(
                conn,
                "SELECT node_id, plugin_id, release_id, kind, stage, "
                "coalesce(error_code, '') AS reason, "
                "count(*) AS operations, "
                "coalesce(sum(staging_ms), 0) AS staging_sum, "
                "coalesce(max(staging_ms), 0) AS staging_max, "
                "coalesce(sum(starting_ms), 0) AS starting_sum, "
                "coalesce(max(starting_ms), 0) AS starting_max, "
                "coalesce(sum(validating_ms), 0) AS validating_sum, "
                "coalesce(max(validating_ms), 0) AS validating_max, "
                "coalesce(sum(draining_ms), 0) AS draining_sum, "
                "coalesce(max(draining_ms), 0) AS draining_max "
                f"FROM plugin_deployment_operation WHERE {where} "
                "GROUP BY node_id, plugin_id, release_id, kind, stage, "
                "coalesce(error_code, '') "
                "ORDER BY node_id, plugin_id, release_id, kind, stage, reason",
                tuple(params),
            )
        # 指标 label 用与 API / Console 相同的阶段词表（proto 枚举名），
        # 否则同一字段会出现两套词汇，跨面核对必然出错。
        for bucket in buckets:
            bucket["stage"] = STAGE_TO_PROTO.get(
                bucket["stage"], "PLUGIN_OPERATION_STAGE_UNSPECIFIED"
            )
        lines = [
            "# 部署操作指标（ADR-030）：只做台账 GROUP BY，窗口内的操作按当前阶段计入。",
            "# HELP sensoryplex_plugin_deployment_operations"
            " Deployment operations in the window, grouped by node/plugin/release/stage/reason.",
            "# TYPE sensoryplex_plugin_deployment_operations gauge",
        ]
        lines += [
            f"sensoryplex_plugin_deployment_operations{metrics_labels(bucket)}"
            f" {int(bucket['operations'])}"
            for bucket in buckets
        ]
        for suffix, help_text in (
            ("sum", "Sum of phase durations in milliseconds, per deployment operation group."),
            ("max", "Maximum phase duration in milliseconds, per deployment operation group."),
        ):
            family = f"sensoryplex_plugin_deployment_phase_milliseconds_{suffix}"
            lines.append(f"# HELP {family} {help_text}")
            lines.append(f"# TYPE {family} gauge")
            for bucket in buckets:
                for phase in METRIC_PHASES:
                    lines.append(
                        f"{family}{metrics_labels(bucket, f',phase="{phase}"')}"
                        f" {int(bucket[f'{phase}_{suffix}'])}"
                    )
        lines.append(
            "# HELP sensoryplex_plugin_deployment_metrics_info"
            " Metric collection window of this scrape (filters are query parameters)."
        )
        lines.append("# TYPE sensoryplex_plugin_deployment_metrics_info gauge")
        lines.append(
            f'sensoryplex_plugin_deployment_metrics_info{{window_hours="{window_hours}"}} 1'
        )
        return PlainTextResponse(
            "\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    @app.get("/admin/v1/plugin-deployments/{operation_id}")
    def get_operation(
        operation_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        with pool.connection() as conn:
            operation = one(
                conn,
                "SELECT * FROM plugin_deployment_operation WHERE operation_id=%s",
                (operation_id,),
            )
            if not operation:
                fail(404, "plugin_deployment_operation_not_found")
            return out(operation_proto(conn, operation), pb.PluginDeploymentOperation)

    @app.post("/admin/v1/plugin-deployments/{operation_id}:cancel")
    def cancel_operation(
        operation_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """取消只允许在切换前。`cutting_over` 之后拒绝取消——那时旧实例可能已经排空。"""
        with pool.connection() as conn:
            operation = one(
                conn,
                "SELECT * FROM plugin_deployment_operation WHERE operation_id=%s FOR UPDATE",
                (operation_id,),
            )
            if not operation:
                fail(404, "plugin_deployment_operation_not_found")
            if operation["stage"] in TERMINAL_OPERATION_STAGES:
                fail(409, "plugin_deployment_operation_already_closed")
            if operation["stage"] not in PRE_CUTOVER_STAGES:
                fail(409, "plugin_deployment_cancel_window_closed")

            conn.execute(
                """
                UPDATE plugin_deployment_operation
                SET stage='cancelled', cancelled=true, error_code='cancelled_by_administrator',
                    completed_at=now(), updated_at=now()
                WHERE operation_id=%s
                """,
                (operation_id,),
            )
            conn.execute(
                """
                UPDATE plugin_runtime_instance
                SET role='failed', state='failed', error_code='cancelled_by_administrator',
                    updated_at=now()
                WHERE runtime_instance_id=%s
                """,
                (operation["candidate_runtime_instance_id"],),
            )
            slot = one(
                conn,
                "SELECT * FROM console_plugin_instance WHERE instance_id=%s",
                (operation["instance_id"],),
            )
            insert_intent(
                conn,
                node_id=operation["node_id"],
                instance_id=operation["instance_id"],
                action="stop",
                artifact_digest=slot["artifact_digest"] if slot else "",
                config=operation["config"],
                created_by=p.name,
                operation_id=operation_id,
                generation=operation["generation"],
                release_id=operation["release_id"],
                runtime_instance_id=operation["candidate_runtime_instance_id"],
            )
            audit(
                conn,
                p.name,
                "plugin.deploy.cancelled",
                f"{operation['node_id']}:{operation['plugin_id']}:{operation_id}",
            )
            cancelled = one(
                conn,
                "SELECT * FROM plugin_deployment_operation WHERE operation_id=%s",
                (operation_id,),
            )
            return out(operation_proto(conn, cancelled), pb.PluginDeploymentOperation)

    @app.post("/admin/v1/plugin-deployments/{operation_id}:rollback", status_code=201)
    def rollback_operation(
        operation_id: str,
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        """显式回滚 = 创建**反向部署操作**，不改写历史操作行。"""
        with pool.connection() as conn:
            operation = one(
                conn,
                "SELECT * FROM plugin_deployment_operation WHERE operation_id=%s FOR UPDATE",
                (operation_id,),
            )
            if not operation:
                fail(404, "plugin_deployment_operation_not_found")
            if operation["stage"] not in ("succeeded", "failed"):
                fail(409, "plugin_deployment_operation_not_settled")

            slot = one(
                conn,
                "SELECT * FROM console_plugin_instance WHERE instance_id=%s FOR UPDATE",
                (operation["instance_id"],),
            )
            if not slot or not slot["previous_runtime_instance_id"]:
                fail(422, "no_previous_runtime_instance_for_rollback")
            target = one(
                conn,
                "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s",
                (slot["previous_runtime_instance_id"],),
            )
            if not target:
                fail(422, "no_previous_runtime_instance_for_rollback")
            release = one(
                conn,
                "SELECT * FROM plugin_release WHERE release_id=%s",
                (target["release_id"],),
            )
            from ..infrastructure.plugin_registry import require_release_trust

            require_release_trust(conn, release)

            # 回滚还原目标版本自己的配置；升级后的槽位配置不适用于旧 release。
            restore_config = target["config"] if target["config_hash"] else slot["config"]
            count, occupied = conn.execute(
                "SELECT count(*),coalesce(sum(r.declared_memory_bytes),0) "
                "FROM plugin_runtime_instance i JOIN plugin_release r USING(release_id) "
                "WHERE i.instance_id=%s AND i.state NOT IN ('stopped','failed','uninstalled')",
                (slot["instance_id"],),
            ).fetchone()
            if release["trust"] == "trusted_publisher" and count >= 4:
                fail(422, "plugin_resident_version_limit")
            node = one(conn, "SELECT * FROM console_node WHERE node_id=%s", (operation["node_id"],))
            total, others = node_headroom_bytes(conn, node, slot["instance_id"])
            if total - others - int(occupied) < int(release["declared_memory_bytes"]):
                fail(422, "plugin_memory_headroom_insufficient")

            generation = slot["generation"] + 1
            conn.execute(
                "UPDATE console_plugin_instance SET generation=%s, updated_at=now() "
                "WHERE instance_id=%s",
                (generation, slot["instance_id"]),
            )
            candidate = one(
                conn,
                """
                INSERT INTO plugin_runtime_instance(
                    runtime_instance_id, instance_id, node_id, plugin_id, release_id,
                    artifact_digest, bundle_digest, generation, role, state, config_hash, config
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'candidate', 'planned', %s, %s)
                RETURNING *
                """,
                (
                    identifier("rti"),
                    slot["instance_id"],
                    operation["node_id"],
                    operation["plugin_id"],
                    target["release_id"],
                    target["artifact_digest"],
                    target["bundle_digest"],
                    generation,
                    config_hash(restore_config),
                    Jsonb(restore_config),
                ),
            )
            grace_period_ms = min(
                max(int(release["default_deadline_ms"]), DRAIN_GRACE_FLOOR_MS),
                DRAIN_GRACE_CEILING_MS,
            )
            deadline = datetime.now(UTC) + timedelta(
                milliseconds=OPERATION_DEADLINE_MS + grace_period_ms
            )
            new_operation = one(
                conn,
                """
                INSERT INTO plugin_deployment_operation(
                    operation_id, kind, node_id, instance_id, plugin_id, release_id,
                    from_runtime_instance_id, candidate_runtime_instance_id,
                    rollback_of_operation_id, generation, stage, deadline_unix_ms, config,
                    created_by
                ) VALUES (%s, 'rollback', %s, %s, %s, %s, %s, %s, %s, %s, 'accepted', %s, %s, %s)
                RETURNING *
                """,
                (
                    identifier("op"),
                    operation["node_id"],
                    slot["instance_id"],
                    operation["plugin_id"],
                    target["release_id"],
                    slot["active_runtime_instance_id"],
                    candidate["runtime_instance_id"],
                    operation_id,
                    generation,
                    int(deadline.timestamp() * 1000),
                    Jsonb(restore_config),
                    p.name,
                ),
            )
            insert_intent(
                conn,
                node_id=operation["node_id"],
                instance_id=slot["instance_id"],
                action="stage_release",
                artifact_digest=target["artifact_digest"],
                config=restore_config,
                created_by=p.name,
                operation_id=new_operation["operation_id"],
                generation=generation,
                release_id=target["release_id"],
                bundle_digest=target["bundle_digest"],
                runtime_instance_id=candidate["runtime_instance_id"],
                grace_period_ms=grace_period_ms,
                deadline_unix_ms=new_operation["deadline_unix_ms"],
            )
            audit(
                conn,
                p.name,
                "plugin.deploy.rollback",
                f"{operation['node_id']}:{operation['plugin_id']}:{operation_id}"
                f"->{new_operation['operation_id']}",
            )
            return out(operation_proto(conn, new_operation), pb.PluginDeploymentOperation)

    # ── Agent 制品下载：只向持有匹配意图的 Agent 会话开放 ──────────────────

    @app.get("/v1/agent/releases/{release_id}/bundle")
    def download_bundle(
        release_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ):
        token = ""
        if authorization and authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]
        if not token:
            fail(401, "missing_node_session_token")
        shash = hash_token(token)

        with pool.connection() as conn:
            node = one(conn, "SELECT * FROM console_node WHERE session_token_hash=%s", (shash,))
            if not node:
                fail(401, "invalid_node_credentials")
            if node["status"] == "revoked":
                fail(403, "node_revoked")
            release = one(conn, "SELECT * FROM plugin_release WHERE release_id=%s", (release_id,))
            if not release:
                fail(404, "plugin_release_not_found")
            # 只有"手里确实有这个 release 的未完成意图"的节点才能下载。
            entitled = conn.execute(
                """
                SELECT 1 FROM console_deployment_intent
                WHERE node_id=%s AND release_id=%s AND state = ANY(%s)
                LIMIT 1
                """,
                (node["node_id"], release_id, list(ACTIVE_INTENT_STATES)),
            ).fetchone()
            if not entitled:
                audit(
                    conn,
                    node["node_id"],
                    "plugin.release.download_denied",
                    f"{node['node_id']}:{release_id}",
                )
                fail(403, "release_not_entitled_for_this_agent")

        repository = settings.release_repository.resolve()
        bundle_path = (repository / release["bundle_path"]).resolve()
        if not bundle_path.is_relative_to(repository) or not bundle_path.is_file():
            # 描述符在导入时已复算过；这里再判一次，防止制品仓在导入后被替换或移动。
            fail(503, "release_bundle_unavailable")

        return FileResponse(
            bundle_path,
            media_type="application/gzip",
            filename=f"{release_id}.tar.gz",
            headers={"X-Bundle-Digest": release["bundle_digest"]},
        )


def _descriptor_problem(descriptor: dict) -> str:
    """描述符的形状与认证面校验；摘要校验在调用方用落盘字节复算。"""
    required = (
        "release_id",
        "plugin_id",
        "plugin_version",
        "platform",
        "arch",
        "form",
        "artifact_digest",
        "bundle_digest",
        "manifest_digest",
        "config_schema_digest",
        "sbom_digest",
        "bundle_bytes",
        "entrypoint",
        "runtime_requirements",
        "bundle_path",
    )
    missing = [key for key in required if key not in descriptor]
    if missing:
        return f"descriptor_missing_fields:{missing}"
    if descriptor["form"] != "local_native":
        return "unsupported_form"
    if descriptor["platform"] not in {"macos", "linux"}:
        return "unsupported_platform"
    if descriptor["arch"] not in {"aarch64", "x86_64"}:
        return "unsupported_arch"
    # 只允许受控制品仓发布的首方 release；第三方未签名 release 在这里被挡死。
    if descriptor.get("trust") != "first_party" or not descriptor.get("authenticated"):
        return "release_not_first_party_authenticated"
    for field in ("artifact_digest", "bundle_digest"):
        value = str(descriptor.get(field, ""))
        if not value.startswith("sha256:") or len(value) != 71:
            return f"{field}_not_sha256"
    if int(descriptor.get("bundle_bytes", 0)) <= 0:
        return "bundle_bytes_invalid"
    entrypoint = descriptor["entrypoint"]
    if entrypoint.get("transport") != "grpc" or not entrypoint.get("python_module"):
        return "entrypoint_invalid"
    return ""


# ── Agent 回报处理：fencing 校验、阶段推进与蓝绿切换 ────────────────────────


def _fail_operation(conn, operation: dict, code: str, detail: str, candidate_state: str) -> None:
    """失败一律显式落码。切换前失败时槽位 active 指针保持不变。"""
    conn.execute(
        """
        UPDATE plugin_deployment_operation
        SET stage='failed', error_code=%s, error_detail=%s, completed_at=now(), updated_at=now()
        WHERE operation_id=%s AND stage NOT IN ('succeeded','failed','cancelled')
        """,
        (code, detail, operation["operation_id"]),
    )
    conn.execute(
        """
        UPDATE plugin_runtime_instance
        SET role='failed', state=%s, error_code=%s, error_detail=%s, updated_at=now()
        WHERE runtime_instance_id=%s
        """,
        (candidate_state, code, detail, operation["candidate_runtime_instance_id"]),
    )


def apply_hot_report(conn, node_id: str, req, intent: dict) -> dict:
    """处理热部署意图的回报。

    拒绝顺序刻意固定：**先判尸僵（未完成意图状态/操作阶段），再判 fencing（operation/generation），
    最后判截止时间**。任何"过期、重复或旧 generation"的回报都在改状态之前被拒，不会写坏槽位。
    """
    operation = one(
        conn,
        "SELECT * FROM plugin_deployment_operation WHERE operation_id=%s FOR UPDATE",
        (intent["operation_id"],),
    )
    if not operation:
        fail(409, "deployment_operation_missing")

    if intent["state"] not in ACTIVE_INTENT_STATES:
        # 重复投递：同一个意图的第二次回报不是"再执行一次"，而是明确拒绝。
        fail(409, "duplicate_deployment_report")
    if operation["stage"] in TERMINAL_OPERATION_STAGES:
        # 例外：`cancel` 自己下发的"切换前清理"意图（action=stop）必须能被收尾。否则候选进程
        # 已经停了、操作行却永远停在 cancelled 与事实不一致的状态上。
        cleanup_after_cancel = intent["action"] == "stop" and operation["stage"] == "cancelled"
        if not cleanup_after_cancel:
            fail(409, "plugin_deployment_operation_already_closed")
    if operation["node_id"] != node_id:
        fail(409, "deployment_report_node_mismatch")

    # fencing：operation/generation/release/runtime 必须与意图逐字一致，否则是陈旧或伪造回报。
    if req.operation_id != intent["operation_id"]:
        fail(409, "stale_deployment_report")
    if int(req.generation) != int(intent["generation"]):
        fail(409, "fencing_token_mismatch")
    if req.release_id != (intent["release_id"] or ""):
        fail(409, "stale_deployment_report")
    if req.runtime_instance_id != (intent["runtime_instance_id"] or ""):
        fail(409, "stale_deployment_report")

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if now_ms > int(operation["deadline_unix_ms"]):
        _fail_operation(conn, operation, "operation_deadline_exceeded", "deadline passed", "failed")
        conn.execute(
            "UPDATE console_deployment_intent SET state='failed', error_code=%s, "
            "error_detail=%s, completed_at=now() WHERE id=%s",
            ("operation_deadline_exceeded", "deadline passed", intent["id"]),
        )
        audit(
            conn,
            node_id,
            "plugin.deploy.failed",
            f"{operation['operation_id']}:deadline_exceeded",
        )
        return {"status": "rejected", "reason_code": "operation_deadline_exceeded"}

    action = intent["action"]
    if action == "stage_release":
        return _apply_stage_release_report(conn, node_id, req, intent, operation)
    if action == "drain":
        return _apply_drain_report(conn, node_id, req, intent, operation)
    if action == "reconcile":
        conn.execute(
            "UPDATE console_deployment_intent SET state='completed', completed_at=now() "
            "WHERE id=%s",
            (intent["id"],),
        )
        audit(conn, node_id, "plugin.deploy.reconciled", f"{operation['operation_id']}")
        return {"status": "recorded", "reason_code": "reconciliation_recorded"}
    if action == "stop":
        conn.execute(
            "UPDATE plugin_runtime_instance SET state='stopped', updated_at=now() "
            "WHERE runtime_instance_id=%s",
            (intent["runtime_instance_id"],),
        )
        conn.execute(
            "UPDATE console_deployment_intent SET state='completed', completed_at=now() "
            "WHERE id=%s",
            (intent["id"],),
        )
        return {"status": "recorded"}
    fail(409, "unsupported_hot_deployment_action")


def _apply_stage_release_report(conn, node_id, req, intent, operation) -> dict:
    stage = PROTO_TO_STAGE.get(req.stage, "")
    if not req.success:
        _fail_operation(
            conn,
            operation,
            req.error_code or "candidate_start_failed",
            req.error_detail,
            "failed",
        )
        conn.execute(
            "UPDATE console_deployment_intent SET state='failed', error_code=%s, error_detail=%s, "
            "completed_at=now() WHERE id=%s",
            (req.error_code or "candidate_start_failed", req.error_detail, intent["id"]),
        )
        audit(
            conn,
            node_id,
            "plugin.deploy.failed",
            f"{operation['operation_id']}:{req.error_code or 'candidate_start_failed'}",
        )
        return {"status": "recorded", "reason_code": "operation_failed"}

    if stage not in ("staging", "starting", "validating", "candidate_ready"):
        fail(422, "invalid_deployment_stage")

    conn.execute(
        """
        UPDATE plugin_runtime_instance
        SET state=%s, endpoint=%s, supervisor_id=%s, unit_name=%s,
            verified_plugin_id=%s, verified_artifact_digest=%s,
            launch_ms=COALESCE(%s, launch_ms), error_code=NULL, error_detail=NULL, updated_at=now()
        WHERE runtime_instance_id=%s
        """,
        (
            STAGE_TO_RUNTIME_STATE[stage],
            req.endpoint,
            req.supervisor_id,
            req.supervisor_id,
            req.verified_plugin_id,
            req.verified_artifact_digest,
            int(req.starting_ms) or None,
            operation["candidate_runtime_instance_id"],
        ),
    )
    conn.execute(
        """
        UPDATE plugin_deployment_operation
        SET stage=%s, staging_ms=COALESCE(%s, staging_ms), starting_ms=COALESCE(%s, starting_ms),
            validating_ms=COALESCE(%s, validating_ms), updated_at=now()
        WHERE operation_id=%s
        """,
        (
            stage,
            int(req.staging_ms) or None,
            int(req.starting_ms) or None,
            int(req.validating_ms) or None,
            operation["operation_id"],
        ),
    )
    audit(
        conn,
        node_id,
        "plugin.deploy.stage",
        f"{operation['operation_id']}:{stage}",
    )

    if stage != "candidate_ready":
        return {"status": "recorded", "reason_code": f"stage_{stage}"}

    conn.execute(
        "UPDATE console_deployment_intent SET state='completed', completed_at=now() WHERE id=%s",
        (intent["id"],),
    )
    return _cutover(conn, node_id, req, operation)


def _cutover(conn, node_id, req, operation) -> dict:
    """事务 + generation CAS 切换槽位 active 指针；随后对旧实例下发 Drain。"""
    candidate = one(
        conn,
        "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s",
        (operation["candidate_runtime_instance_id"],),
    )
    if not candidate or candidate["state"] != "candidate_ready":
        fail(409, "candidate_not_ready_for_cutover")

    conn.execute(
        "UPDATE plugin_deployment_operation SET stage='cutting_over', updated_at=now() "
        "WHERE operation_id=%s",
        (operation["operation_id"],),
    )
    switched = one(
        conn,
        """
        UPDATE console_plugin_instance
        SET active_runtime_instance_id=%s, active_release_id=%s, previous_runtime_instance_id=%s,
            generation=%s, endpoint=%s, plugin_version=%s, artifact_digest=%s,
            config=%s, config_hash=%s,
            desired_state='ready', actual_state='ready', error_code=NULL, error_detail=NULL,
            updated_at=now()
        WHERE instance_id=%s AND generation=%s
          AND active_runtime_instance_id IS DISTINCT FROM %s
        RETURNING *
        """,
        (
            candidate["runtime_instance_id"],
            candidate["release_id"],
            operation["from_runtime_instance_id"],
            operation["generation"],
            req.endpoint,
            _release_version(conn, candidate["release_id"]),
            candidate["artifact_digest"],
            Jsonb(candidate["config"]),
            candidate["config_hash"],
            operation["instance_id"],
            operation["generation"],
            candidate["runtime_instance_id"],
        ),
    )
    if not switched:
        # 世代已被更新的操作推进，或这个候选已经切过一次：拒绝，不改任何指针。
        fail(409, "generation_cas_failed")

    conn.execute(
        "UPDATE plugin_runtime_instance SET role='active', state='active', updated_at=now() "
        "WHERE runtime_instance_id=%s",
        (candidate["runtime_instance_id"],),
    )
    audit(
        conn,
        node_id,
        "plugin.deploy.cutover",
        f"{operation['operation_id']}:{candidate['runtime_instance_id']}",
    )

    old_runtime_id = operation["from_runtime_instance_id"]
    if not old_runtime_id:
        conn.execute(
            "UPDATE plugin_deployment_operation SET stage='succeeded', completed_at=now(), "
            "updated_at=now() WHERE operation_id=%s",
            (operation["operation_id"],),
        )
        audit(conn, node_id, "plugin.deploy.succeeded", operation["operation_id"])
        return {"status": "recorded", "reason_code": "cutover_succeeded_without_previous"}

    old_runtime = one(
        conn,
        "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s",
        (old_runtime_id,),
    )
    from ..infrastructure.runtime_bindings import runtime_is_pinned

    if runtime_is_pinned(conn, old_runtime):
        conn.execute(
            "UPDATE plugin_runtime_instance SET role='previous',state='active',updated_at=now() "
            "WHERE runtime_instance_id=%s",
            (old_runtime_id,),
        )
        conn.execute(
            "UPDATE plugin_deployment_operation SET stage='succeeded',completed_at=now(), "
            "updated_at=now() WHERE operation_id=%s",
            (operation["operation_id"],),
        )
        audit(conn, node_id, "plugin.deploy.retained_for_revision", old_runtime_id)
        return {"status": "recorded", "reason_code": "cutover_retained_pinned_version"}
    conn.execute(
        "UPDATE plugin_runtime_instance SET role='previous', state='draining', updated_at=now() "
        "WHERE runtime_instance_id=%s",
        (old_runtime_id,),
    )
    old_release = one(
        conn, "SELECT * FROM plugin_release WHERE release_id=%s", (old_runtime["release_id"],)
    )
    grace_ms = min(
        max(int(old_release["default_deadline_ms"]), DRAIN_GRACE_FLOOR_MS), DRAIN_GRACE_CEILING_MS
    )
    drain_deadline_unix_ms = max(
        int(operation["deadline_unix_ms"]),
        int(datetime.now(UTC).timestamp() * 1000) + grace_ms + 60_000,
    )
    insert_intent(
        conn,
        node_id=node_id,
        instance_id=operation["instance_id"],
        action="drain",
        artifact_digest=old_runtime["artifact_digest"],
        config=old_runtime and operation["config"],
        created_by=operation["created_by"],
        operation_id=operation["operation_id"],
        generation=operation["generation"],
        release_id=old_runtime["release_id"],
        bundle_digest=old_runtime["bundle_digest"],
        runtime_instance_id=old_runtime_id,
        grace_period_ms=grace_ms,
        deadline_unix_ms=drain_deadline_unix_ms,
    )
    conn.execute(
        "UPDATE plugin_deployment_operation SET stage='draining_old', "
        "deadline_unix_ms=%s, updated_at=now() WHERE operation_id=%s",
        (drain_deadline_unix_ms, operation["operation_id"]),
    )
    audit(
        conn,
        node_id,
        "plugin.deploy.draining_old",
        f"{operation['operation_id']}:{old_runtime_id}",
    )
    return {"status": "recorded", "reason_code": "cutover_draining_old"}


def _apply_drain_report(conn, node_id, req, intent, operation) -> dict:
    conn.execute(
        "UPDATE console_deployment_intent SET state=%s, error_code=%s, error_detail=%s, "
        "completed_at=now() WHERE id=%s",
        (
            "completed" if req.success else "failed",
            None if req.success else (req.error_code or "drain_failed"),
            None if req.success else req.error_detail,
            intent["id"],
        ),
    )
    if not req.success:
        conn.execute(
            "UPDATE plugin_deployment_operation SET stage='failed', error_code=%s, "
            "error_detail=%s, "
            "draining_ms=COALESCE(%s, draining_ms), completed_at=now(), updated_at=now() "
            "WHERE operation_id=%s AND stage NOT IN ('succeeded','failed','cancelled')",
            (
                req.error_code or "drain_failed",
                req.error_detail,
                int(req.draining_ms) or None,
                operation["operation_id"],
            ),
        )
        conn.execute(
            "UPDATE plugin_runtime_instance SET error_code=%s, error_detail=%s, updated_at=now() "
            "WHERE runtime_instance_id=%s",
            (req.error_code or "drain_failed", req.error_detail, intent["runtime_instance_id"]),
        )
        audit(
            conn,
            node_id,
            "plugin.deploy.failed",
            f"{operation['operation_id']}:{req.error_code or 'drain_failed'}",
        )
        return {"status": "recorded", "reason_code": "drain_failed"}

    conn.execute(
        "UPDATE plugin_runtime_instance SET state='stopped', drain_ms=%s, error_code=NULL, "
        "error_detail=NULL, updated_at=now() WHERE runtime_instance_id=%s",
        (int(req.draining_ms) or None, intent["runtime_instance_id"]),
    )
    conn.execute(
        "UPDATE plugin_deployment_operation SET stage='succeeded', draining_ms=%s, "
        "completed_at=now(), updated_at=now() WHERE operation_id=%s "
        "AND stage NOT IN ('succeeded','failed','cancelled')",
        (int(req.draining_ms) or None, operation["operation_id"]),
    )
    audit(conn, node_id, "plugin.deploy.succeeded", operation["operation_id"])
    return {"status": "recorded", "reason_code": "operation_succeeded"}


def _release_version(conn, release_id: str) -> str:
    row = one(conn, "SELECT plugin_version FROM plugin_release WHERE release_id=%s", (release_id,))
    return row["plugin_version"] if row else ""


def record_runtime_observations(conn, node_id: str, observations) -> list[dict]:
    """Agent 重启对账：只按真实观测更新，未知状态报告 `reconciliation_required`。"""
    results = []
    for observation in observations:
        runtime = one(
            conn,
            "SELECT * FROM plugin_runtime_instance WHERE runtime_instance_id=%s",
            (observation.runtime_instance_id,),
        )
        if not runtime or runtime["node_id"] != node_id:
            results.append(
                {
                    "runtime_instance_id": observation.runtime_instance_id,
                    "reconciliation": "unknown",
                }
            )
            continue
        if observation.reconciliation == "matched":
            if observation.endpoint and observation.endpoint != runtime["endpoint"]:
                conn.execute(
                    "UPDATE plugin_runtime_instance SET endpoint=%s, supervisor_id=%s, "
                    "unit_name=%s, "
                    "error_code=NULL, updated_at=now() WHERE runtime_instance_id=%s",
                    (
                        observation.endpoint,
                        observation.supervisor_id,
                        observation.supervisor_id,
                        runtime["runtime_instance_id"],
                    ),
                )
            results.append(
                {
                    "runtime_instance_id": observation.runtime_instance_id,
                    "reconciliation": "matched",
                }
            )
            continue

        # 未知/不一致：不猜成功、不动 role/state、不删制品，只把事实记下来。
        conn.execute(
            "UPDATE plugin_runtime_instance SET error_code='reconciliation_required', "
            "error_detail=%s, updated_at=now() WHERE runtime_instance_id=%s",
            (
                observation.detail or observation.observed_state or "unknown",
                runtime["runtime_instance_id"],
            ),
        )
        if runtime["state"] != "stopped":
            conn.execute(
                "UPDATE plugin_deployment_operation SET error_code='reconciliation_required', "
                "error_detail=%s, updated_at=now() WHERE candidate_runtime_instance_id=%s "
                "AND stage NOT IN ('succeeded','failed','cancelled')",
                (
                    observation.detail or observation.observed_state or "unknown",
                    runtime["runtime_instance_id"],
                ),
            )
        audit(
            conn,
            node_id,
            "plugin.deploy.reconciliation_required",
            f"{runtime['runtime_instance_id']}:{observation.detail or observation.observed_state}",
        )
        results.append(
            {
                "runtime_instance_id": observation.runtime_instance_id,
                "reconciliation": "reconciliation_required",
            }
        )
    return results

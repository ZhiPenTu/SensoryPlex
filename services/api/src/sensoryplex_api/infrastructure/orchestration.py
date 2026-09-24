"""ADR-029 P1 可编排插件执行核心：不可变 Revision、DAG 编译、Run/Task 状态机、受控调度与租约恢复。

本模块是执行编排的事实权威：
- 严格校验图结构、modality 契约、placement（same_item 硬约束同机数据面）与无环；
- 不可变 revision 固化 graph_digest 与定义；
- PostgreSQL 事务原子推进状态，支持幂等提交、取消传播、有界重试与崩溃租约回收；
- 绝不使用时钟或 delivery ID 替代业务幂等键，已取消 Run 绝不解锁新 Task。
"""

import hashlib
import json
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from ..contracts import audit, fail, identifier, one, rows

MAX_GRAPH_NODES = 128
MAX_GRAPH_EDGES = 1024
MAX_ATTEMPTS = 16
VALID_PLACEMENTS = {"data_plane_local", "object_ref_allowed"}
VALID_JOINS = {"same_item", "same_stream_window", "window_contains"}


def validate_and_normalize_graph(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> tuple[bool, list[str], str, list[str], dict[str, Any]]:
    """校验 DAG 结构、modality 生产消费契约、placement 本地性与拓扑无环性。"""
    errors: list[str] = []
    if not nodes or len(nodes) > MAX_GRAPH_NODES:
        return False, ["invalid_orchestration_node_count"], "", [], {}
    if len(edges) > MAX_GRAPH_EDGES:
        return False, ["invalid_orchestration_edge_count"], "", [], {}

    node_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node.get("id", "")).strip()
        plugin_id = str(node.get("plugin_id", "")).strip()
        if not node_id or not plugin_id:
            errors.append("invalid_orchestration_node")
            continue
        if node_id in node_map:
            errors.append("duplicate_orchestration_node_id")
            continue

        deadline_ms = int(node.get("deadline_ms", 0))
        if deadline_ms <= 0:
            errors.append("invalid_orchestration_node_deadline")
            continue

        max_attempts = int(node.get("max_attempts", 1))
        if max_attempts <= 0 or max_attempts > MAX_ATTEMPTS:
            errors.append("invalid_orchestration_attempt_budget")
            continue

        placement = str(node.get("placement", "data_plane_local")).strip().lower()
        if placement not in VALID_PLACEMENTS:
            errors.append("invalid_orchestration_placement")
            continue

        consumes = [str(m).strip() for m in node.get("consumes", []) if str(m).strip()]
        produces = [str(m).strip() for m in node.get("produces", []) if str(m).strip()]
        if len(set(consumes)) != len(consumes) or len(set(produces)) != len(produces):
            errors.append("duplicate_orchestration_modality")
            continue

        node_map[node_id] = {
            "id": node_id,
            "plugin_id": plugin_id,
            "consumes": sorted(consumes),
            "produces": sorted(produces),
            "placement": placement,
            "deadline_ms": deadline_ms,
            "max_attempts": max_attempts,
            "priority": int(node.get("priority", 0)),
        }

    if errors:
        return False, errors, "", [], {}

    in_degree: dict[str, int] = {nid: 0 for nid in node_map}
    outgoing: dict[str, list[str]] = defaultdict(list)
    normalized_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    for edge in edges:
        from_id = str(edge.get("from", edge.get("from_node_id", ""))).strip()
        to_id = str(edge.get("to", edge.get("to_node_id", ""))).strip()
        modality = str(edge.get("modality", "")).strip()
        join_policy = str(edge.get("join", edge.get("join_policy", "same_item"))).strip().lower()
        required = bool(edge.get("required", True))

        if from_id not in node_map:
            errors.append("orchestration_edge_from_not_found")
            continue
        if to_id not in node_map:
            errors.append("orchestration_edge_to_not_found")
            continue
        if from_id == to_id or not modality:
            errors.append("invalid_orchestration_edge")
            continue

        from_node = node_map[from_id]
        to_node = node_map[to_id]
        if modality not in from_node["produces"]:
            errors.append("orchestration_edge_modality_not_produced")
            continue
        if modality not in to_node["consumes"]:
            errors.append("orchestration_edge_modality_not_consumed")
            continue
        if join_policy not in VALID_JOINS:
            errors.append("invalid_orchestration_join_policy")
            continue

        # 硬约束：同一原始 BufferDescriptor 边（same_item）必须且只能在同一数据面节点
        if join_policy == "same_item" and (
            from_node["placement"] != "data_plane_local"
            or to_node["placement"] != "data_plane_local"
        ):
            errors.append("same_item_requires_data_plane_local")
            continue

        edge_key = (from_id, to_id, modality, join_policy)
        if edge_key in seen_edges:
            errors.append("duplicate_orchestration_edge")
            continue
        seen_edges.add(edge_key)

        in_degree[to_id] += 1
        outgoing[from_id].append(to_id)
        normalized_edges.append(
            {
                "from_node_id": from_id,
                "to_node_id": to_id,
                "modality": modality,
                "join_policy": join_policy,
                "required": required,
            }
        )

    if errors:
        return False, errors, "", [], {}

    # Kahn 算法拓扑排序与环检测
    queue = [nid for nid, deg in sorted(in_degree.items()) if deg == 0]
    topological_order: list[str] = []
    current_degrees = dict(in_degree)

    while queue:
        current = queue.pop(0)
        topological_order.append(current)
        for neighbor in sorted(outgoing.get(current, [])):
            current_degrees[neighbor] -= 1
            if current_degrees[neighbor] == 0:
                queue.append(neighbor)

    if len(topological_order) != len(node_map):
        return False, ["orchestration_cycle_detected"], "", [], {}

    normalized_nodes = [node_map[nid] for nid in sorted(node_map)]
    normalized_edges.sort(
        key=lambda e: (
            e["from_node_id"],
            e["to_node_id"],
            e["modality"],
            e["join_policy"],
        )
    )

    canonical_payload = {
        "nodes": normalized_nodes,
        "edges": normalized_edges,
    }
    digest_bytes = hashlib.sha256(
        json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    graph_digest = f"sha256:{digest_bytes}"

    return True, [], graph_digest, topological_order, canonical_payload


def publish_pipeline_revision(
    conn,
    owner: str,
    pipeline_id: str,
    name: str,
    description: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """发布不可变 Pipeline Revision。图结构通过严格 DAG 编译后落库，拒绝后续就地修改。"""
    valid, errors, graph_digest, topo, graph_json = validate_and_normalize_graph(nodes, edges)
    if not valid:
        fail(422, errors[0])

    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"pipeline:{pipeline_id}",)
    )

    # 确保主定义存在
    conn.execute(
        """
        INSERT INTO pipeline_definition (pipeline_id, name, description, owner)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (pipeline_id) DO UPDATE
        SET name = EXCLUDED.name, description = EXCLUDED.description
        """,
        (pipeline_id, name, description, owner),
    )

    # 获取下一版本号
    rev_row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 FROM pipeline_revision WHERE pipeline_id=%s",
        (pipeline_id,),
    ).fetchone()
    next_revision = rev_row[0] if rev_row else 1

    conn.execute(
        """
        INSERT INTO pipeline_revision (
            pipeline_id, revision, graph_digest, definition_json, created_by
        )
        VALUES (%s, %s, %s, %s, %s)
        """,
        (pipeline_id, next_revision, graph_digest, Jsonb(graph_json), owner),
    )

    audit(conn, owner, "pipeline.revision.publish", f"{pipeline_id}:v{next_revision}")

    return {
        "pipeline_id": pipeline_id,
        "revision": next_revision,
        "graph_digest": graph_digest,
        "nodes": graph_json["nodes"],
        "edges": graph_json["edges"],
    }


def submit_pipeline_run(
    conn,
    owner: str,
    pipeline_id: str,
    revision: int,
    input_ref: str,
    idempotency_key: str,
    deadline_unix_ms: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """提交 Pipeline Run。通过条件唯一索引与行锁确保严格幂等性，初次提交原子实例化所有 Task。"""
    if not idempotency_key:
        fail(422, "missing_idempotency_key")
    if not input_ref:
        fail(422, "missing_input_ref")

    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"run_sub:{pipeline_id}:{revision}:{idempotency_key}",),
    )

    rev_record = one(
        conn,
        "SELECT * FROM pipeline_revision WHERE pipeline_id=%s AND revision=%s",
        (pipeline_id, revision),
    )
    if not rev_record:
        fail(404, "pipeline_revision_not_found")

    # 幂等检查：活跃状态的同参运行直接返回
    existing_run = one(
        conn,
        """
        SELECT * FROM pipeline_run
        WHERE pipeline_id=%s AND revision=%s AND input_ref=%s AND idempotency_key=%s
          AND state IN ('accepted', 'validating', 'queued', 'running')
        """,
        (pipeline_id, revision, input_ref, idempotency_key),
    )
    if existing_run:
        existing_tasks = rows(
            conn,
            "SELECT * FROM pipeline_task WHERE run_id=%s ORDER BY node_id ASC",
            (existing_run["run_id"],),
        )
        return existing_run, existing_tasks, True

    run_id = identifier("run")
    if deadline_unix_ms <= 0:
        deadline_unix_ms = int(time.time() * 1000) + 3600_000  # 默认 1 小时超时

    # 插入 Run
    conn.execute(
        """
        INSERT INTO pipeline_run (
            run_id, pipeline_id, revision, input_ref, idempotency_key,
            deadline_unix_ms, state, owner
        ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s)
        """,
        (
            run_id,
            pipeline_id,
            revision,
            input_ref,
            idempotency_key,
            deadline_unix_ms,
            owner,
        ),
    )

    graph_json = rev_record["definition_json"]
    nodes = graph_json.get("nodes", [])
    edges = graph_json.get("edges", [])

    # 计算入度以确认初始状态
    in_degrees: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in edges:
        in_degrees[e["to_node_id"]] = in_degrees.get(e["to_node_id"], 0) + 1

    created_tasks: list[dict[str, Any]] = []
    for n in nodes:
        node_id = n["id"]
        task_id = identifier("task")
        initial_state = "ready" if in_degrees[node_id] == 0 else "pending"
        task_idemp = f"{run_id}:{node_id}"

        conn.execute(
            """
            INSERT INTO pipeline_task (
                task_id, run_id, node_id, attempt, max_attempts,
                idempotency_key, state
            ) VALUES (%s, %s, %s, 0, %s, %s, %s)
            """,
            (
                task_id,
                run_id,
                node_id,
                n.get("max_attempts", 1),
                task_idemp,
                initial_state,
            ),
        )
        created_tasks.append(
            {
                "task_id": task_id,
                "run_id": run_id,
                "node_id": node_id,
                "attempt": 0,
                "max_attempts": n.get("max_attempts", 1),
                "idempotency_key": task_idemp,
                "state": initial_state,
            }
        )

    for e in edges:
        conn.execute(
            """
            INSERT INTO pipeline_task_edge (
                run_id, from_node_id, to_node_id, modality, join_policy, required
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                e["from_node_id"],
                e["to_node_id"],
                e["modality"],
                e["join_policy"],
                e.get("required", True),
            ),
        )

    audit(conn, owner, "pipeline.run.submit", run_id)

    run_record = one(conn, "SELECT * FROM pipeline_run WHERE run_id=%s", (run_id,))
    return run_record, created_tasks, False


def get_pipeline_run(conn, run_id: str) -> dict[str, Any]:
    """获取 Run 详情、所有任务快照及调度租约列表。"""
    run_record = one(conn, "SELECT * FROM pipeline_run WHERE run_id=%s", (run_id,))
    if not run_record:
        fail(404, "run_not_found")

    tasks = rows(
        conn,
        "SELECT * FROM pipeline_task WHERE run_id=%s ORDER BY created_at ASC",
        (run_id,),
    )
    assignments = rows(
        conn,
        "SELECT * FROM scheduler_assignment WHERE run_id=%s ORDER BY created_at ASC",
        (run_id,),
    )
    return {
        "run": run_record,
        "tasks": tasks,
        "assignments": assignments,
    }


def cancel_pipeline_run(
    conn, run_id: str, reason: str = "user_requested", actor: str = "system"
) -> dict[str, Any]:
    """取消 Pipeline Run。未开始任务直接终态，已分配任务触发取消，已取消 Run 不再解锁任何新任务。"""
    run_record = one(
        conn,
        "SELECT * FROM pipeline_run WHERE run_id=%s FOR UPDATE",
        (run_id,),
    )
    if not run_record:
        fail(404, "run_not_found")

    if run_record["state"] in ("succeeded", "failed", "cancelled", "expired"):
        tasks = rows(conn, "SELECT * FROM pipeline_task WHERE run_id=%s", (run_id,))
        return {"run": run_record, "cancelled_tasks": tasks}

    conn.execute(
        """
        UPDATE pipeline_run
        SET state='cancelled', error_code='cancelled', error_detail=%s,
            completed_at=now(), updated_at=now()
        WHERE run_id=%s
        """,
        (reason, run_id),
    )

    cancelled_tasks = rows(
        conn,
        """
        UPDATE pipeline_task
        SET state='cancelled', reason_code='run_cancelled', updated_at=now()
        WHERE run_id=%s AND state NOT IN ('succeeded', 'failed', 'blocked', 'cancelled')
        RETURNING *
        """,
        (run_id,),
    )

    conn.execute(
        """
        UPDATE scheduler_assignment
        SET decision='cancelled', reason_code='run_cancelled'
        WHERE run_id=%s AND decision='assigned'
        """,
        (run_id,),
    )

    audit(conn, actor, "pipeline.run.cancel", run_id)
    updated_run = one(conn, "SELECT * FROM pipeline_run WHERE run_id=%s", (run_id,))
    return {"run": updated_run, "cancelled_tasks": cancelled_tasks}


def schedule_ready_tasks(
    conn,
    candidate_node_id: str = "local-node",
    is_co_located: bool = True,
    max_tasks: int = 10,
) -> list[dict[str, Any]]:
    """调度扫描：基于数据本地性与资源上限派发就绪任务，签发租约。"""
    ready_tasks = rows(
        conn,
        """
        SELECT t.*, r.pipeline_id, r.revision, r.deadline_unix_ms AS run_deadline_ms
        FROM pipeline_task t
        JOIN pipeline_run r ON r.run_id = t.run_id
        WHERE t.state = 'ready' AND r.state IN ('running', 'queued')
        ORDER BY t.created_at ASC
        LIMIT %s
        FOR UPDATE OF t SKIP LOCKED
        """,
        (max_tasks,),
    )

    assigned: list[dict[str, Any]] = []
    for task in ready_tasks:
        rev_record = one(
            conn,
            "SELECT definition_json FROM pipeline_revision WHERE pipeline_id=%s AND revision=%s",
            (task["pipeline_id"], task["revision"]),
        )
        if not rev_record:
            continue

        nodes_spec = rev_record["definition_json"].get("nodes", [])
        node_spec = next((n for n in nodes_spec if n["id"] == task["node_id"]), None)
        if not node_spec:
            continue

        placement = node_spec.get("placement", "data_plane_local")
        deadline_ms = int(node_spec.get("deadline_ms", 30000))

        # 数据本地性硬约束：若为 raw buffer (data_plane_local) 但候选节点非同机，显式拒绝派发
        if placement == "data_plane_local" and not is_co_located:
            asgn_id = identifier("asgn_rej")
            conn.execute(
                """
                INSERT INTO scheduler_assignment (
                    assignment_id, task_id, run_id, attempt, requested_node_id,
                    actual_node_id, data_plane_node_id, decision, reason_code
                ) VALUES (
                    %s, %s, %s, %s, %s, NULL,
                    'local-data-plane', 'rejected', 'data_locality_violation'
                )
                """,
                (
                    asgn_id,
                    task["task_id"],
                    task["run_id"],
                    task["attempt"] + 1,
                    candidate_node_id,
                ),
            )
            continue

        asgn_id = identifier("asgn")
        new_attempt = task["attempt"] + 1
        lease_expires_at = datetime.now(UTC) + timedelta(milliseconds=deadline_ms + 15000)

        conn.execute(
            """
            UPDATE pipeline_task
            SET state='assigned', attempt=%s, assignment_id=%s, updated_at=now()
            WHERE task_id=%s
            """,
            (new_attempt, asgn_id, task["task_id"]),
        )

        conn.execute(
            """
            INSERT INTO scheduler_assignment (
                assignment_id, task_id, run_id, attempt, requested_node_id,
                actual_node_id, data_plane_node_id, decision, reason_code, lease_expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'assigned', '', %s)
            """,
            (
                asgn_id,
                task["task_id"],
                task["run_id"],
                new_attempt,
                candidate_node_id,
                candidate_node_id,
                "local-data-plane" if is_co_located else "remote",
                lease_expires_at,
            ),
        )

        assigned.append(
            {
                "task_id": task["task_id"],
                "run_id": task["run_id"],
                "node_id": task["node_id"],
                "attempt": new_attempt,
                "assignment_id": asgn_id,
                "plugin_id": node_spec["plugin_id"],
                "deadline_ms": deadline_ms,
                "placement": placement,
            }
        )

    return assigned


def report_task_result(
    conn,
    task_id: str,
    run_id: str,
    attempt: int,
    assignment_id: str,
    success: bool,
    output_ref: str = "",
    retryable: bool = False,
    reason_code: str = "",
    error_detail: str = "",
) -> dict[str, Any]:
    """处理任务结果上报。严格对账 attempt/assignment，
    处理成功解锁、有界重试、失败阻断及取消后结果丢弃。"""
    task = one(
        conn,
        "SELECT * FROM pipeline_task WHERE task_id=%s FOR UPDATE",
        (task_id,),
    )
    if not task:
        fail(404, "task_not_found")

    # 结果归属不变式：签名必须完全一致
    if (
        task["run_id"] != run_id
        or task["attempt"] != attempt
        or task["assignment_id"] != assignment_id
    ):
        audit(
            conn,
            "worker",
            "task.stale_result_rejected",
            f"{task_id}:att{attempt}:asgn{assignment_id}",
        )
        fail(409, "stale_task_result")

    # 取消优先不变式：已取消的任务不论返回什么，绝不改写为成功，绝不解锁下游
    if task["state"] == "cancelled":
        audit(
            conn,
            "worker",
            "task.discarded_late_result",
            f"{task_id}:att{attempt}:asgn{assignment_id}",
        )
        return {
            "task": task,
            "unlocked_task_ids": [],
            "discarded": True,
            "reason": "task_already_cancelled",
        }

    unlocked_task_ids: list[str] = []

    if success:
        conn.execute(
            """
            UPDATE pipeline_task
            SET state='succeeded', output_ref=%s, reason_code=NULL,
                error_detail=NULL, updated_at=now()
            WHERE task_id=%s
            """,
            (output_ref, task_id),
        )

        # 解锁下游任务：当某后继任务的所有必需前驱均 succeeded 时，将其从 pending 置为 ready
        outgoing_edges = rows(
            conn,
            "SELECT to_node_id FROM pipeline_task_edge WHERE run_id=%s AND from_node_id=%s",
            (run_id, task["node_id"]),
        )
        for edge in outgoing_edges:
            child_node_id = edge["to_node_id"]
            # 检查 child 的所有 required 父任务
            unmet_parents = rows(
                conn,
                """
                SELECT p.task_id, p.state
                FROM pipeline_task_edge e
                JOIN pipeline_task p ON p.run_id = e.run_id AND p.node_id = e.from_node_id
                WHERE e.run_id=%s AND e.to_node_id=%s AND e.required=true AND p.state != 'succeeded'
                """,
                (run_id, child_node_id),
            )
            if not unmet_parents:
                unlocked = one(
                    conn,
                    """
                    UPDATE pipeline_task
                    SET state='ready', updated_at=now()
                    WHERE run_id=%s AND node_id=%s AND state='pending'
                    RETURNING task_id
                    """,
                    (run_id, child_node_id),
                )
                if unlocked:
                    unlocked_task_ids.append(unlocked["task_id"])

        # 检查 Run 是否整体成功
        remaining = conn.execute(
            "SELECT count(*) FROM pipeline_task WHERE run_id=%s AND state != 'succeeded'",
            (run_id,),
        ).fetchone()[0]

        if remaining == 0:
            conn.execute(
                """
                UPDATE pipeline_run
                SET state='succeeded', completed_at=now(), updated_at=now()
                WHERE run_id=%s
                """,
                (run_id,),
            )

        updated_task = one(conn, "SELECT * FROM pipeline_task WHERE task_id=%s", (task_id,))
        return {
            "task": updated_task,
            "unlocked_task_ids": unlocked_task_ids,
            "run_completed": remaining == 0,
        }

    else:
        # 失败处理：有上限重试或阻断失败
        if retryable and task["attempt"] < task["max_attempts"]:
            conn.execute(
                """
                UPDATE pipeline_task
                SET state='retry_wait', reason_code=%s, error_detail=%s, updated_at=now()
                WHERE task_id=%s
                """,
                (reason_code or "transient_error", error_detail, task_id),
            )
            updated_task = one(conn, "SELECT * FROM pipeline_task WHERE task_id=%s", (task_id,))
            return {
                "task": updated_task,
                "unlocked_task_ids": [],
                "retry_scheduled": True,
            }
        else:
            final_reason = (
                f"retry_exhausted:{reason_code}"
                if task["attempt"] >= task["max_attempts"]
                else (reason_code or "task_failed")
            )
            conn.execute(
                """
                UPDATE pipeline_task
                SET state='failed', reason_code=%s, error_detail=%s, updated_at=now()
                WHERE task_id=%s
                """,
                (final_reason, error_detail, task_id),
            )

            # 必需上游失败，递归级联阻塞所有下游 pending 任务
            conn.execute(
                """
                WITH RECURSIVE descendants AS (
                    SELECT to_node_id FROM pipeline_task_edge
                    WHERE run_id=%s AND from_node_id=%s AND required=true
                    UNION
                    SELECT e.to_node_id FROM pipeline_task_edge e
                    JOIN descendants d ON e.from_node_id = d.to_node_id
                    WHERE e.run_id=%s AND e.required=true
                )
                UPDATE pipeline_task
                SET state='blocked', reason_code='upstream_failed', updated_at=now()
                WHERE run_id=%s AND state='pending'
                  AND node_id IN (SELECT to_node_id FROM descendants)
                """,
                (run_id, task["node_id"], run_id, run_id),
            )

            conn.execute(
                """
                UPDATE pipeline_run
                SET state='failed', error_code=%s, error_detail=%s,
                    completed_at=now(), updated_at=now()
                WHERE run_id=%s
                """,
                (final_reason, error_detail, run_id),
            )

            updated_task = one(conn, "SELECT * FROM pipeline_task WHERE task_id=%s", (task_id,))
            return {
                "task": updated_task,
                "unlocked_task_ids": [],
                "run_failed": True,
                "reason_code": final_reason,
            }


def release_retry_wait_tasks(conn) -> list[str]:
    """将处于 retry_wait 的任务刷新回 ready 状态，供调度器重新认领。"""
    released = rows(
        conn,
        """
        UPDATE pipeline_task
        SET state='ready', updated_at=now()
        WHERE state='retry_wait'
        RETURNING task_id
        """,
    )
    return [r["task_id"] for r in released]


def reconcile_and_recover_leases(conn) -> dict[str, Any]:
    """崩溃恢复器：扫描已过期的调度租约，收敛已确认事实，其余按重试预算回收或失败。"""
    expired_assignments = rows(
        conn,
        """
        SELECT a.assignment_id, a.task_id, a.run_id, a.attempt,
               t.state AS task_state, t.attempt AS current_attempt,
               t.max_attempts, t.node_id, t.output_ref
        FROM scheduler_assignment a
        JOIN pipeline_task t ON t.task_id = a.task_id
        WHERE a.decision = 'assigned' AND a.lease_expires_at < now()
        FOR UPDATE OF a, t
        """,
    )

    recovered: list[str] = []
    failed: list[str] = []

    for item in expired_assignments:
        conn.execute(
            """
            UPDATE scheduler_assignment
            SET decision='lease_expired', reason_code='lease_expired'
            WHERE assignment_id=%s
            """,
            (item["assignment_id"],),
        )

        # 仅处理仍停留在 assigned / running 的任务
        if item["task_state"] in ("assigned", "running"):
            if item["output_ref"]:
                # 如果事实已写入，收敛为成功
                conn.execute(
                    """
                    UPDATE pipeline_task
                    SET state='succeeded', reason_code=NULL, updated_at=now()
                    WHERE task_id=%s
                    """,
                    (item["task_id"],),
                )
                recovered.append(item["task_id"])
            elif item["current_attempt"] < item["max_attempts"]:
                # 仍有尝试预算，重新入队 ready 等待调度
                conn.execute(
                    """
                    UPDATE pipeline_task
                    SET state='ready', reason_code='lease_expired_recovered', updated_at=now()
                    WHERE task_id=%s
                    """,
                    (item["task_id"],),
                )
                recovered.append(item["task_id"])
                audit(conn, "scheduler", "task.lease_recovery_retry", item["task_id"])
            else:
                # 尝试预算耗尽，置为失败并阻断下游
                final_reason = "lease_expired_exhausted"
                conn.execute(
                    """
                    UPDATE pipeline_task
                    SET state='failed', reason_code=%s, updated_at=now()
                    WHERE task_id=%s
                    """,
                    (final_reason, item["task_id"]),
                )
                conn.execute(
                    """
                    WITH RECURSIVE descendants AS (
                        SELECT to_node_id FROM pipeline_task_edge
                        WHERE run_id=%s AND from_node_id=%s AND required=true
                        UNION
                        SELECT e.to_node_id FROM pipeline_task_edge e
                        JOIN descendants d ON e.from_node_id = d.to_node_id
                        WHERE e.run_id=%s AND e.required=true
                    )
                    UPDATE pipeline_task
                    SET state='blocked', reason_code='upstream_failed', updated_at=now()
                    WHERE run_id=%s AND state='pending'
                  AND node_id IN (SELECT to_node_id FROM descendants)
                    """,
                    (item["run_id"], item["node_id"], item["run_id"], item["run_id"]),
                )
                conn.execute(
                    """
                    UPDATE pipeline_run
                    SET state='failed', error_code=%s, completed_at=now(), updated_at=now()
                    WHERE run_id=%s
                    """,
                    (final_reason, item["run_id"]),
                )
                failed.append(item["task_id"])
                audit(conn, "scheduler", "task.lease_recovery_exhausted", item["task_id"])

    return {
        "expired_assignments_count": len(expired_assignments),
        "recovered_tasks": recovered,
        "failed_tasks": failed,
    }

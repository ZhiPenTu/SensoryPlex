"""ADR-029 P1 可编排插件执行核心 API 路由。

提供 Pipeline 发布/校验、Run 提交、状态查询、取消传播、重试与调度驱动接口。
由 RBAC 严格保护：
- pipelines:manage: 方案校验与发布
- jobs:write: Run 提交、取消与任务结果上报
- jobs:read: Run 与任务状态详情查询
"""

from typing import Annotated, Any

from fastapi import Body, Depends

from ..contracts import fail, one, rows, text_field
from ..infrastructure import orchestration as orch


def _format_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "pipeline_id": run["pipeline_id"],
        "revision": int(run["revision"]),
        "input_ref": run["input_ref"],
        "idempotency_key": run["idempotency_key"],
        "deadline_unix_ms": int(run["deadline_unix_ms"]),
        "state": f"PIPELINE_RUN_STATE_{run['state'].upper()}",
        "owner": run.get("owner", ""),
        "error_code": run.get("error_code") or "",
        "error_detail": run.get("error_detail") or "",
        "created_at_unix_ms": int(run["created_at"].timestamp() * 1000)
        if run.get("created_at")
        else 0,
        "updated_at_unix_ms": int(run["updated_at"].timestamp() * 1000)
        if run.get("updated_at")
        else 0,
        "completed_at_unix_ms": int(run["completed_at"].timestamp() * 1000)
        if run.get("completed_at")
        else 0,
    }


def _format_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "run_id": task["run_id"],
        "node_id": task["node_id"],
        "attempt": int(task["attempt"]),
        "max_attempts": int(task.get("max_attempts", 1)),
        "idempotency_key": task["idempotency_key"],
        "state": f"PIPELINE_TASK_STATE_{task['state'].upper()}",
        "assignment_id": task.get("assignment_id") or "",
        "reason_code": task.get("reason_code") or "",
        "error_detail": task.get("error_detail") or "",
        "output_ref": task.get("output_ref") or "",
        "created_at_unix_ms": int(task["created_at"].timestamp() * 1000)
        if task.get("created_at")
        else 0,
        "updated_at_unix_ms": int(task["updated_at"].timestamp() * 1000)
        if task.get("updated_at")
        else 0,
    }


def _format_assignment(asgn: dict[str, Any]) -> dict[str, Any]:
    return {
        "assignment_id": asgn["assignment_id"],
        "task_id": asgn["task_id"],
        "run_id": asgn["run_id"],
        "attempt": int(asgn["attempt"]),
        "requested_node_id": asgn.get("requested_node_id") or "",
        "actual_node_id": asgn.get("actual_node_id") or "",
        "data_plane_node_id": asgn.get("data_plane_node_id") or "",
        "decision": asgn["decision"],
        "reason_code": asgn.get("reason_code") or "",
        "lease_expires_at_unix_ms": int(asgn["lease_expires_at"].timestamp() * 1000)
        if asgn.get("lease_expires_at")
        else 0,
        "created_at_unix_ms": int(asgn["created_at"].timestamp() * 1000)
        if asgn.get("created_at")
        else 0,
    }


def register(app, pool, auth, settings):
    @app.post("/v1/orchestration/pipelines:validate")
    def validate_pipeline(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        nodes = body.get("nodes", [])
        edges = body.get("edges", [])
        valid, errors, digest, topo, _ = orch.validate_and_normalize_graph(nodes, edges)
        return {
            "valid": valid,
            "errors": errors,
            "graph_digest": digest,
            "topological_order": topo,
        }

    @app.post("/v1/orchestration/pipelines")
    def publish_pipeline(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("pipelines:manage"))] = None,
    ):
        pipeline_id = text_field(body.get("pipeline_id", ""), 64)
        name = text_field(body.get("name", pipeline_id), 120)
        description = body.get("description", "")
        nodes = body.get("nodes", [])
        edges = body.get("edges", [])

        with pool.connection() as conn:
            result = orch.publish_pipeline_revision(
                conn,
                owner=p.name,
                pipeline_id=pipeline_id,
                name=name,
                description=description,
                nodes=nodes,
                edges=edges,
            )
        return {"revision": result}

    @app.get("/v1/orchestration/pipelines/{pipeline_id}")
    def get_pipeline_revisions(
        pipeline_id: str,
        p: Annotated[object, Depends(auth.require("jobs:read"))] = None,
    ):
        with pool.connection() as conn:
            definition = one(
                conn,
                "SELECT * FROM pipeline_definition WHERE pipeline_id=%s",
                (pipeline_id,),
            )
            if not definition:
                fail(404, "pipeline_not_found")
            revs = rows(
                conn,
                "SELECT pipeline_id, revision, graph_digest, definition_json, "
                "created_by, created_at "
                "FROM pipeline_revision WHERE pipeline_id=%s ORDER BY revision DESC",
                (pipeline_id,),
            )
        return {
            "pipeline": definition,
            "revisions": [
                {
                    "pipeline_id": r["pipeline_id"],
                    "revision": r["revision"],
                    "graph_digest": r["graph_digest"],
                    "nodes": r["definition_json"].get("nodes", []),
                    "edges": r["definition_json"].get("edges", []),
                    "created_by": r["created_by"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in revs
            ],
        }

    @app.post("/v1/orchestration/runs", status_code=201)
    def submit_run(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        pipeline_id = text_field(body.get("pipeline_id", ""), 64)
        revision = int(body.get("revision", 1))
        input_ref = body.get("input_ref", "")
        idempotency_key = body.get("idempotency_key", "")
        deadline_unix_ms = int(body.get("deadline_unix_ms", 0))

        with pool.connection() as conn:
            run_rec, task_recs, is_duplicate = orch.submit_pipeline_run(
                conn,
                owner=p.name,
                pipeline_id=pipeline_id,
                revision=revision,
                input_ref=input_ref,
                idempotency_key=idempotency_key,
                deadline_unix_ms=deadline_unix_ms,
            )
        return {
            "run": _format_run(run_rec),
            "tasks": [_format_task(t) for t in task_recs],
            "is_duplicate": is_duplicate,
        }

    @app.get("/v1/orchestration/runs/{run_id}")
    def get_run(
        run_id: str,
        p: Annotated[object, Depends(auth.require("jobs:read"))] = None,
    ):
        with pool.connection() as conn:
            res = orch.get_pipeline_run(conn, run_id)
        return {
            "run": _format_run(res["run"]),
            "tasks": [_format_task(t) for t in res["tasks"]],
            "assignments": [_format_assignment(a) for a in res["assignments"]],
        }

    @app.post("/v1/orchestration/runs/{run_id}:cancel")
    def cancel_run(
        run_id: str,
        body: Annotated[dict | None, Body()] = None,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        reason = (
            body.get("reason", "user_requested") if isinstance(body, dict) else "user_requested"
        )
        with pool.connection() as conn:
            res = orch.cancel_pipeline_run(conn, run_id, reason=reason, actor=p.name)
        return {
            "run": _format_run(res["run"]),
            "cancelled_tasks": [_format_task(t) for t in res["cancelled_tasks"]],
        }

    @app.post("/v1/orchestration/tasks:claim")
    def claim_tasks(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        """节点 Agent 认领就绪任务：结合节点能力、数据本地性与并发预算派发任务并签发租约。"""
        node_id = text_field(body.get("node_id", ""), 64)
        supported_plugins = body.get("supported_plugins")
        max_tasks = int(body.get("max_tasks", 5))

        with pool.connection() as conn:
            node_rec = one(
                conn,
                "SELECT is_co_located, status FROM console_node WHERE node_id=%s",
                (node_id,),
            )
            is_co_located = node_rec["is_co_located"] if node_rec else True
            if node_rec and node_rec["status"] not in ("ready", "candidate"):
                fail(409, f"node_not_eligible:{node_rec['status']}")

            assigned = orch.schedule_ready_tasks(
                conn,
                candidate_node_id=node_id,
                is_co_located=is_co_located,
                supported_plugins=supported_plugins,
                max_tasks=max_tasks,
            )
            tasks = []
            assignments = []
            for item in assigned:
                t = one(conn, "SELECT * FROM pipeline_task WHERE task_id=%s", (item["task_id"],))
                a = one(
                    conn,
                    "SELECT * FROM scheduler_assignment WHERE assignment_id=%s",
                    (item["assignment_id"],),
                )
                if t and a:
                    tasks.append(_format_task(t))
                    assignments.append(_format_assignment(a))

        return {
            "tasks": tasks,
            "assignments": assignments,
        }

    @app.post("/v1/orchestration/scheduler:step")
    def scheduler_step(
        body: Annotated[dict | None, Body()] = None,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        """驱动一次调度循环：回收过期租约、释放等待重试任务、按本地性派发就绪任务。"""
        node_id = body.get("node_id") if isinstance(body, dict) else None
        is_co_located = body.get("is_co_located") if isinstance(body, dict) else None
        supported_plugins = body.get("supported_plugins") if isinstance(body, dict) else None
        max_tasks = int(body.get("max_tasks", 10)) if isinstance(body, dict) else 10

        with pool.connection() as conn:
            recovery = orch.reconcile_and_recover_leases(conn)
            released = orch.release_retry_wait_tasks(conn)
            assigned = orch.schedule_ready_tasks(
                conn,
                candidate_node_id=node_id,
                is_co_located=is_co_located,
                supported_plugins=supported_plugins,
                max_tasks=max_tasks,
            )
        return {
            "recovery": recovery,
            "released_retries": released,
            "assigned_tasks": assigned,
        }

    @app.post("/v1/orchestration/tasks/{task_id}:result")
    def report_result(
        task_id: str,
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("jobs:write"))] = None,
    ):
        run_id = body.get("run_id", "")
        attempt = int(body.get("attempt", 0))
        assignment_id = body.get("assignment_id", "")
        success = bool(body.get("success", False))
        output_ref = body.get("output_ref", "")
        retryable = bool(body.get("retryable", False))
        reason_code = body.get("reason_code", "")
        error_detail = body.get("error_detail", "")

        with pool.connection() as conn:
            res = orch.report_task_result(
                conn,
                task_id=task_id,
                run_id=run_id,
                attempt=attempt,
                assignment_id=assignment_id,
                success=success,
                output_ref=output_ref,
                retryable=retryable,
                reason_code=reason_code,
                error_detail=error_detail,
            )
        return {
            "task": _format_task(res["task"]),
            "unlocked_task_ids": res.get("unlocked_task_ids", []),
            "discarded": res.get("discarded", False),
            "retry_scheduled": res.get("retry_scheduled", False),
            "run_failed": res.get("run_failed", False),
            "run_completed": res.get("run_completed", False),
            "reason_code": res.get("reason_code") or res["task"].get("reason_code") or "",
        }

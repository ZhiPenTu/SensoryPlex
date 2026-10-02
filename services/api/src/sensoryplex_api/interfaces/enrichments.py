"""通用 Consumer 的节点授权输入、恢复租约及不可变结果暂存。"""

import base64
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as pb
from fastapi import Body, Header, Query
from fastapi.responses import FileResponse
from google.protobuf.message import DecodeError

from ..contracts import fail, one, rows
from ..infrastructure import enrichments as store
from ..infrastructure.runtime_bindings import pinned_runtime


def register(app, pool, settings, authenticated_node):
    def context(conn, task_id, authorization):
        locator = one(
            conn, "SELECT target_node_id,run_id FROM enrichment_task WHERE task_id=%s", (task_id,)
        )
        if not locator:
            fail(404, "enrichment_task_unknown")
        authenticated_node(conn, authorization, locator["target_node_id"])
        run = one(
            conn, "SELECT state FROM pipeline_run WHERE run_id=%s FOR UPDATE", (locator["run_id"],)
        )
        row = one(conn, "SELECT * FROM enrichment_task WHERE task_id=%s FOR UPDATE", (task_id,))
        if run["state"] == "cancelled" or row["state"] == "cancelled":
            fail(409, "enrichment_cancelled")
        return row

    @app.get("/v1/agent/enrichments/routes")
    def routes(
        node_id: str = Query(..., max_length=120),
        authorization: Annotated[str | None, Header()] = None,
    ):
        with pool.connection() as conn:
            authenticated_node(conn, authorization, node_id)
            pending = rows(
                conn,
                "SELECT DISTINCT route_id,release_id,config_id FROM enrichment_task WHERE "
                "target_node_id=%s AND state='queued' ORDER BY route_id LIMIT 32",
                (node_id,),
            )
            return {
                "items": [
                    {**item, "subject": store.TASK_PREFIX + item["route_id"]} for item in pending
                ]
            }

    @app.post("/v1/agent/enrichments/{task_id}:claim")
    def claim(task_id: str, authorization: Annotated[str | None, Header()] = None):
        with pool.connection() as conn:
            row = context(conn, task_id, authorization)
            if row["state"] != "queued":
                return {"terminal": True, "state": row["state"]}
            staged = one(
                conn, "SELECT result_digest FROM enrichment_result WHERE task_id=%s", (task_id,)
            )
            if staged:
                return {"staged": True, **staged}
            task = pb.EnrichmentTask.FromString(row["manifest_bytes"])
            if task.deadline_unix_ms <= int(time.time() * 1000):
                store.terminal_failure(conn, row, "enrichment_deadline_exceeded")
                return {
                    "terminal": True,
                    "state": "failed",
                    "reason_code": "enrichment_deadline_exceeded",
                }
            if row["lease_expires_at"] and row["lease_expires_at"] > datetime.now(UTC):
                fail(409, "enrichment_lease_busy")
            if row["attempt"] >= task.max_attempts:
                store.terminal_failure(conn, row, "enrichment_retries_exhausted")
                return {
                    "terminal": True,
                    "state": "failed",
                    "reason_code": "enrichment_retries_exhausted",
                }
            runtime = pinned_runtime(
                conn,
                row["target_node_id"],
                {
                    "plugin_id": task.plugin.plugin_id,
                    "release_id": task.release_id,
                    "artifact_digest": task.plugin.artifact_digest,
                    "config_hash": task.plugin.config_hash,
                },
            )
            if not runtime:
                fail(409, "enrichment_pinned_runtime_unavailable")
            lease = "lease_" + secrets.token_hex(16)
            expiry = min(
                datetime.now(UTC) + timedelta(seconds=store.ACK_WAIT_S),
                datetime.fromtimestamp(task.deadline_unix_ms / 1000, UTC),
            )
            conn.execute(
                "UPDATE enrichment_task SET attempt=attempt+1,lease_id=%s,lease_expires_at=%s "
                "WHERE task_id=%s",
                (lease, expiry, task_id),
            )
            inputs = []
            for reference in task.observation_refs:
                source = one(
                    conn,
                    "SELECT o.contract_bytes FROM observation o "
                    "JOIN material_observation mo USING(observation_id) "
                    "JOIN material_execution e ON (e.material_unit_id,e.material_revision)="
                    "(mo.material_unit_id,mo.revision) "
                    "WHERE o.observation_id=%s AND e.execution_id=%s LIMIT 1",
                    (reference, task.execution_id),
                )
                if not source:
                    fail(403, "enrichment_input_unauthorized")
                inputs.append(base64.b64encode(source["contract_bytes"]).decode())
            return {
                "task_b64": base64.b64encode(row["manifest_bytes"]).decode(),
                "lease_id": lease,
                "attempt": row["attempt"] + 1,
                "runtime_instance_id": runtime["runtime_instance_id"],
                "inputs_b64": inputs,
            }

    @app.post("/v1/agent/enrichments/{task_id}:renew")
    def renew(
        task_id: str,
        body: Annotated[dict, Body()],
        authorization: Annotated[str | None, Header()] = None,
    ):
        with pool.connection() as conn:
            row = context(conn, task_id, authorization)
            _require_lease(row, body)
            expiry = min(
                datetime.now(UTC) + timedelta(seconds=store.ACK_WAIT_S),
                datetime.fromtimestamp(row["deadline_unix_ms"] / 1000, UTC),
            )
            conn.execute(
                "UPDATE enrichment_task SET lease_expires_at=%s WHERE task_id=%s", (expiry, task_id)
            )
            return {"renewed": True}

    @app.get("/v1/agent/enrichments/{task_id}/asset")
    def media_input(
        task_id: str,
        lease_id: str = Query(..., max_length=64),
        authorization: Annotated[str | None, Header()] = None,
    ):
        with pool.connection() as conn:
            row = context(conn, task_id, authorization)
            _require_lease(row, {"lease_id": lease_id})
            task = pb.EnrichmentTask.FromString(row["manifest_bytes"])
            if task.observation_refs:
                fail(403, "enrichment_media_input_not_declared")
            upload = one(
                conn,
                "SELECT u.sha256,u.content_type FROM console_job_execution e "
                "JOIN console_job_draft j ON j.id=e.job_id "
                "JOIN console_upload u ON u.id=j.asset_id WHERE e.execution_id=%s",
                (task.execution_id,),
            )
            if not upload or upload["sha256"] != task.content_hash:
                fail(403, "enrichment_input_unauthorized")
        path = settings.blob_root.resolve() / upload["sha256"][7:]
        if not path.is_file():
            fail(503, "blob_unavailable")
        return FileResponse(path, media_type=upload["content_type"])

    @app.post("/v1/agent/enrichments/{task_id}:result")
    def stage(
        task_id: str,
        body: Annotated[dict, Body()],
        authorization: Annotated[str | None, Header()] = None,
    ):
        try:
            result = pb.EnrichmentResult.FromString(
                base64.b64decode(body["result_b64"], validate=True)
            )
        except (ValueError, KeyError, TypeError, DecodeError):
            fail(422, "enrichment_result_invalid")
        with pool.connection() as conn:
            row = context(conn, task_id, authorization)
            if row["state"] not in {"queued", "succeeded", "failed"}:
                fail(409, "enrichment_not_active")
            try:
                store.validate_result(conn, result, row)
            except ValueError as error:
                fail(422, str(error))
            saved = one(
                conn, "SELECT result_digest FROM enrichment_result WHERE task_id=%s", (task_id,)
            )
            if saved:
                if saved["result_digest"] != result.result_digest:
                    fail(409, "enrichment_result_conflict")
                return {"duplicate": True, **saved}
            _require_lease(row, body)
            data = result.SerializeToString(deterministic=True)
            conn.execute(
                "INSERT INTO enrichment_result(task_id,result_digest,contract_bytes,staged_by) "
                "VALUES (%s,%s,%s,%s)",
                (task_id, result.result_digest, data, row["target_node_id"]),
            )
            reference = pb.EnrichmentResultReference(
                task_id=task_id, result_digest=result.result_digest
            ).SerializeToString(deterministic=True)
            conn.execute(
                "INSERT INTO enrichment_result_outbox(task_id,contract_bytes) VALUES (%s,%s)",
                (task_id, reference),
            )
            return {"staged": True, "result_digest": result.result_digest}

    @app.post("/v1/agent/enrichments/{task_id}:retry")
    def retry(
        task_id: str,
        body: Annotated[dict, Body()],
        authorization: Annotated[str | None, Header()] = None,
    ):
        with pool.connection() as conn:
            row = context(conn, task_id, authorization)
            _require_lease(row, body)
            reason = body.get("reason_code", "enrichment_worker_failed")
            if not isinstance(reason, str) or not __import__("re").fullmatch(
                r"[a-z][a-z0-9_]{0,119}", reason
            ):
                fail(422, "enrichment_failure_reason_invalid")
            task = pb.EnrichmentTask.FromString(row["manifest_bytes"])
            if row["attempt"] >= task.max_attempts:
                store.terminal_failure(conn, row, "enrichment_retries_exhausted")
                return {"terminal": True, "state": "failed"}
            conn.execute(
                "UPDATE enrichment_task SET lease_expires_at=NULL,reason_code=%s WHERE task_id=%s",
                (reason, task_id),
            )
            return {"retry": True}

    def _require_lease(row, body):
        if (
            row["state"] != "queued"
            or not row["lease_id"]
            or body.get("lease_id") != row["lease_id"]
            or not row["lease_expires_at"]
            or row["lease_expires_at"] <= datetime.now(UTC)
            or row["deadline_unix_ms"] <= int(time.time() * 1000)
        ):
            fail(409, "enrichment_lease_expired")

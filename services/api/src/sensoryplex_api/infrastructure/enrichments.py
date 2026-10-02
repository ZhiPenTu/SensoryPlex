"""通用补全台账：锁定输入身份、显式无观测、取消优先和事务追加。"""

import hashlib
import time

from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as pb
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.manifest import canonical
from edge_material_sdk.validation import validate_digest, validate_observation
from psycopg.types.json import Jsonb

from ..contracts import one, rows, validate_processor_reason
from . import materials
from .runtime_bindings import pinned_runtime

STREAM = "sensoryplex-enrichments-v1"
TASK_PREFIX = "sensoryplex.tasks.enrichment.v1."
RESULT_SUBJECT = "sensoryplex.results.enrichment.v1"
MAX_TASKS_PER_EXECUTION = 16384
MAX_RESULTS = 1024
ACK_WAIT_S = 90


def route_id(target, node):
    return hashlib.sha256(
        canonical({"target": target, "release": node["release_id"], "config": node["config_hash"]})
    ).hexdigest()[:32]


def enqueue(conn, *, execution, revision, description, units):
    """Timeline 的同一事务创建任务；仅携带受控引用，不从队列读事实。"""
    timeline = next(n for n in revision["definition_json"]["nodes"] if n["id"] == "timeline_fusion")
    delayed = [n for n in timeline.get("delayed_enrichments", []) if n.get("release_id")]
    created = []
    for node in delayed:
        if not pinned_runtime(conn, execution["target_node_id"], node):
            raise ValueError("enrichment_pinned_runtime_unavailable")
        inputs = []
        selector = node["input_selector"]
        if selector.startswith("node:"):
            saved = one(
                conn,
                "SELECT o.contract_bytes FROM pipeline_task t JOIN plugin_task_output o "
                "USING(task_id) WHERE t.run_id=%s AND t.node_id=%s AND t.state='succeeded'",
                (execution["run_id"], selector[5:]),
            )
            if not saved:
                raise ValueError("enrichment_upstream_unavailable")
            output = pb.PluginTaskOutput.FromString(saved["contract_bytes"])
            for observation in output.observations:
                if observation.modality in node["consumes"]:
                    inputs.append((observation.time_range, [observation.observation_id]))
        else:
            has_track = any(
                track.track_kind
                == ("video" if node["consumes"] == ["media.video_frame"] else "audio")
                for track in description.tracks
            )
            if has_track:
                inputs = [(unit.time_range, []) for unit in units]
        if len(created) + len(inputs) > MAX_TASKS_PER_EXECUTION:
            raise ValueError("enrichment_task_budget_exceeded")
        for span, refs in inputs:
            unit = next(
                (u for u in units if u.time_range.start_ms <= span.start_ms < u.time_range.end_ms),
                None,
            )
            if not unit:
                raise ValueError("enrichment_input_range_unmapped")
            identity = hashlib.sha256(
                canonical([execution["execution_id"], node["id"], refs, span.start_ms, span.end_ms])
            ).hexdigest()[:32]
            task_id = "enr_" + identity
            route = route_id(execution["target_node_id"], node)
            run = one(
                conn,
                "SELECT deadline_unix_ms FROM pipeline_run WHERE run_id=%s",
                (execution["run_id"],),
            )
            task = pb.EnrichmentTask(
                task_id=task_id,
                execution_id=execution["execution_id"],
                run_id=execution["run_id"],
                node_id=node["id"],
                data_plane_node_id=execution["target_node_id"],
                route_id=route,
                plugin={
                    "plugin_id": node["plugin_id"],
                    "version": node["plugin_version"],
                    "artifact_digest": node["artifact_digest"],
                    "config_hash": node["config_hash"],
                },
                release_id=node["release_id"],
                config_id=node["config_id"],
                observation_refs=refs,
                asset_id="asset-" + description.source.content_hash[7:19],
                stream_id=description.source.stream_id,
                source_id=description.source.source_id,
                material_unit_id=unit.material_unit_id,
                time_range=span,
                content_hash=description.source.content_hash,
                max_attempts=node["max_attempts"],
                deadline_unix_ms=run["deadline_unix_ms"],
                input_modality=node["consumes"][0],
                process_timeout_ms=node["deadline_ms"],
            )
            data = task.SerializeToString(deterministic=True)
            old = one(
                conn, "SELECT manifest_bytes FROM enrichment_task WHERE task_id=%s", (task_id,)
            )
            if old and old["manifest_bytes"] != data:
                raise ValueError("enrichment_task_identity_conflict")
            conn.execute(
                "INSERT INTO "
                "enrichment_task(task_id,execution_id,run_id,node_id,target_node_id,route_id,release_id,config_id,material_unit_id,start_ms,end_ms,manifest_bytes,deadline_unix_ms)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    task_id,
                    task.execution_id,
                    task.run_id,
                    task.node_id,
                    task.data_plane_node_id,
                    route,
                    task.release_id,
                    task.config_id,
                    task.material_unit_id,
                    span.start_ms,
                    span.end_ms,
                    data,
                    task.deadline_unix_ms,
                ),
            )
            conn.execute(
                "INSERT INTO enrichment_outbox(task_id,subject,contract_bytes) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (task_id, TASK_PREFIX + route, data),
            )
            created.append(task_id)
    for unit in units:
        resolved, waiting = [], []
        for modality in sorted({m for n in delayed for m in n["produces"]}):
            exists = one(
                conn,
                "SELECT 1 FROM enrichment_task t JOIN plugin_registration g USING(release_id) "
                "WHERE t.execution_id=%s AND t.start_ms<%s AND t.end_ms>%s "
                "AND g.manifest->'spec'->'outputs' @> %s::jsonb LIMIT 1",
                (
                    execution["execution_id"],
                    unit.time_range.end_ms,
                    unit.time_range.start_ms,
                    Jsonb([{"modality": modality}]),
                ),
            )
            if not exists:
                resolved.append(modality)
            else:
                waiting.append(modality)
        pending = sorted((set(unit.pending_enrichments) | set(waiting)) - set(resolved))
        if pending == list(unit.pending_enrichments) and not resolved:
            continue
        del unit.pending_enrichments[:]
        unit.pending_enrichments.extend(pending)
        unit.revision += 1
        unit.status = (
            "partial" if pending else ("enriched" if unit.observations else "no_observations")
        )
        unit.created_at_unix_ms = int(time.time() * 1000)
        materials.append_material(
            conn,
            unit,
            trace_id="enrichment:no-input:" + execution["execution_id"],
            execution_id=execution["execution_id"],
        )
        window = one(
            conn,
            "SELECT * FROM timeline_window_state WHERE execution_id=%s AND start_ms=%s "
            "AND end_ms=%s ORDER BY state_revision DESC LIMIT 1",
            (execution["execution_id"], unit.time_range.start_ms, unit.time_range.end_ms),
        )
        if window:
            states, reasons = dict(window["modality_states"]), dict(window["reason_codes"])
            for modality in resolved:
                if any(o.modality == modality for o in unit.observations):
                    states[modality] = "observed"
                else:
                    states[modality] = "not_applicable"
                    reasons[modality] = "enrichment_input_not_available"
            conn.execute(
                "INSERT INTO timeline_window_state(execution_id,stream_id,start_ms,end_ms,"
                "state_revision,sampling_state,modality_states,reason_codes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    execution["execution_id"],
                    window["stream_id"],
                    window["start_ms"],
                    window["end_ms"],
                    window["state_revision"] + 1,
                    window["sampling_state"],
                    Jsonb(states),
                    Jsonb(reasons),
                ),
            )
    return created


def digest_result(result):
    value = pb.EnrichmentResult()
    value.CopyFrom(result)
    value.ClearField("result_digest")
    return "sha256:" + hashlib.sha256(value.SerializeToString(deterministic=True)).hexdigest()


def validate_result(conn, result, task_row):
    """结果必须逐字匹配入队清单、schema 和来源，不相信 Consumer 的路由声明。"""
    task = result.task
    if task.SerializeToString(deterministic=True) != task_row["manifest_bytes"]:
        raise ValueError("enrichment_manifest_mismatch")
    if digest_result(result) != result.result_digest:
        raise ValueError("enrichment_result_digest_mismatch")
    if len(result.observations) > MAX_RESULTS or result.ByteSize() > 3 << 20:
        raise ValueError("enrichment_result_limit_exceeded")
    validate_processor_reason(result.outcome_reason)
    if result.HasField("error"):
        validate_processor_reason(result.error.reason_code, required=True)
    if task.observation_refs:
        if result.input_receipts:
            raise ValueError("enrichment_input_receipt_unexpected")
    elif not result.HasField("error"):
        if len(result.input_receipts) != 1:
            raise ValueError("enrichment_input_receipt_missing")
        receipt = result.input_receipts[0]
        validate_digest(receipt.content_hash)
        if (
            receipt.kind != task.input_modality.removeprefix("media.")
            or receipt.source_item_id != f"buf-{task.task_id}@{receipt.content_hash[7:23]}"
            or not task.time_range.start_ms
            <= receipt.time_range.start_ms
            < receipt.time_range.end_ms
            <= task.time_range.end_ms
        ):
            raise ValueError("enrichment_input_receipt_mismatch")
    if result.HasField("error"):
        if (
            result.observations
            or result.outcome
            or not result.error.reason_code
            or result.error.retryable
        ):
            raise ValueError("enrichment_ambiguous_result")
        return
    if result.outcome == runtime.PROCESS_OUTCOME_NO_OBSERVATIONS:
        if result.observations or not result.outcome_reason:
            raise ValueError("enrichment_ambiguous_result")
        return
    if result.outcome != runtime.PROCESS_OUTCOME_OBSERVED or not result.observations:
        raise ValueError("enrichment_empty_result")
    ids = set()
    sources = {}
    for reference in task.observation_refs:
        row = one(
            conn, "SELECT contract_bytes FROM observation WHERE observation_id=%s", (reference,)
        )
        if not row:
            raise ValueError("enrichment_input_observation_missing")
        source = material.Observation.FromString(row["contract_bytes"])
        sources[source.source_item_id] = (source.content_hash, source.time_range)
    for observation in result.observations:
        validate_observation(observation)
        materials._registered_observation(conn, observation)
        registration = one(
            conn, "SELECT manifest FROM plugin_registration WHERE release_id=%s", (task.release_id,)
        )
        if observation.modality not in {
            d["modality"] for d in registration["manifest"]["spec"]["outputs"]
        }:
            raise ValueError("enrichment_output_modality_mismatch")
        if (
            observation.provenance.processor_release_id != task.release_id
            or observation.provenance.config_hash != task.plugin.config_hash
            or observation.stream_id != task.stream_id
            or observation.source_id != task.source_id
            or observation.observation_id in ids
            or observation.time_range.start_ms < task.time_range.start_ms
            or observation.time_range.end_ms > task.time_range.end_ms
        ):
            raise ValueError("enrichment_output_identity_mismatch")
        if sources:
            source = sources.get(observation.source_item_id)
            if (
                not source
                or observation.content_hash != source[0]
                or observation.time_range.start_ms < source[1].start_ms
                or observation.time_range.end_ms > source[1].end_ms
            ):
                raise ValueError("enrichment_output_source_mismatch")
        else:
            receipt = next(
                (
                    r
                    for r in result.input_receipts
                    if r.source_item_id == observation.source_item_id
                ),
                None,
            )
            if (
                not receipt
                or observation.content_hash != receipt.content_hash
                or not receipt.time_range.start_ms
                <= observation.time_range.start_ms
                < observation.time_range.end_ms
                <= receipt.time_range.end_ms
            ):
                raise ValueError("enrichment_output_source_mismatch")
        ids.add(observation.observation_id)


def fuse(conn, reference):
    """锁 Run 后锁任务，取消与融合共享顺序，结果重复通知只回原终态。"""
    locator = one(conn, "SELECT run_id FROM enrichment_task WHERE task_id=%s", (reference.task_id,))
    if not locator:
        raise ValueError("enrichment_task_unknown")
    run = one(conn, "SELECT * FROM pipeline_run WHERE run_id=%s FOR UPDATE", (locator["run_id"],))
    row = one(
        conn, "SELECT * FROM enrichment_task WHERE task_id=%s FOR UPDATE", (reference.task_id,)
    )
    saved = one(conn, "SELECT * FROM enrichment_result WHERE task_id=%s", (reference.task_id,))
    if not saved or saved["result_digest"] != reference.result_digest:
        raise ValueError("enrichment_result_reference_mismatch")
    if row["state"] != "queued":
        return row["state"]
    if run["state"] == "cancelled":
        conn.execute(
            "UPDATE enrichment_task SET "
            "state='cancelled',reason_code='run_cancelled',completed_at=now() WHERE task_id=%s",
            (row["task_id"],),
        )
        return "cancelled"
    result = pb.EnrichmentResult.FromString(saved["contract_bytes"])
    validate_result(conn, result, row)
    for receipt in result.input_receipts:
        conn.execute(
            "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                receipt.source_item_id,
                result.task.stream_id,
                receipt.kind,
                receipt.time_range.start_ms,
                receipt.time_range.end_ms,
            ),
        )
    state = "failed" if result.HasField("error") else "succeeded"
    reason = result.error.reason_code if state == "failed" else result.outcome_reason
    conn.execute(
        "UPDATE enrichment_task SET "
        "state=%s,reason_code=%s,completed_at=now(),lease_expires_at=NULL WHERE task_id=%s",
        (state, reason, row["task_id"]),
    )
    _append_affected(conn, row, result, state, reason)
    from .orchestration import _sync_console_execution

    _sync_console_execution(conn, row["run_id"])
    return state


def _append_affected(conn, row, result, state, reason):
    registration = one(
        conn, "SELECT manifest FROM plugin_registration WHERE release_id=%s", (row["release_id"],)
    )
    modalities = [d["modality"] for d in registration["manifest"]["spec"]["outputs"]]
    current = rows(
        conn,
        "SELECT DISTINCT ON (m.material_unit_id) m.contract_bytes FROM material_unit m JOIN "
        "material_execution e ON "
        "(m.material_unit_id,m.revision)=(e.material_unit_id,e.material_revision) WHERE "
        "e.execution_id=%s AND m.start_ms<%s AND m.end_ms>%s ORDER BY "
        "m.material_unit_id,m.revision DESC",
        (row["execution_id"], row["end_ms"], row["start_ms"]),
    )
    for record in current:
        unit = material.MaterialUnit.FromString(record["contract_bytes"])
        present = {o.observation_id for o in unit.observations}
        additions = [
            o
            for o in result.observations
            if o.observation_id not in present
            and o.time_range.start_ms < unit.time_range.end_ms
            and o.time_range.end_ms > unit.time_range.start_ms
        ]
        unit.observations.extend(additions)
        pending = list(unit.pending_enrichments)
        for modality in modalities:
            other = one(
                conn,
                "SELECT 1 FROM enrichment_task t JOIN plugin_registration g USING(release_id) "
                "WHERE t.execution_id=%s AND t.state='queued' AND t.start_ms<%s AND t.end_ms>%s "
                "AND g.manifest->'spec'->'outputs' @> %s::jsonb LIMIT 1",
                (
                    row["execution_id"],
                    unit.time_range.end_ms,
                    unit.time_range.start_ms,
                    Jsonb([{"modality": modality}]),
                ),
            )
            if not other:
                pending = [m for m in pending if m != modality]
        del unit.pending_enrichments[:]
        unit.pending_enrichments.extend(pending)
        unit.status = (
            "partial" if pending else ("enriched" if unit.observations else "no_observations")
        )
        unit.created_at_unix_ms = int(time.time() * 1000)
        materials.append_derived_material(
            conn, unit, trace_id="enrichment:" + row["task_id"], execution_id=row["execution_id"]
        )
    windows = rows(
        conn,
        "SELECT DISTINCT ON (start_ms,end_ms) * FROM timeline_window_state WHERE execution_id=%s "
        "AND start_ms<%s AND end_ms>%s ORDER BY start_ms,end_ms,state_revision DESC",
        (row["execution_id"], row["end_ms"], row["start_ms"]),
    )
    for window in windows:
        states, reasons = dict(window["modality_states"]), dict(window["reason_codes"])
        for modality in modalities:
            states[modality] = (
                "failed"
                if state == "failed"
                else (
                    "observed"
                    if any(o.modality == modality for o in result.observations)
                    else "not_observed"
                )
            )
            # 汇总本窗口的全部任务；后到的无检测不能覆盖先前的真实观测。
            aggregate = rows(
                conn,
                "SELECT t.state,t.reason_code,r.contract_bytes FROM enrichment_task t "
                "JOIN plugin_registration g USING(release_id) "
                "LEFT JOIN enrichment_result r USING(task_id) WHERE t.execution_id=%s "
                "AND t.start_ms<%s AND t.end_ms>%s "
                "AND g.manifest->'spec'->'outputs' @> %s::jsonb",
                (
                    row["execution_id"],
                    window["end_ms"],
                    window["start_ms"],
                    Jsonb([{"modality": modality}]),
                ),
            )
            observed = any(
                entry["contract_bytes"]
                and any(
                    o.modality == modality
                    and o.time_range.start_ms < window["end_ms"]
                    and o.time_range.end_ms > window["start_ms"]
                    for o in pb.EnrichmentResult.FromString(entry["contract_bytes"]).observations
                )
                for entry in aggregate
                if entry["state"] == "succeeded"
            )
            failed = next((e for e in aggregate if e["state"] == "failed"), None)
            if failed:
                states[modality], reasons[modality] = "failed", failed["reason_code"]
            elif any(e["state"] == "queued" for e in aggregate):
                states[modality] = "queued"
                reasons.pop(modality, None)
            elif observed:
                states[modality] = "observed"
                reasons.pop(modality, None)
            else:
                states[modality] = "not_observed"
                reasons[modality] = reason or "enrichment_no_observations"
        conn.execute(
            "INSERT INTO "
            "timeline_window_state(execution_id,stream_id,start_ms,end_ms,state_revision,sampling_state,modality_states,reason_codes)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row["execution_id"],
                window["stream_id"],
                window["start_ms"],
                window["end_ms"],
                window["state_revision"] + 1,
                window["sampling_state"],
                Jsonb(states),
                Jsonb(reasons),
            ),
        )


def terminal_failure(conn, row, reason):
    """恢复预算耗尽和 deadline 也必须收敛事实，不能永久留在 queued。"""
    conn.execute(
        "UPDATE enrichment_task SET state='failed',reason_code=%s,completed_at=now(),"
        "lease_expires_at=NULL WHERE task_id=%s",
        (reason, row["task_id"]),
    )
    _append_affected(conn, row, pb.EnrichmentResult(), "failed", reason)
    from .orchestration import _sync_console_execution

    _sync_console_execution(conn, row["run_id"])


def reject_staged_result(conn, reference, reason):
    """仅终止与已暂存摘要一致的失败结果；错误引用不得干扰真实任务。"""
    locator = one(conn, "SELECT run_id FROM enrichment_task WHERE task_id=%s", (reference.task_id,))
    if not locator:
        return False
    run = one(conn, "SELECT * FROM pipeline_run WHERE run_id=%s FOR UPDATE", (locator["run_id"],))
    row = one(
        conn, "SELECT * FROM enrichment_task WHERE task_id=%s FOR UPDATE", (reference.task_id,)
    )
    saved = one(
        conn, "SELECT result_digest FROM enrichment_result WHERE task_id=%s", (reference.task_id,)
    )
    if not saved or saved["result_digest"] != reference.result_digest:
        return False
    if row["state"] != "queued":
        return True
    if run["state"] == "cancelled":
        conn.execute(
            "UPDATE enrichment_task SET state='cancelled',reason_code='run_cancelled',"
            "completed_at=now(),lease_expires_at=NULL WHERE task_id=%s",
            (row["task_id"],),
        )
    else:
        terminal_failure(conn, row, reason)
    return True


def expire(conn):
    pending = rows(
        conn,
        "SELECT t.task_id,t.run_id FROM enrichment_task t WHERE t.state='queued' "
        "AND (t.deadline_unix_ms<%s OR (t.lease_expires_at<now() AND t.attempt>0 "
        "AND NOT EXISTS (SELECT 1 FROM enrichment_result r WHERE r.task_id=t.task_id))) "
        "ORDER BY t.created_at LIMIT 100",
        (int(time.time() * 1000),),
    )
    for locator in pending:
        one(
            conn, "SELECT run_id FROM pipeline_run WHERE run_id=%s FOR UPDATE", (locator["run_id"],)
        )
        row = one(
            conn, "SELECT * FROM enrichment_task WHERE task_id=%s FOR UPDATE", (locator["task_id"],)
        )
        if row["state"] != "queued":
            continue
        task = pb.EnrichmentTask.FromString(row["manifest_bytes"])
        if row["deadline_unix_ms"] < int(time.time() * 1000):
            terminal_failure(conn, row, "enrichment_deadline_exceeded")
        elif row["attempt"] >= task.max_attempts:
            terminal_failure(conn, row, "enrichment_retries_exhausted")

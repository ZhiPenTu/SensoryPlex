"""VLM 延迟满足的任务账本、WorkQueue 发布器与结果融合器。

本模块的边界故意很窄：快路径先在 PostgreSQL 写入 OCR/Timeline 事实，再在同一事务内生成
不可变 `TaskInputManifest` 和 `vlm_task_outbox` 行。JetStream 的 WorkQueue 只负责让多个
VLM Consumer 竞争拉取；最终成功、失败、Observation 和时间轴状态只由本模块的事务收敛。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from edge_material_sdk.generated.common.v1 import common_pb2
from edge_material_sdk.generated.material.v1 import material_pb2
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2
from edge_material_sdk.vlm_tasks import (
    MAX_PROMPT_CHARS,
    RESULT_SUBJECT,
    TASK_STREAM,
    TASK_SUBJECT,
    VlmTaskContractError,
    result_digest,
    validate_task_manifest,
)
from google.protobuf.message import DecodeError
from psycopg.types.json import Jsonb

from ..contracts import one
from . import materials

TASK_STREAM_MAX_MSGS = 100_000
TASK_STREAM_MAX_BYTES = 128 << 20
TASK_STREAM_MAX_AGE_S = 2 * 24 * 3600
TASK_STREAM_DUPLICATE_WINDOW_S = 2 * 3600
TASK_ACK_WAIT_S = 90
TASK_MAX_DELIVER = 8
VLM_MODALITY = "vision.scene_description"
VLM_PLUGIN_ID = "org.sensoryplex.vlm-moondream"
DEFAULT_PROMPT = "Describe what is visible in this image in one sentence."


class VlmDelayedError(RuntimeError):
    """稳定错误码；detail 只允许短的契约差异或异常类名。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class VlmQueueOptions:
    stream: str = TASK_STREAM
    task_subject: str = TASK_SUBJECT
    result_subject: str = RESULT_SUBJECT
    ack_wait_s: int = TASK_ACK_WAIT_S
    max_deliver: int = TASK_MAX_DELIVER

    def validate(self) -> None:
        if self.stream != TASK_STREAM:
            raise VlmDelayedError("vlm_task_stream_name_invalid")
        if (self.task_subject, self.result_subject) != (TASK_SUBJECT, RESULT_SUBJECT):
            raise VlmDelayedError("vlm_task_subject_invalid")
        if not 5 <= self.ack_wait_s <= 600:
            raise VlmDelayedError("vlm_task_ack_wait_invalid")
        if not 1 <= self.max_deliver <= 32:
            raise VlmDelayedError("vlm_task_max_deliver_invalid")


def _retention_name(value: object) -> str:
    return str(getattr(value, "value", value)).lower().replace("_", "")


def task_stream_snapshot(info) -> dict[str, Any]:
    """压平 nats-py StreamInfo，便于严格漂移对账和纯单元测试。"""
    config = info.config
    return {
        "subjects": list(config.subjects or []),
        "storage": str(config.storage or ""),
        "retention": _retention_name(config.retention),
        "max_msgs": int(config.max_msgs or 0),
        "max_bytes": int(config.max_bytes or 0),
        "max_age": int(config.max_age or 0),
        "duplicate_window": int(config.duplicate_window or 0),
    }


def task_stream_contract_diff(existing: dict[str, Any]) -> list[str]:
    """流已存在时只报告漂移，绝不静默改 WorkQueue 保留策略。"""
    expected_subjects = sorted([TASK_SUBJECT, RESULT_SUBJECT])
    diff: list[str] = []
    if sorted(existing.get("subjects") or []) != expected_subjects:
        diff.append("subjects")
    if (existing.get("storage") or "").lower() not in {"file", "filestorage"}:
        diff.append("storage")
    if _retention_name(existing.get("retention")) not in {
        "workqueue",
        "workqueueretention",
    }:
        diff.append("retention")
    if int(existing.get("max_msgs") or 0) != TASK_STREAM_MAX_MSGS:
        diff.append("max_msgs")
    if int(existing.get("max_bytes") or 0) != TASK_STREAM_MAX_BYTES:
        diff.append("max_bytes")
    if int(existing.get("max_age") or 0) != TASK_STREAM_MAX_AGE_S:
        diff.append("max_age")
    if int(existing.get("duplicate_window") or 0) != TASK_STREAM_DUPLICATE_WINDOW_S:
        diff.append("duplicate_window")
    return diff


def task_stream_config():
    """发布端创建的唯一 WorkQueue 配置；消费端永远不建流。"""
    import nats.js.api as jsapi

    return jsapi.StreamConfig(
        name=TASK_STREAM,
        subjects=[TASK_SUBJECT, RESULT_SUBJECT],
        storage=jsapi.StorageType.FILE,
        retention=jsapi.RetentionPolicy.WORK_QUEUE,
        max_msgs=TASK_STREAM_MAX_MSGS,
        max_bytes=TASK_STREAM_MAX_BYTES,
        max_age=TASK_STREAM_MAX_AGE_S,
        duplicate_window=TASK_STREAM_DUPLICATE_WINDOW_S,
    )


def _not_found(error: Exception) -> bool:
    return type(error).__name__ == "NotFoundError"


async def ensure_task_stream(js):
    """发布端按契约建流；已有流发生任何漂移都显式拒绝。"""
    try:
        info = await js.stream_info(TASK_STREAM)
    except Exception as error:  # noqa: BLE001
        if not _not_found(error):
            raise VlmDelayedError("vlm_task_stream_unavailable", type(error).__name__) from error
        return await js.add_stream(config=task_stream_config())
    diff = task_stream_contract_diff(task_stream_snapshot(info))
    if diff:
        raise VlmDelayedError("vlm_task_stream_contract_mismatch", ",".join(diff))
    return info


async def require_task_stream(js):
    """Consumer/sink 只验流，缺流说明发布器没有就绪，不能靠消费端掩盖。"""
    try:
        info = await js.stream_info(TASK_STREAM)
    except Exception as error:  # noqa: BLE001
        if _not_found(error):
            raise VlmDelayedError("vlm_task_stream_missing") from error
        raise VlmDelayedError("vlm_task_stream_unavailable", type(error).__name__) from error
    diff = task_stream_contract_diff(task_stream_snapshot(info))
    if diff:
        raise VlmDelayedError("vlm_task_stream_contract_mismatch", ",".join(diff))
    return info


async def connect_bounded(nats_url: str, *, name: str, timeout_s: float = 10.0):
    """启动期有界连接；稳态掉线交给 nats-py 重连，不能静默卡在首次连接。"""
    import nats

    try:
        return await asyncio.wait_for(
            nats.connect(
                nats_url,
                name=name,
                max_reconnect_attempts=-1,
                reconnect_time_wait=1,
                connect_timeout=timeout_s,
            ),
            timeout=timeout_s * 2,
        )
    except TimeoutError as error:
        raise VlmDelayedError("vlm_task_nats_unreachable", type(error).__name__) from error
    except Exception as error:  # noqa: BLE001
        raise VlmDelayedError("vlm_task_nats_unreachable", type(error).__name__) from error


def _task_id() -> str:
    return "vlm_" + uuid.uuid4().hex


def _material_id(execution_id: str, source_item_id: str) -> str:
    digest = hashlib.sha256(f"{execution_id}|{source_item_id}".encode()).hexdigest()[:32]
    return "mat_vlm_" + digest


def _prompt_for_revision(conn, node: dict[str, Any]) -> str:
    config_id = str(node.get("config_id") or "")
    row = (
        conn.execute(
            "SELECT config FROM console_plugin_config WHERE id=%s", (config_id,)
        ).fetchone()
        if config_id
        else None
    )
    config = row[0] if row else {}
    prompt = str(config.get("prompt") or DEFAULT_PROMPT).strip()
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise VlmDelayedError("vlm_task_prompt_invalid")
    return prompt


def _candidate_video_items(
    items: dict[str, tuple[str, int, int]], *, interval_ms: int
) -> list[tuple[str, int, int]]:
    """固定步长只决定是否创建任务；真正像素始终由 Consumer 按锚点即时解码。"""
    candidates = sorted(
        (item_id, start_ms, end_ms)
        for item_id, (kind, start_ms, end_ms) in items.items()
        if kind == "video_frame"
    )
    selected: list[tuple[str, int, int]] = []
    next_at = -1
    for item_id, start_ms, end_ms in candidates:
        if start_ms >= next_at:
            selected.append((item_id, start_ms, end_ms))
            next_at = start_ms + interval_ms
    return selected


def _material_for_item(
    units: list[material_pb2.MaterialUnit], *, execution_id: str, item_id: str, start_ms: int
) -> str:
    matching = [
        unit.material_unit_id
        for unit in units
        if unit.material_unit_id and unit.time_range.start_ms <= start_ms < unit.time_range.end_ms
    ]
    return sorted(matching)[0] if matching else _material_id(execution_id, item_id)


def enqueue_vlm_tasks(
    conn,
    *,
    execution: dict[str, Any],
    revision: dict[str, Any],
    description,
    items: dict[str, tuple[str, int, int]],
    units: list[material_pb2.MaterialUnit],
) -> list[str]:
    """为快路径完成的真实帧锚点原子生成慢路径任务和发布 outbox。"""
    nodes = revision["definition_json"].get("nodes", [])
    timeline = next((item for item in nodes if item.get("id") == "timeline_fusion"), {})
    delayed = timeline.get("delayed_enrichments") or []
    if not delayed:
        return []
    if not isinstance(delayed, list) or len(delayed) != 1 or not isinstance(delayed[0], dict):
        raise VlmDelayedError("vlm_task_revision_metadata_invalid")
    node = delayed[0]
    if node.get("plugin_id") != VLM_PLUGIN_ID:
        raise VlmDelayedError("vlm_task_plugin_identity_invalid")
    policy = timeline.get("execution_policy") or {}
    interval_ms = int(policy.get("vlm_sample_interval_ms") or 5_000)
    if not 1_000 <= interval_ms <= 60_000:
        raise VlmDelayedError("vlm_task_sampling_policy_invalid")
    prompt = _prompt_for_revision(conn, node)
    source = description.source
    asset_id = f"asset-{source.content_hash[7:19]}"
    media_locator = f"media_asset:{asset_id}"
    created: list[str] = []
    for source_item_id, start_ms, end_ms in _candidate_video_items(items, interval_ms=interval_ms):
        task_id = _task_id()
        task = orchestration_pb2.TaskInputManifest(
            run_id=str(execution["run_id"]),
            task_id=task_id,
            attempt=1,
            stream_id=source.stream_id,
            content_hash=source.content_hash,
            execution_id=str(execution["execution_id"]),
            asset_id=asset_id,
            media_locator=media_locator,
            time_range=common_pb2.TimeRange(start_ms=start_ms, end_ms=end_ms),
            prompt=prompt,
            source_id=source.source_id,
            source_item_id=source_item_id,
            material_unit_id=_material_for_item(
                units,
                execution_id=str(execution["execution_id"]),
                item_id=source_item_id,
                start_ms=start_ms,
            ),
            plugin=orchestration_pb2.PluginReference(
                plugin_id=VLM_PLUGIN_ID,
                version=node["plugin_version"],
                artifact_digest=node["artifact_digest"],
                config_hash=node["config_hash"],
            ),
        )
        validate_task_manifest(task)
        event_id = f"vlm-task:{task_id}"
        inserted = conn.execute(
            """
            INSERT INTO vlm_enrichment_task(
                task_id,execution_id,asset_id,content_hash,media_locator,stream_id,source_id,
                source_item_id,material_unit_id,start_ms,end_ms,prompt,plugin_id,artifact_digest,
                config_hash,state
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued')
            ON CONFLICT (execution_id,source_item_id) DO NOTHING
            RETURNING task_id
            """,
            (
                task_id,
                execution["execution_id"],
                asset_id,
                source.content_hash,
                media_locator,
                source.stream_id,
                source.source_id,
                source_item_id,
                task.material_unit_id,
                start_ms,
                end_ms,
                prompt,
                VLM_PLUGIN_ID,
                node["artifact_digest"],
                node["config_hash"],
            ),
        ).fetchone()
        if not inserted:
            continue
        conn.execute(
            "INSERT INTO vlm_task_outbox(event_id,task_id,contract_bytes) VALUES (%s,%s,%s)",
            (event_id, task_id, task.SerializeToString(deterministic=True)),
        )
        _advance_coverage(
            conn,
            {
                "execution_id": execution["execution_id"],
                "stream_id": source.stream_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
            },
            state="queued",
        )
        created.append(task_id)
    return created


def claim_task_outbox(conn, batch: int) -> list[dict[str, Any]]:
    if not 1 <= batch <= 1_000:
        raise VlmDelayedError("vlm_task_publish_batch_invalid")
    rows = conn.execute(
        """
        SELECT event_id,task_id,contract_bytes FROM vlm_task_outbox
        WHERE published_at IS NULL ORDER BY created_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED
        """,
        (batch,),
    ).fetchall()
    conn.commit()
    return [{"event_id": row[0], "task_id": row[1], "contract_bytes": row[2]} for row in rows]


def mark_task_outbox_published(conn, event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    rows = conn.execute(
        """
        UPDATE vlm_task_outbox outbox SET published_at=now(),attempt=attempt+1
        WHERE event_id=ANY(%s) AND published_at IS NULL
        RETURNING task_id
        """,
        (event_ids,),
    ).fetchall()
    if rows:
        conn.execute(
            """
            UPDATE vlm_enrichment_task SET state='published',published_at=now()
            WHERE task_id=ANY(%s) AND state='queued'
            """,
            ([row[0] for row in rows],),
        )
    conn.commit()
    return len(rows)


def mark_task_outbox_failed(conn, event_ids: list[str]) -> None:
    if event_ids:
        conn.execute(
            "UPDATE vlm_task_outbox SET attempt=attempt+1 WHERE event_id=ANY(%s)",
            (event_ids,),
        )
        conn.commit()


async def publish_task_outbox_cycle(conn, js, *, batch: int = 64) -> dict[str, int]:
    """发布一批任务；确认前不触碰 published_at，崩溃重发由 Msg-Id 去重。"""
    candidates = claim_task_outbox(conn, batch)
    published: list[str] = []
    failed: list[str] = []
    for item in candidates:
        try:
            task = orchestration_pb2.TaskInputManifest.FromString(item["contract_bytes"])
            validate_task_manifest(task)
            await js.publish(
                TASK_SUBJECT,
                item["contract_bytes"],
                headers={"Nats-Msg-Id": item["event_id"]},
                timeout=5,
            )
            published.append(item["event_id"])
        except (DecodeError, VlmTaskContractError, VlmDelayedError):
            # 本地契约坏行不能被标成已发布；留在 outbox 供运维显式修复。
            failed.append(item["event_id"])
        except Exception:  # noqa: BLE001 - 传输错误可重试，细节不写出
            failed.append(item["event_id"])
    done = mark_task_outbox_published(conn, published)
    mark_task_outbox_failed(conn, failed)
    return {"claimed": len(candidates), "published": done, "failed": len(failed)}


def _task_row(conn, task_id: str, *, lock: bool = True) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if lock else ""
    return one(conn, "SELECT * FROM vlm_enrichment_task WHERE task_id=%s" + suffix, (task_id,))


def _validate_result_against_task(
    conn, result: orchestration_pb2.VlmTaskResult, task: dict[str, Any]
) -> None:
    validate_task_manifest(result.task)
    published = conn.execute(
        "SELECT contract_bytes FROM vlm_task_outbox WHERE task_id=%s", (task["task_id"],)
    ).fetchone()
    if not published:
        raise VlmDelayedError("vlm_result_task_contract_missing")
    try:
        canonical = orchestration_pb2.TaskInputManifest.FromString(published[0])
    except DecodeError as error:
        raise VlmDelayedError("vlm_result_task_contract_invalid") from error
    # 结果必须原样带回发布时的不可变 Manifest。仅比对表列会遗漏 plugin version、
    # attempt 等契约字段，可能把并非本次发布身份的 Observation 收敛为成功。
    canonical_bytes = canonical.SerializeToString(deterministic=True)
    received_bytes = result.task.SerializeToString(deterministic=True)
    if canonical_bytes != received_bytes:
        raise VlmDelayedError("vlm_result_task_binding_invalid")
    expected = {
        "task_id": task["task_id"],
        "execution_id": task["execution_id"],
        "asset_id": task["asset_id"],
        "content_hash": task["content_hash"],
        "media_locator": task["media_locator"],
        "stream_id": task["stream_id"],
        "source_id": task["source_id"],
        "source_item_id": task["source_item_id"],
        "material_unit_id": task["material_unit_id"],
        "prompt": task["prompt"],
    }
    if any(getattr(result.task, name) != value for name, value in expected.items()):
        raise VlmDelayedError("vlm_result_task_binding_invalid")
    if (
        result.task.plugin.plugin_id != task["plugin_id"]
        or result.task.plugin.artifact_digest != task["artifact_digest"]
        or result.task.plugin.config_hash != task["config_hash"]
    ):
        raise VlmDelayedError("vlm_result_plugin_binding_invalid")
    if (
        result.task.time_range.start_ms != task["start_ms"]
        or result.task.time_range.end_ms != task["end_ms"]
    ):
        raise VlmDelayedError("vlm_result_time_range_invalid")
    computed = result_digest(result)
    if result.result_digest != computed:
        raise VlmDelayedError("vlm_result_digest_invalid")
    if result.completed_at_unix_ms <= 0:
        raise VlmDelayedError("vlm_result_completed_at_invalid")
    if result.success:
        observation = result.observation
        if (
            not observation.observation_id
            or observation.modality != VLM_MODALITY
            or observation.stream_id != task["stream_id"]
            or observation.source_id != task["source_id"]
            or observation.source_item_id != task["source_item_id"]
            or observation.time_range.start_ms != task["start_ms"]
            or observation.time_range.end_ms != task["end_ms"]
            or observation.provenance.plugin != VLM_PLUGIN_ID
            or observation.provenance.artifact_digest != task["artifact_digest"]
            or observation.provenance.config_hash != task["config_hash"]
        ):
            raise VlmDelayedError("vlm_result_observation_invalid")
    elif result.observation.observation_id:
        raise VlmDelayedError("vlm_result_failure_observation_forbidden")


def _upsert_model_release(conn, observation: material_pb2.Observation) -> None:
    provenance = observation.provenance
    if not all(
        (
            provenance.model_release_id,
            provenance.model_id,
            provenance.model_version,
            provenance.model_artifact_digest,
            provenance.execution_backend,
            provenance.config_hash,
        )
    ):
        raise VlmDelayedError("vlm_result_model_provenance_invalid")
    existing = conn.execute(
        "SELECT name,artifact_hash FROM model_release WHERE model_release_id=%s",
        (provenance.model_release_id,),
    ).fetchone()
    if existing and existing != (provenance.model_id, provenance.model_artifact_digest):
        raise VlmDelayedError("vlm_result_model_release_conflict")
    conn.execute(
        """
        INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,config_hash)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (model_release_id) DO NOTHING
        """,
        (
            provenance.model_release_id,
            provenance.model_id,
            provenance.model_version,
            provenance.model_artifact_digest,
            provenance.execution_backend,
            provenance.config_hash,
        ),
    )


def _merge_material(conn, task: dict[str, Any], observation: material_pb2.Observation) -> None:
    row = conn.execute(
        """
        SELECT contract_bytes FROM material_unit WHERE material_unit_id=%s
        ORDER BY revision DESC LIMIT 1
        """,
        (task["material_unit_id"],),
    ).fetchone()
    if row:
        unit = material_pb2.MaterialUnit.FromString(row[0])
        unit.revision += 1
        if any(item.observation_id == observation.observation_id for item in unit.observations):
            return
        unit.observations.append(observation)
        unit.pending_enrichments[:] = [
            value for value in unit.pending_enrichments if value != VLM_MODALITY
        ]
    else:
        unit = material_pb2.MaterialUnit(
            material_unit_id=task["material_unit_id"],
            stream_id=task["stream_id"],
            time_range=common_pb2.TimeRange(start_ms=task["start_ms"], end_ms=task["end_ms"]),
            status="partial",
            revision=1,
            observations=[observation],
            source_refs=[
                material_pb2.SourceReference(
                    asset_id=task["asset_id"],
                    time_range=common_pb2.TimeRange(
                        start_ms=task["start_ms"], end_ms=task["end_ms"]
                    ),
                    content_hash=task["content_hash"],
                )
            ],
            pipeline_version="delayed-vlm-v1",
            created_at_unix_ms=int(time.time() * 1000),
        )
    materials.append_material(
        conn,
        unit,
        trace_id=f"vlm-task:{task['task_id']}",
        execution_id=task["execution_id"],
    )


def _advance_coverage(conn, task: dict[str, Any], *, state: str, reason: str = "") -> None:
    windows = conn.execute(
        """
        WITH latest AS (
            SELECT *,row_number() OVER (
                PARTITION BY execution_id,stream_id,start_ms,end_ms ORDER BY state_revision DESC
            ) AS ordinal
            FROM timeline_window_state WHERE execution_id=%s AND stream_id=%s
        ) SELECT start_ms,end_ms,sampling_state,modality_states,reason_codes
        FROM latest WHERE ordinal=1 AND end_ms>%s AND start_ms<%s ORDER BY start_ms
        """,
        (task["execution_id"], task["stream_id"], task["start_ms"], task["end_ms"]),
    ).fetchall()
    for start_ms, end_ms, sampling, modalities, reasons in windows:
        next_revision = conn.execute(
            """
            SELECT coalesce(max(state_revision),0)+1 FROM timeline_window_state
            WHERE execution_id=%s AND stream_id=%s AND start_ms=%s AND end_ms=%s
            """,
            (task["execution_id"], task["stream_id"], start_ms, end_ms),
        ).fetchone()[0]
        latest_modalities = dict(modalities or {})
        latest_reasons = dict(reasons or {})
        latest_modalities[VLM_MODALITY] = state
        if reason:
            latest_reasons[VLM_MODALITY] = reason
        else:
            latest_reasons.pop(VLM_MODALITY, None)
        conn.execute(
            """
            INSERT INTO timeline_window_state(
                execution_id,stream_id,start_ms,end_ms,state_revision,sampling_state,
                modality_states,reason_codes
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                task["execution_id"],
                task["stream_id"],
                start_ms,
                end_ms,
                next_revision,
                sampling,
                Jsonb(latest_modalities),
                Jsonb(latest_reasons),
            ),
        )


def _sync_delayed_execution(conn, execution_id: str) -> None:
    """慢路径收敛后更新 Console 投影；原编排 Run 的真实状态不由 NATS 改写。"""
    execution = conn.execute(
        "SELECT job_id FROM console_job_execution WHERE execution_id=%s FOR UPDATE", (execution_id,)
    ).fetchone()
    if not execution:
        return
    job_id = execution[0]
    states = [
        row[0]
        for row in conn.execute(
            "SELECT state FROM vlm_enrichment_task WHERE execution_id=%s", (execution_id,)
        ).fetchall()
    ]
    if not states or any(state not in {"succeeded", "failed"} for state in states):
        return
    failed = any(state == "failed" for state in states)
    final = "succeeded_with_partial_enrichment" if failed else "succeeded"
    conn.execute(
        """
        UPDATE console_job_execution SET state=%s,completed_at=coalesce(completed_at,now())
        WHERE execution_id=%s AND state='ready_for_review'
        """,
        (final, execution_id),
    )
    conn.execute(
        """
        UPDATE console_job_draft SET state='completed',completed_at=coalesce(completed_at,now()),
            error_code=CASE WHEN %s THEN 'vlm_enrichment_partial_failure' ELSE NULL END,
            error_detail=NULL WHERE id=%s AND state='ready_for_review'
        """,
        (failed, job_id),
    )


def apply_vlm_result(conn, result: orchestration_pb2.VlmTaskResult) -> dict[str, Any]:
    """以 task_id/result_digest 幂等收敛 VLM 返回，并在同一事务里写事实和 outbox。"""
    with conn.transaction():
        task = _task_row(conn, result.task.task_id)
        if not task:
            raise VlmDelayedError("vlm_result_task_not_found")
        _validate_result_against_task(conn, result, task)
        existing = conn.execute(
            "SELECT result_digest FROM vlm_enrichment_result WHERE task_id=%s", (task["task_id"],)
        ).fetchone()
        if existing:
            if existing[0] == result.result_digest:
                return {"task_id": task["task_id"], "replayed": True, "state": task["state"]}
            raise VlmDelayedError("vlm_result_conflict")
        if task["state"] in {"succeeded", "failed"}:
            raise VlmDelayedError("vlm_result_terminal_without_receipt")
        if result.success:
            _upsert_model_release(conn, result.observation)
            _merge_material(conn, task, result.observation)
            conn.execute(
                """
                INSERT INTO vlm_enrichment_result(
                    task_id,result_digest,observation_id,contract_bytes
                )
                VALUES (%s,%s,%s,%s)
                """,
                (
                    task["task_id"],
                    result.result_digest,
                    result.observation.observation_id,
                    result.SerializeToString(deterministic=True),
                ),
            )
            conn.execute(
                """
                UPDATE vlm_enrichment_task SET state='succeeded',result_digest=%s,reason_code='',
                    started_at=coalesce(started_at,now()),completed_at=now()
                WHERE task_id=%s
                """,
                (result.result_digest, task["task_id"]),
            )
            _advance_coverage(conn, task, state="observed")
        else:
            if result.retryable:
                # 可重试失败不 ACK 任务；Consumer 会让 AckWait 重投给可用节点。
                raise VlmDelayedError("vlm_result_retryable_must_not_be_published")
            conn.execute(
                """
                UPDATE vlm_enrichment_task SET state='failed',result_digest=%s,reason_code=%s,
                    started_at=coalesce(started_at,now()),completed_at=now()
                WHERE task_id=%s
                """,
                (result.result_digest, result.reason_code or "vlm_result_failed", task["task_id"]),
            )
            conn.execute(
                """
                INSERT INTO vlm_enrichment_result(
                    task_id,result_digest,observation_id,contract_bytes
                ) VALUES (%s,%s,NULL,%s)
                """,
                (
                    task["task_id"],
                    result.result_digest,
                    result.SerializeToString(deterministic=True),
                ),
            )
            _advance_coverage(
                conn,
                task,
                state="failed",
                reason=result.reason_code or "vlm_result_failed",
            )
        _sync_delayed_execution(conn, task["execution_id"])
        return {
            "task_id": task["task_id"],
            "replayed": False,
            "state": "succeeded" if result.success else "failed",
        }

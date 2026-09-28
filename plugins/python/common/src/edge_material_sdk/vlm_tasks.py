"""VLM 延迟满足任务的纯契约。

这里不连接 NATS、不读取数据库，也不解析媒体。发布器、结果融合器和宿主 VLM Consumer
共用这些常量与校验，避免把 stream/subject/定位符规则复制成几份会漂移的实现。
"""

from __future__ import annotations

import re

from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2

TASK_STREAM = "sensoryplex-tasks"
TASK_SUBJECT = "sensoryplex.tasks.vlm.v1"
RESULT_SUBJECT = "sensoryplex.results.vlm.v1"
VLM_DURABLE = "vlm-moondream-workers"
RESULT_DURABLE = "vlm-result-fuser"
MAX_PROMPT_CHARS = 2_000
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ASSET_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")


class VlmTaskContractError(ValueError):
    """可安全出现在状态行和审计日志里的稳定任务契约拒绝码。"""


def validate_task_manifest(task: orchestration_pb2.TaskInputManifest) -> None:
    """验证慢路径只能携带受控引用与半开时间锚点。"""
    required = (
        task.task_id,
        task.execution_id,
        task.run_id,
        task.asset_id,
        task.content_hash,
        task.media_locator,
        task.stream_id,
        task.source_id,
        task.source_item_id,
        task.material_unit_id,
        task.plugin.plugin_id,
        task.plugin.artifact_digest,
        task.plugin.config_hash,
    )
    if any(not value or len(value) > 256 for value in required):
        raise VlmTaskContractError("vlm_task_identity_invalid")
    if task.attempt < 1:
        raise VlmTaskContractError("vlm_task_attempt_invalid")
    if not _DIGEST.fullmatch(task.content_hash):
        raise VlmTaskContractError("vlm_task_content_hash_invalid")
    if (
        not _ASSET_ID.fullmatch(task.asset_id)
        or task.media_locator != f"media_asset:{task.asset_id}"
    ):
        raise VlmTaskContractError("vlm_task_media_locator_invalid")
    if task.time_range.start_ms < 0 or task.time_range.end_ms <= task.time_range.start_ms:
        raise VlmTaskContractError("vlm_task_time_range_invalid")
    if not task.prompt.strip() or len(task.prompt) > MAX_PROMPT_CHARS:
        raise VlmTaskContractError("vlm_task_prompt_invalid")
    if task.plugin.plugin_id != "org.sensoryplex.vlm-moondream":
        raise VlmTaskContractError("vlm_task_plugin_identity_invalid")
    if (
        not task.plugin.version
        or len(task.plugin.version) > 256
        or not _DIGEST.fullmatch(task.plugin.artifact_digest)
        or not _DIGEST.fullmatch(task.plugin.config_hash)
    ):
        raise VlmTaskContractError("vlm_task_plugin_identity_invalid")
    # 该路径按需从本机受控存储解码，绝不接受 Runtime raw descriptor / lease 作为消息输入。
    if task.descriptor_ref or task.observation_refs or task.data_plane_node_id:
        raise VlmTaskContractError("vlm_task_raw_data_reference_forbidden")


def result_digest(result: orchestration_pb2.VlmTaskResult) -> str:
    """仅返回可复算的结果摘要，不把 Observation 文本写入日志。"""
    import hashlib

    copy = orchestration_pb2.VlmTaskResult()
    copy.CopyFrom(result)
    copy.result_digest = ""
    return "sha256:" + hashlib.sha256(copy.SerializeToString(deterministic=True)).hexdigest()

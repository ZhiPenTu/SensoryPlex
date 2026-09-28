"""VLM 延迟满足的 Proto 与 JetStream 声明契约。

这些测试只验证控制消息边界和声明漂移；真实竞争拉取、ACK 与超时重投由
`make vlm-workqueue-check` 在 compose 的 JetStream 上验证。
"""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from edge_material_plugin_vlm_moondream import slow_consumer
from edge_material_sdk.generated.common.v1 import common_pb2
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2
from edge_material_sdk.vlm_tasks import VlmTaskContractError, validate_task_manifest
from sensoryplex_api.infrastructure import vlm_delayed

DIGEST = "sha256:" + "a" * 64


def manifest(**overrides):
    values = {
        "run_id": "run-contract",
        "task_id": "vlm-contract",
        "attempt": 1,
        "stream_id": "stream-contract",
        "content_hash": DIGEST,
        "execution_id": "execution-contract",
        "asset_id": "asset-contract",
        "media_locator": "media_asset:asset-contract",
        "time_range": common_pb2.TimeRange(start_ms=1_000, end_ms=1_033),
        "prompt": "Describe the scene.",
        "source_id": "source-contract",
        "source_item_id": "item-contract",
        "material_unit_id": "material-contract",
        "plugin": orchestration_pb2.PluginReference(
            plugin_id="org.sensoryplex.vlm-moondream",
            version="0.1.1",
            artifact_digest=DIGEST,
            config_hash=DIGEST,
        ),
    }
    values.update(overrides)
    return orchestration_pb2.TaskInputManifest(**values)


def test_manifest_is_time_anchored_and_never_carries_raw_data_or_paths():
    task = manifest()
    validate_task_manifest(task)

    task.media_locator = "file:///Users/someone/Movies/video.mp4"
    with pytest.raises(VlmTaskContractError, match="vlm_task_media_locator_invalid"):
        validate_task_manifest(task)


def test_manifest_binds_the_media_asset_and_the_immutable_plugin_identity():
    task = manifest(media_locator="media_asset:another-asset")
    with pytest.raises(VlmTaskContractError, match="vlm_task_media_locator_invalid"):
        validate_task_manifest(task)

    task = manifest()
    task.plugin.version = ""
    with pytest.raises(VlmTaskContractError, match="vlm_task_plugin_identity_invalid"):
        validate_task_manifest(task)

    task = manifest(content_hash="sha256:" + "z" * 64)
    with pytest.raises(VlmTaskContractError, match="vlm_task_content_hash_invalid"):
        validate_task_manifest(task)


def test_pull_consumer_uses_actual_available_memory_and_requires_an_explicit_watermark():
    assert (
        slow_consumer._linux_available_memory_bytes("MemAvailable:       12345 kB\n") == 12_641_280
    )
    assert (
        slow_consumer._macos_available_memory_bytes(
            "Pages free:                               12.\n"
            "Pages speculative:                        3.\n",
            page_size=16_384,
        )
        == 245_760
    )
    with pytest.raises(
        slow_consumer.SlowConsumerError, match="vlm_consumer_memory_watermark_required"
    ):
        slow_consumer._memory_watermark({"data_plane_mode": "local_decode"})

    task = manifest(descriptor_ref="lease://runtime/raw-frame")
    with pytest.raises(VlmTaskContractError, match="vlm_task_raw_data_reference_forbidden"):
        validate_task_manifest(task)


def test_workqueue_contract_detects_any_retention_or_subject_drift():
    expected = {
        "subjects": [vlm_delayed.TASK_SUBJECT, vlm_delayed.RESULT_SUBJECT],
        "storage": "file",
        "retention": "workqueue",
        "max_msgs": vlm_delayed.TASK_STREAM_MAX_MSGS,
        "max_bytes": vlm_delayed.TASK_STREAM_MAX_BYTES,
        "max_age": vlm_delayed.TASK_STREAM_MAX_AGE_S,
        "duplicate_window": vlm_delayed.TASK_STREAM_DUPLICATE_WINDOW_S,
    }
    assert vlm_delayed.task_stream_contract_diff(expected) == []

    drifted = {**expected, "retention": "limits", "subjects": [vlm_delayed.TASK_SUBJECT]}
    assert vlm_delayed.task_stream_contract_diff(drifted) == ["subjects", "retention"]


class NotFoundError(Exception):
    """模拟 nats-py 的稳定缺流异常类型。"""


class FakeJetStream:
    def __init__(self):
        self.created = None

    async def stream_info(self, _name):
        raise NotFoundError()

    async def add_stream(self, *, config):
        self.created = config
        return SimpleNamespace(config=config)


@pytest.mark.asyncio
async def test_publisher_creates_the_exact_workqueue_stream_once():
    js = FakeJetStream()
    await vlm_delayed.ensure_task_stream(js)

    assert js.created.name == vlm_delayed.TASK_STREAM
    assert sorted(js.created.subjects) == sorted(
        [vlm_delayed.TASK_SUBJECT, vlm_delayed.RESULT_SUBJECT]
    )
    assert "work_queue" in str(js.created.retention).lower()


def test_pull_consumer_module_entrypoint_invokes_main():
    """原生节点以 `python -m` 启动，缺入口会把未消费误读为队列空闲。"""
    source_path = "/workspace/plugins/python/processors/vlm-moondream/src"
    environment = {
        **os.environ,
        "PYTHONPATH": source_path + ":" + os.environ.get("PYTHONPATH", ""),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "edge_material_plugin_vlm_moondream.slow_consumer",
            "--nats-url",
            "nats://127.0.0.1:4222",
            "--media-root",
            "/tmp",
            "--config",
            "/tmp/config.json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode != 0
    assert "vlm_consumer_config_unreadable" in completed.stderr

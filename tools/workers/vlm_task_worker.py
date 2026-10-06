"""宿主单并发 VLM 队列执行器，使用已安装且摘要匹配的插件处理受控时间锚点。

调度与内存准入属于宿主执行器；模型和结果 provenance 仍来自不可变插件包。
每次只拉取一条引用，推理期间续租，收到结果发布确认后才 ACK，不预解码后续视频。
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import nats
from edge_material_plugin_vlm_moondream import slow_consumer as consumer
from edge_material_plugin_vlm_moondream.artifact import package_digest
from edge_material_plugin_vlm_moondream.plugin import PLUGIN_NAME, VisionVlmPlugin
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2
from edge_material_sdk.vlm_tasks import (
    RESULT_SUBJECT,
    TASK_SUBJECT,
    VLM_DURABLE,
    VlmTaskContractError,
    result_digest,
    validate_task_manifest,
)

MAX_ATTEMPTS = 8


def macos_available_memory(text: str, page_size: int) -> int:
    """采用 free + inactive + speculative，包含可回收文件缓存，不包含 wired/compressed。

    只数空闲页会把 macOS 已用作缓存的可回收内存误判成耗尽，导致队列永久不 fetch。
    """
    pages = 0
    for name in ("free", "inactive", "speculative"):
        match = re.search(rf"^Pages {name}:\s+(\d+)\.$", text, re.MULTILINE)
        if not match:
            raise consumer.SlowConsumerError("vlm_consumer_memory_probe_unavailable")
        pages += int(match.group(1))
    if page_size <= 0:
        raise consumer.SlowConsumerError("vlm_consumer_memory_probe_unavailable")
    return pages * page_size


def available_memory() -> int:
    if sys.platform != "darwin":
        return consumer._available_memory_bytes()
    output = subprocess.run(
        ["/usr/bin/vm_stat"], capture_output=True, text=True, check=True, timeout=2
    )
    return macos_available_memory(output.stdout, os.sysconf("SC_PAGE_SIZE"))


def decode_anchor(media: Path, start_ms: int, timeout_s: float = 30.0) -> bytes:
    try:
        return consumer._decode_anchor(media, start_ms, timeout_s)
    except consumer.SlowConsumerError as error:
        if str(error) == "vlm_consumer_anchor_decode_failed" and start_ms > 0:
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    f"{max(0, start_ms - 100) / 1000:.3f}",
                    "-i",
                    str(media),
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout_s,
            )
            if completed.returncode == 0 and completed.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                return completed.stdout
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-sseof",
                    "-1",
                    "-i",
                    str(media),
                    "-update",
                    "1",
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout_s,
            )
            if completed.returncode == 0 and completed.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                return completed.stdout
        raise


async def keep_lease(message, stop):
    """推理不阻塞事件循环；长于 AckWait 的推理仍只有一个持有者。"""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except TimeoutError:
            await message.in_progress()


async def run(args):
    config, config_hash, watermark = consumer._config(args.config)
    digest = package_digest()
    plugin = VisionVlmPlugin(artifact_digest=digest)
    plugin.configure(config)
    resolver = consumer.LocalMediaResolver(args.media_root)
    client = await nats.connect(args.nats_url, name="sensoryplex-native-vlm-worker")
    try:
        js = client.jetstream()
        await consumer._require_consumer(js)
        subscription = await js.pull_subscribe(TASK_SUBJECT, durable=VLM_DURABLE)
        print(json.dumps({"event": "vlm.worker.ready", "concurrency": 1}), flush=True)
        capacity_log_at = 0.0
        while True:
            free = await asyncio.to_thread(available_memory)
            if free < watermark:
                if time.monotonic() >= capacity_log_at:
                    print(
                        json.dumps(
                            {
                                "event": "vlm.worker.waiting_capacity",
                                "available_memory_bytes": free,
                                "required_memory_bytes": watermark,
                            }
                        ),
                        flush=True,
                    )
                    capacity_log_at = time.monotonic() + 30
                await asyncio.sleep(1)
                continue
            try:
                messages = await subscription.fetch(1, timeout=1)
            except TimeoutError:
                continue
            for message in messages:
                task = orchestration_pb2.TaskInputManifest()
                stop = asyncio.Event()
                heartbeat = asyncio.create_task(keep_lease(message, stop))
                try:
                    task.ParseFromString(message.data)
                    validate_task_manifest(task)
                    if (
                        task.plugin.plugin_id != PLUGIN_NAME
                        or task.plugin.artifact_digest != digest
                        or task.plugin.config_hash != config_hash
                        or task.prompt != str(config.get("prompt", "")).strip()
                    ):
                        raise consumer.SlowConsumerError("vlm_consumer_plugin_identity_mismatch")

                    def infer(task=task):
                        media = resolver.resolve(task)
                        png = decode_anchor(media, task.time_range.start_ms, 30)
                        return plugin.describe_decoded_anchor(
                            stream_id=task.stream_id,
                            source_id=task.source_id,
                            source_item_id=task.source_item_id,
                            start_ms=task.time_range.start_ms,
                            end_ms=task.time_range.end_ms,
                            png=png,
                            task_config_hash=task.plugin.config_hash,
                        )

                    observation = await asyncio.to_thread(infer)
                    result = orchestration_pb2.VlmTaskResult(
                        task=task,
                        success=True,
                        observation=observation,
                        completed_at_unix_ms=int(time.time() * 1000),
                    )
                    result.result_digest = result_digest(result)
                except (consumer.SlowConsumerError, VlmTaskContractError) as error:
                    if (
                        getattr(error, "retryable", False)
                        and message.metadata.num_delivered < MAX_ATTEMPTS
                    ):
                        await message.nak(delay=90)
                        continue
                    result = consumer._failure(task, str(error), retryable=False)
                except Exception as error:  # noqa: BLE001 - 日志只记错误类别，不记媒体/模型正文
                    print(
                        json.dumps(
                            {
                                "event": "vlm.worker.retry",
                                "task_id": task.task_id,
                                "error_type": type(error).__name__,
                            }
                        ),
                        flush=True,
                    )
                    if message.metadata.num_delivered < MAX_ATTEMPTS:
                        await message.nak(delay=90)
                        continue
                    result = consumer._failure(
                        task, "vlm_consumer_retries_exhausted", retryable=False
                    )
                finally:
                    stop.set()
                    await heartbeat
                try:
                    await js.publish(
                        RESULT_SUBJECT,
                        result.SerializeToString(deterministic=True),
                        headers={"Nats-Msg-Id": f"vlm-result:{task.task_id}"},
                        timeout=10,
                    )
                    await message.ack()
                    print(
                        json.dumps(
                            {
                                "event": "vlm.worker.result",
                                "task_id": task.task_id,
                                "start_ms": task.time_range.start_ms,
                                "success": result.success,
                                "reason_code": result.reason_code,
                            }
                        ),
                        flush=True,
                    )
                except Exception:  # noqa: BLE001 - 发布未确认时保留任务等待重投
                    await message.nak(delay=90)
    finally:
        plugin.close()
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))

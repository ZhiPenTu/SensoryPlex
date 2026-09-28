"""VLM 延迟满足 Pull Consumer。

它是插件侧的宿主原生进程，不依赖控制面内部模块：从 WorkQueue 一次拉一条受控 manifest，
把 `media_asset:<id>` 映射到本机预先配置的只读媒体根，按 `[start_ms,end_ms)` 抽一张 PNG，
调用 Moondream 后发布轻量 Observation 结果。NATS 中从不出现原始帧、宿主路径或模型密钥。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import nats
import nats.js.api as jsapi
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2
from edge_material_sdk.vlm_tasks import (
    RESULT_SUBJECT,
    TASK_STREAM,
    TASK_SUBJECT,
    VLM_DURABLE,
    VlmTaskContractError,
    result_digest,
    validate_task_manifest,
)

from .artifact import package_digest
from .plugin import MIN_LOCAL_DECODE_FREE_MEMORY_BYTES, PLUGIN_NAME, VisionVlmPlugin


class SlowConsumerError(RuntimeError):
    """稳定失败码；不携带本机路径、媒体名称或模型响应正文。"""

    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _memory_watermark(config: dict) -> int:
    """慢路径必须声明本机余量水位；缺失时不猜一个看似合理的值。"""
    value = config.get("min_free_memory_bytes")
    if isinstance(value, bool) or not isinstance(value, int):
        raise SlowConsumerError("vlm_consumer_memory_watermark_required")
    if value < MIN_LOCAL_DECODE_FREE_MEMORY_BYTES:
        raise SlowConsumerError("vlm_consumer_memory_watermark_invalid")
    return value


def _linux_available_memory_bytes(meminfo: str) -> int:
    """Linux 使用内核的 MemAvailable，而不是把 total 或 cache 当成可安全分配量。"""
    matched = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", meminfo, flags=re.MULTILINE)
    if not matched:
        raise SlowConsumerError("vlm_consumer_memory_probe_unavailable")
    return int(matched.group(1)) * 1024


def _macos_available_memory_bytes(vm_stat: str, *, page_size: int) -> int:
    """Apple Silicon 的统一内存只采用 free + speculative 页，宁可少拉取也不虚报余量。"""
    pages = 0
    for name in ("free", "speculative"):
        matched = re.search(rf"^Pages {name}:\s+(\d+)\.$", vm_stat, flags=re.MULTILINE)
        if matched:
            pages += int(matched.group(1))
    if pages <= 0 or page_size <= 0:
        raise SlowConsumerError("vlm_consumer_memory_probe_unavailable")
    return pages * page_size


def _available_memory_bytes() -> int:
    """读取当前可用主存；未知平台不 fetch，避免把总内存误认成可用余量。"""
    if sys.platform.startswith("linux"):
        try:
            return _linux_available_memory_bytes(Path("/proc/meminfo").read_text(encoding="utf-8"))
        except OSError as error:
            raise SlowConsumerError("vlm_consumer_memory_probe_unavailable") from error
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["vm_stat"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
                timeout=2,
            )
            if completed.returncode != 0:
                raise SlowConsumerError("vlm_consumer_memory_probe_unavailable")
            return _macos_available_memory_bytes(
                completed.stdout,
                page_size=int(os.sysconf("SC_PAGE_SIZE")),
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise SlowConsumerError("vlm_consumer_memory_probe_unavailable") from error
    raise SlowConsumerError("vlm_consumer_memory_probe_unavailable")


def _config(path: Path) -> tuple[dict, str, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SlowConsumerError("vlm_consumer_config_unreadable") from error
    if not isinstance(value, dict) or value.get("data_plane_mode") != "local_decode":
        raise SlowConsumerError("vlm_consumer_local_decode_config_required")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return value, "sha256:" + hashlib.sha256(encoded).hexdigest(), _memory_watermark(value)


class LocalMediaResolver:
    """仅把受控摘要解析为本机挂载文件；消息里的 locator 永远不是路径。"""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise SlowConsumerError("vlm_consumer_media_root_unavailable")
        self._verified: dict[tuple[int, int, int], str] = {}

    def resolve(self, task: orchestration_pb2.TaskInputManifest) -> Path:
        if task.media_locator != f"media_asset:{task.asset_id}":
            raise SlowConsumerError("vlm_consumer_media_locator_mismatch")
        candidate = (self.root / task.content_hash[7:]).resolve()
        if candidate.parent != self.root or not candidate.is_file():
            raise SlowConsumerError("vlm_consumer_media_unavailable", retryable=True)
        stat = candidate.stat()
        cache_key = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        actual = self._verified.get(cache_key)
        if actual is None:
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = "sha256:" + digest.hexdigest()
            self._verified = {cache_key: actual}
        if actual != task.content_hash:
            raise SlowConsumerError("vlm_consumer_media_digest_mismatch")
        return candidate


def _decode_anchor(media: Path, start_ms: int, timeout_s: float) -> bytes:
    """离线文件按锚点快速抽一帧；图片只留在本机 stdout 管道，绝不写日志或 NATS。"""
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{start_ms / 1000:.3f}",
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
    except FileNotFoundError as error:
        raise SlowConsumerError("vlm_consumer_decoder_unavailable") from error
    except subprocess.TimeoutExpired as error:
        raise SlowConsumerError("vlm_consumer_decode_timeout", retryable=True) from error
    if completed.returncode != 0 or not completed.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SlowConsumerError("vlm_consumer_anchor_decode_failed")
    return completed.stdout


def _failure(task, code: str, *, retryable: bool) -> orchestration_pb2.VlmTaskResult:
    result = orchestration_pb2.VlmTaskResult(
        task=task,
        success=False,
        retryable=retryable,
        reason_code=code,
        completed_at_unix_ms=int(time.time() * 1000),
    )
    result.result_digest = result_digest(result)
    return result


async def _require_consumer(js):
    """Consumer 只验证/创建自己的 durable；WorkQueue stream 必须由底座发布器创建。"""
    try:
        stream = await js.stream_info(TASK_STREAM)
    except Exception as error:  # noqa: BLE001
        raise SlowConsumerError("vlm_task_stream_missing") from error
    config = stream.config
    if sorted(config.subjects or []) != sorted([TASK_SUBJECT, RESULT_SUBJECT]):
        raise SlowConsumerError("vlm_task_stream_contract_mismatch")
    try:
        info = await js.consumer_info(TASK_STREAM, VLM_DURABLE)
    except Exception as error:  # noqa: BLE001
        if type(error).__name__ != "NotFoundError":
            raise SlowConsumerError("vlm_task_consumer_unavailable", retryable=True) from error
        await js.add_consumer(
            TASK_STREAM,
            config=jsapi.ConsumerConfig(
                durable_name=VLM_DURABLE,
                filter_subject=TASK_SUBJECT,
                ack_policy=jsapi.AckPolicy.EXPLICIT,
                ack_wait=90,
                max_deliver=8,
            ),
        )
        return
    current = info.config
    if (
        current.filter_subject != TASK_SUBJECT
        or int(current.ack_wait or 0) != 90
        or int(current.max_deliver or 0) != 8
    ):
        raise SlowConsumerError("vlm_task_consumer_contract_mismatch")


async def run(
    *, nats_url: str, media_root: Path, config_path: Path, decode_timeout_s: float
) -> int:
    config, config_hash, min_free_memory_bytes = _config(config_path)
    artifact_digest = package_digest()
    plugin = VisionVlmPlugin(artifact_digest=artifact_digest)
    plugin.configure(config)
    resolver = LocalMediaResolver(media_root)
    try:
        client = await asyncio.wait_for(
            nats.connect(nats_url, name="sensoryplex-vlm-moondream-pull", connect_timeout=10),
            timeout=20,
        )
    except Exception as error:  # noqa: BLE001
        raise SlowConsumerError("vlm_task_nats_unreachable", retryable=True) from error
    try:
        js = client.jetstream()
        await _require_consumer(js)
        subscription = await js.pull_subscribe(TASK_SUBJECT, durable=VLM_DURABLE)
        print(json.dumps({"event": "vlm.consumer.ready", "durable": VLM_DURABLE}), flush=True)
        next_capacity_log_at = 0.0
        while True:
            available_memory_bytes = await asyncio.to_thread(_available_memory_bytes)
            if available_memory_bytes < min_free_memory_bytes:
                if time.monotonic() >= next_capacity_log_at:
                    print(
                        json.dumps(
                            {
                                "event": "vlm.consumer.waiting_capacity",
                                "available_memory_bytes": available_memory_bytes,
                                "min_free_memory_bytes": min_free_memory_bytes,
                            }
                        ),
                        flush=True,
                    )
                    next_capacity_log_at = time.monotonic() + 30
                await asyncio.sleep(1)
                continue
            try:
                messages = await subscription.fetch(1, timeout=1)
            except TimeoutError:
                continue
            for message in messages:
                task = orchestration_pb2.TaskInputManifest()
                try:
                    task.ParseFromString(message.data)
                    validate_task_manifest(task)
                    if (
                        task.plugin.plugin_id != PLUGIN_NAME
                        or task.plugin.artifact_digest != artifact_digest
                        or task.plugin.config_hash != config_hash
                        or task.prompt != str(config.get("prompt", "")).strip()
                    ):
                        raise SlowConsumerError("vlm_consumer_plugin_identity_mismatch")
                    media = resolver.resolve(task)
                    png = _decode_anchor(media, task.time_range.start_ms, decode_timeout_s)
                    observation = plugin.describe_decoded_anchor(
                        stream_id=task.stream_id,
                        source_id=task.source_id,
                        source_item_id=task.source_item_id,
                        start_ms=task.time_range.start_ms,
                        end_ms=task.time_range.end_ms,
                        png=png,
                        task_config_hash=task.plugin.config_hash,
                    )
                    result = orchestration_pb2.VlmTaskResult(
                        task=task,
                        success=True,
                        observation=observation,
                        completed_at_unix_ms=int(time.time() * 1000),
                    )
                    result.result_digest = result_digest(result)
                except (SlowConsumerError, VlmTaskContractError) as error:
                    if getattr(error, "retryable", False):
                        await message.nak(delay=90)
                        continue
                    result = _failure(task, str(error), retryable=False)
                except Exception:  # noqa: BLE001 - 模型/解码未知错误可重试但不泄露细节
                    await message.nak(delay=90)
                    continue
                try:
                    await js.publish(
                        RESULT_SUBJECT,
                        result.SerializeToString(deterministic=True),
                        headers={"Nats-Msg-Id": f"vlm-result:{task.task_id}"},
                        timeout=10,
                    )
                except Exception:  # noqa: BLE001 - 结果未确认则不 ACK 原任务
                    await message.nak(delay=90)
                    continue
                await message.ack()
    finally:
        plugin.close()
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nats-url", default=os.getenv("SENSORYPLEX_NATS_URL", ""))
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--decode-timeout-s", type=float, default=30.0)
    arguments = parser.parse_args(argv)
    if not arguments.nats_url or not 1 <= arguments.decode_timeout_s <= 300:
        raise SystemExit("vlm_consumer_arguments_invalid")
    try:
        return asyncio.run(
            run(
                nats_url=arguments.nats_url,
                media_root=arguments.media_root,
                config_path=arguments.config,
                decode_timeout_s=arguments.decode_timeout_s,
            )
        )
    except SlowConsumerError as error:
        raise SystemExit(error.code) from error


if __name__ == "__main__":
    raise SystemExit(main())

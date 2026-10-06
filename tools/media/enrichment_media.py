"""同机受控适配器：授权下载、私有暂存，Rust 持有解码数据面与共享内存。"""

import asyncio
import hashlib
import os
import re
import tempfile
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import grpc
from edge_material_sdk.generated.common.v1 import common_pb2 as common

MAX_ASSET_BYTES = 2 << 30


def download(client, task, lease_id, target):
    request = urllib.request.Request(
        client.main_url + f"/v1/agent/enrichments/{task.task_id}/asset?lease_id={lease_id}",
        headers={"Authorization": "Bearer " + client.session_token},
    )
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as handle:
        for block in iter(lambda: response.read(1 << 20), b""):
            size += len(block)
            if size > MAX_ASSET_BYTES:
                raise ValueError("enrichment_asset_limit_exceeded")
            digest.update(block)
            handle.write(block)
    if "sha256:" + digest.hexdigest() != task.content_hash:
        raise ValueError("enrichment_asset_digest_mismatch")


@asynccontextmanager
async def media_descriptor(client, state, task, lease_id):
    if not re.fullmatch(r"execution_[a-f0-9]{32}", task.execution_id):
        raise ValueError("enrichment_execution_identity_invalid")
    native_root = Path(__file__).resolve().parents[2]
    binary = (
        Path(os.environ.get("SENSORYPLEX_RUNTIME_BIN", ""))
        if os.environ.get("SENSORYPLEX_RUNTIME_BIN")
        else (
            Path("/Users/tuzhipeng/Documents/SensoryPlex/target/release/sensoryplex-runtime")
            if Path(
                "/Users/tuzhipeng/Documents/SensoryPlex/target/release/sensoryplex-runtime"
            ).is_file()
            else native_root / "target/release/sensoryplex-runtime"
        )
    )
    if not binary.is_file():
        raise ValueError("enrichment_runtime_unavailable")
    with tempfile.TemporaryDirectory(prefix="sensoryplex-enrichment-") as directory:
        private = Path(directory)
        existing = (
            Path(state["state_dir"])
            / "plugins/task-executions"
            / task.execution_id
            / ("asset-" + task.content_hash.removeprefix("sha256:"))
        )
        fallback = (
            Path("/tmp/sensoryplex-task-executions")
            / task.execution_id
            / ("asset-" + task.content_hash.removeprefix("sha256:"))
        )
        media = (
            existing
            if existing.is_file()
            else (fallback if fallback.is_file() else private / "asset")
        )
        if media == private / "asset":
            await asyncio.to_thread(download, client, task, lease_id, media)
        manifest, output = private / "task.pb", private / "input.pb"
        manifest.write_bytes(task.SerializeToString(deterministic=True))
        producer = await asyncio.create_subprocess_exec(
            str(binary),
            "enrichment-media",
            str(manifest),
            str(media),
            str(output),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            until = time.monotonic() + 40
            while not output.is_file():
                if producer.returncode is not None:
                    raise ValueError("enrichment_media_decode_failed")
                if time.monotonic() >= until:
                    raise ValueError("enrichment_media_decode_timeout")
                await asyncio.sleep(0.05)
            descriptor = common.BufferDescriptor.FromString(output.read_bytes())
            async with grpc.aio.insecure_channel(descriptor.locator.handoff_endpoint) as channel:
                await asyncio.wait_for(channel.channel_ready(), timeout=5)
            yield descriptor
        finally:
            if producer.returncode is None:
                producer.terminate()
            try:
                await asyncio.wait_for(producer.wait(), timeout=5)
            except TimeoutError:
                producer.kill()
                await producer.wait()

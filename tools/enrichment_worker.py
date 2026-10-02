"""宿主通用补全 Consumer：只调用标准 Process，不导入业务或数据库模块。"""

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import time
import urllib.error
from contextlib import asynccontextmanager
from pathlib import Path

import grpc
import nats
from edge_material_sdk.enrichments import STREAM, require_stream, result_digest, subscription
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as pb
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.generated.runtime.v1 import runtime_pb2_grpc as rpc

from tools.enrichment_media import media_descriptor
from tools.node_agent import NodeAgentClient
from tools.node_agent_platform import read_endpoint_file


def available_memory():
    """读真实系统水位；探测失败不猜可用内存。"""
    if os.uname().sysname == "Darwin":
        output = subprocess.check_output(["vm_stat"], text=True, timeout=5)
        page = int(re.search(r"page size of (\d+) bytes", output)[1])
        return page * sum(
            int(re.search(label + r":\s+(\d+)", output)[1])
            for label in ["Pages free", "Pages inactive", "Pages speculative"]
        )
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return int(values["MemAvailable"].strip().split()[0]) * 1024


async def invoke(client, state, message, row):
    task = pb.EnrichmentTask.FromString(message.data)
    task_id = task.task_id
    try:
        acquired = await asyncio.to_thread(
            client._post, f"/v1/agent/enrichments/{task_id}:claim", {}, token=client.session_token
        )
    except RuntimeError as error:
        if "enrichment_cancelled" in str(error):
            await message.ack()
            return
        raise
    if acquired.get("terminal") or acquired.get("staged"):
        await message.ack()
        return
    if base64.b64decode(acquired["task_b64"], validate=True) != message.data:
        raise ValueError("enrichment_queue_manifest_mismatch")
    if task.route_id != row["route_id"] or task.data_plane_node_id != state["node_id"]:
        raise ValueError("enrichment_route_mismatch")
    registry = Path(state["state_dir"]) / "plugins/hot-deploy.json"
    runtime_entry = json.loads(registry.read_text())[acquired["runtime_instance_id"]]
    if (
        runtime_entry["artifact_digest"] != task.plugin.artifact_digest
        or runtime_entry["config_hash"] != task.plugin.config_hash
        or runtime_entry.get("desired_state") != "running"
    ):
        raise ValueError("enrichment_runtime_identity_mismatch")
    endpoint = read_endpoint_file(Path(runtime_entry["endpoint_file"]))["endpoint"]
    stop = asyncio.Event()

    async def renew():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except TimeoutError:
                await asyncio.to_thread(
                    client._post,
                    f"/v1/agent/enrichments/{task_id}:renew",
                    {"lease_id": acquired["lease_id"]},
                    token=client.session_token,
                )
                await message.in_progress()

    async def process():
        inputs = [
            runtime.PluginInput(
                observation=material.Observation.FromString(base64.b64decode(value, validate=True))
            )
            for value in acquired["inputs_b64"]
        ]
        receipts = []

        @asynccontextmanager
        async def scoped_inputs():
            if inputs:
                yield inputs
            else:
                async with media_descriptor(
                    client, state, task, acquired["lease_id"]
                ) as descriptor:
                    receipts.append(
                        pb.EnrichmentInputReceipt(
                            source_item_id=descriptor.buffer_id
                            + "@"
                            + descriptor.content_hash[7:23],
                            content_hash=descriptor.content_hash,
                            kind=descriptor.kind,
                            time_range=descriptor.time_range,
                        )
                    )
                    yield [runtime.PluginInput(buffer=descriptor)]

        request = runtime.ProcessRequest(
            context=common.RequestContext(
                request_id=task_id,
                trace_id="enrichment:" + task.execution_id,
                pipeline_run_id=task.run_id,
                stream_id=task.stream_id,
                source_id=task.source_id,
                idempotency_key=task_id,
                attempt=acquired["attempt"],
                deadline_unix_ms=min(
                    task.deadline_unix_ms,
                    int(time.time() * 1000) + (task.process_timeout_ms or 300000),
                ),
                privacy_policy={"data_egress": "local_only"},
            ),
            inputs=inputs,
            processor_release_id=task.release_id,
        )
        async with scoped_inputs() as provided:
            del request.inputs[:]
            request.inputs.extend(provided)
            async with grpc.aio.insecure_channel(
                endpoint, options=[("grpc.max_receive_message_length", 4 << 20)]
            ) as channel:
                response = await rpc.ProcessorPluginServiceStub(channel).Process(
                    request,
                    timeout=min(
                        300,
                        max(0.1, (request.context.deadline_unix_ms - time.time() * 1000) / 1000),
                    ),
                )
                return response, receipts

    keeper = asyncio.create_task(renew())
    processing = asyncio.create_task(process())
    try:
        done, _ = await asyncio.wait([keeper, processing], return_when=asyncio.FIRST_COMPLETED)
        if keeper in done:
            await keeper
            raise ValueError("enrichment_lease_renewal_lost")
        response, receipts = await processing
        result = pb.EnrichmentResult(
            task=task,
            observations=response.observations,
            outcome=response.outcome,
            outcome_reason=response.outcome_reason,
            input_receipts=receipts,
        )
        if response.HasField("error"):
            if response.error.retryable and acquired["attempt"] < task.max_attempts:
                await asyncio.to_thread(
                    client._post,
                    f"/v1/agent/enrichments/{task_id}:retry",
                    {"lease_id": acquired["lease_id"], "reason_code": response.error.reason_code},
                    token=client.session_token,
                )
                await message.nak(delay=2)
                return
            result.error.CopyFrom(response.error)
            result.error.retryable = False
            if response.error.retryable:
                result.error.reason_code = "enrichment_retries_exhausted"
        result.result_digest = result_digest(result)
        await asyncio.to_thread(
            client._post,
            f"/v1/agent/enrichments/{task_id}:result",
            {
                "lease_id": acquired["lease_id"],
                "result_b64": base64.b64encode(
                    result.SerializeToString(deterministic=True)
                ).decode(),
            },
            token=client.session_token,
        )
        await message.ack()
        print(
            json.dumps(
                {
                    "event": "enrichment.staged",
                    "task_id": task_id,
                    "observations": len(result.observations),
                    "reason": result.outcome_reason or result.error.reason_code,
                }
            ),
            flush=True,
        )
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) else "enrichment_worker_failed"
        if isinstance(error, grpc.aio.AioRpcError):
            reason = (
                "enrichment_process_timeout"
                if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
                else "enrichment_plugin_unavailable"
            )
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", reason):
            reason = "enrichment_worker_failed"
        try:
            retried = await asyncio.to_thread(
                client._post,
                f"/v1/agent/enrichments/{task_id}:retry",
                {"lease_id": acquired["lease_id"], "reason_code": reason},
                token=client.session_token,
            )
        except RuntimeError as failure:
            if "enrichment_cancelled" in str(failure):
                await message.ack()
                return
            raise
        if retried.get("terminal"):
            await message.ack()
        else:
            await message.nak(delay=2)
        print(
            json.dumps({"event": "enrichment.retry", "task_id": task_id, "reason": reason}),
            flush=True,
        )
    finally:
        stop.set()
        for task_future in (processing, keeper):
            if not task_future.done():
                task_future.cancel()
        await asyncio.gather(processing, keeper, return_exceptions=True)


async def run(args):
    state = json.loads(Path(args.state_file).read_text())
    state["state_dir"] = str(Path(args.state_file).resolve().parent)
    client = NodeAgentClient(state["main_url"], state["node_id"], state["session_token"])
    bus = await nats.connect(
        args.nats_url,
        name="sensoryplex-generic-enrichment-worker",
        connect_timeout=5,
        max_reconnect_attempts=32,
    )
    js = bus.jetstream()
    await require_stream(js)
    pulls = {}
    control_failures = 0
    print(json.dumps({"event": "enrichment.consumer.ready", "concurrency": 1}), flush=True)
    try:
        while True:
            if await asyncio.to_thread(available_memory) < args.min_free_memory_bytes:
                await asyncio.sleep(2)
                continue
            try:
                routes = await asyncio.to_thread(
                    client._get_json,
                    "/v1/agent/enrichments/routes?node_id=" + state["node_id"],
                )
            except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError):
                # 控制面短暂停机不丢消费身份；有界退避，凭据与原始异常不进入日志。
                control_failures = min(control_failures + 1, 6)
                delay = min(30, 2**control_failures)
                print(
                    json.dumps(
                        {
                            "event": "enrichment.consumer.waiting_control",
                            "reason": "enrichment_control_unavailable",
                            "retry_after_s": delay,
                        }
                    ),
                    flush=True,
                )
                await asyncio.sleep(delay)
                continue
            if control_failures:
                print(json.dumps({"event": "enrichment.consumer.control_restored"}), flush=True)
                control_failures = 0
            active = {row["route_id"] for row in routes["items"]}
            for key in list(pulls):
                if key not in active:
                    await pulls.pop(key).unsubscribe()
                    # 路由只在有待办时驻留；清理已终结路由的 durable，避免版本升级无限累积。
                    await js.delete_consumer(STREAM, "enrichment-" + key)
            for row in routes["items"]:
                key = row["route_id"]
                if key not in pulls:
                    pulls[key] = await subscription(js, row["subject"], "enrichment-" + key)
                try:
                    messages = await pulls[key].fetch(1, timeout=1)
                except TimeoutError:
                    continue
                for message in messages:
                    try:
                        await invoke(client, state, message, row)
                    except Exception as error:
                        reason = (
                            str(error)
                            if isinstance(error, ValueError)
                            else "enrichment_delivery_failed"
                        )
                        if not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", reason):
                            reason = "enrichment_delivery_failed"
                        print(
                            json.dumps(
                                {
                                    "event": "enrichment.consumer.failed",
                                    "reason": reason,
                                }
                            ),
                            flush=True,
                        )
                        await message.nak(delay=90)
            if not routes["items"]:
                await asyncio.sleep(1)
    finally:
        await bus.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--nats-url", default="nats://127.0.0.1:24222")
    parser.add_argument("--min-free-memory-bytes", type=int, default=268435456)
    args = parser.parse_args()
    if not 268435456 <= args.min_free_memory_bytes <= 64 << 30:
        parser.error("min_free_memory_bytes_out_of_range")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

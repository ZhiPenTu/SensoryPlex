"""VLM 延迟满足的 JetStream WorkQueue 验收。

本工具运行在 api 容器内，只验证 NATS 的真实传输语义，不伪造模型或媒体结果：

1. 在本次运行专属的 WorkQueue stream 上启动两个独立 Python Pull Consumer 进程；
2. 发布十条合法的时间锚点 `TaskInputManifest`，确认每条只被其中一个进程 ACK；
3. 让一个进程取走一条任务但故意不 ACK，确认 AckWait 后另一进程收到第二次投递并 ACK；
4. 删除**本次创建的精确临时 stream**，不触碰 `sensoryplex-tasks` 或开发任务。

它证明竞争消费和失败重投的消息层语义，不证明 Moondream 模型可用、授权媒体可本地解码，
也不等于完整 Golden Path。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import subprocess
import sys
import time
import uuid

import nats
import nats.js.api as jsapi
from edge_material_sdk.generated.common.v1 import common_pb2
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2
from edge_material_sdk.vlm_tasks import validate_task_manifest

DIGEST = "sha256:" + "a" * 64
ACK_WAIT_S = 2
MAX_DELIVER = 4


class VerificationError(RuntimeError):
    """验收失败只包含可公开的控制面状态，不包含消息正文或连接凭据。"""


def _task(index: int) -> bytes:
    task = orchestration_pb2.TaskInputManifest(
        run_id="run-workqueue-check",
        task_id=f"vlm-workqueue-{index}",
        attempt=1,
        stream_id="stream-workqueue-check",
        content_hash=DIGEST,
        execution_id="execution-workqueue-check",
        asset_id="asset-workqueue-check",
        media_locator="media_asset:asset-workqueue-check",
        time_range=common_pb2.TimeRange(start_ms=index * 1_000, end_ms=index * 1_000 + 33),
        prompt="Describe the scene.",
        source_id="source-workqueue-check",
        source_item_id=f"frame-workqueue-{index}",
        material_unit_id=f"material-workqueue-{index}",
        plugin=orchestration_pb2.PluginReference(
            plugin_id="org.sensoryplex.vlm-moondream",
            version="0.1.1",
            artifact_digest=DIGEST,
            config_hash=DIGEST,
        ),
    )
    validate_task_manifest(task)
    return task.SerializeToString(deterministic=True)


def _write_json(path: pathlib.Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


async def _worker(
    *,
    nats_url: str,
    stream: str,
    subject: str,
    durable: str,
    output: pathlib.Path,
    expected: int,
    ack: bool,
) -> None:
    client = await nats.connect(nats_url, name="sensoryplex-vlm-workqueue-check", connect_timeout=5)
    try:
        js = client.jetstream()
        subscription = await js.pull_subscribe(subject, durable=durable, stream=stream)
        _write_json(output.with_suffix(".ready"), {"ready": True})
        received: list[str] = []
        deliveries: list[int] = []
        for _ in range(expected):
            messages = await subscription.fetch(1, timeout=15)
            if len(messages) != 1:
                raise VerificationError("workqueue_fetch_count_invalid")
            message = messages[0]
            task = orchestration_pb2.TaskInputManifest.FromString(message.data)
            validate_task_manifest(task)
            received.append(task.task_id)
            deliveries.append(int(message.metadata.num_delivered))
            if ack:
                await message.ack()
        _write_json(output, {"task_ids": received, "deliveries": deliveries})
    finally:
        await client.close()


def _worker_main(arguments: argparse.Namespace) -> int:
    try:
        asyncio.run(
            _worker(
                nats_url=arguments.nats_url,
                stream=arguments.stream,
                subject=arguments.subject,
                durable=arguments.durable,
                output=arguments.output,
                expected=arguments.expected,
                ack=not arguments.without_ack,
            )
        )
    except Exception as error:  # noqa: BLE001 - 子进程只打印稳定异常类/码
        print(getattr(error, "code", type(error).__name__), file=sys.stderr)
        return 1
    return 0


def _start_worker(arguments: argparse.Namespace, output: pathlib.Path, *, expected: int, ack: bool):
    command = [
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "--worker",
        "--nats-url",
        arguments.nats_url,
        "--stream",
        arguments.stream,
        "--subject",
        arguments.subject,
        "--durable",
        arguments.durable,
        "--output",
        str(output),
        "--expected",
        str(expected),
    ]
    if not ack:
        command.append("--without-ack")
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def _wait_ready(outputs: list[pathlib.Path], processes: list[subprocess.Popen]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if all(path.with_suffix(".ready").is_file() for path in outputs):
            return
        for process in processes:
            if process.poll() is not None:
                raise VerificationError("workqueue_consumer_start_failed")
        time.sleep(0.05)
    raise VerificationError("workqueue_consumer_start_timeout")


def _wait_process(process: subprocess.Popen, *, label: str) -> None:
    try:
        exit_code = process.wait(timeout=25)
    except subprocess.TimeoutExpired as error:
        process.kill()
        raise VerificationError(f"{label}_timeout") from error
    if exit_code:
        detail = (process.stderr.read() if process.stderr else "").strip()
        raise VerificationError(f"{label}_failed:{detail[:80]}")


async def _verify(arguments: argparse.Namespace) -> dict:
    client = await nats.connect(
        arguments.nats_url,
        name="sensoryplex-vlm-workqueue-acceptance",
        connect_timeout=5,
        max_reconnect_attempts=1,
    )
    stream_created = False
    workers: list[subprocess.Popen] = []
    try:
        js = client.jetstream()
        await js.add_stream(
            config=jsapi.StreamConfig(
                name=arguments.stream,
                subjects=[arguments.subject],
                storage=jsapi.StorageType.FILE,
                retention=jsapi.RetentionPolicy.WORK_QUEUE,
                max_msgs=32,
                max_bytes=1 << 20,
                max_age=300,
                duplicate_window=30,
            )
        )
        stream_created = True
        await js.add_consumer(
            arguments.stream,
            config=jsapi.ConsumerConfig(
                durable_name=arguments.durable,
                filter_subject=arguments.subject,
                ack_policy=jsapi.AckPolicy.EXPLICIT,
                ack_wait=ACK_WAIT_S,
                max_deliver=MAX_DELIVER,
            ),
        )
        workspace = pathlib.Path(arguments.workspace)
        first = workspace / "first.json"
        second = workspace / "second.json"
        workers = [
            _start_worker(arguments, first, expected=5, ack=True),
            _start_worker(arguments, second, expected=5, ack=True),
        ]
        _wait_ready([first, second], workers)
        for index in range(10):
            await js.publish(
                arguments.subject,
                _task(index),
                headers={"Nats-Msg-Id": f"workqueue-check:{index}"},
                timeout=5,
            )
        for index, worker in enumerate(workers, start=1):
            _wait_process(worker, label=f"workqueue_consumer_{index}")
        consumed = [
            *json.loads(first.read_text(encoding="utf-8"))["task_ids"],
            *json.loads(second.read_text(encoding="utf-8"))["task_ids"],
        ]
        expected_ids = {f"vlm-workqueue-{index}" for index in range(10)}
        if set(consumed) != expected_ids or len(consumed) != len(set(consumed)):
            raise VerificationError("workqueue_competition_identity_mismatch")
        if (
            not json.loads(first.read_text(encoding="utf-8"))["task_ids"]
            or not json.loads(second.read_text(encoding="utf-8"))["task_ids"]
        ):
            raise VerificationError("workqueue_competition_not_shared")

        unacked = workspace / "unacked.json"
        taker = _start_worker(arguments, unacked, expected=1, ack=False)
        workers.append(taker)
        _wait_ready([unacked], [taker])
        await js.publish(
            arguments.subject,
            _task(99),
            headers={"Nats-Msg-Id": "workqueue-check:redelivery"},
            timeout=5,
        )
        _wait_process(taker, label="workqueue_unacked_consumer")
        await asyncio.sleep(ACK_WAIT_S + 1)

        redelivered = workspace / "redelivered.json"
        reclaimer = _start_worker(arguments, redelivered, expected=1, ack=True)
        workers.append(reclaimer)
        _wait_ready([redelivered], [reclaimer])
        _wait_process(reclaimer, label="workqueue_redelivery_consumer")
        redelivery = json.loads(redelivered.read_text(encoding="utf-8"))
        if redelivery["task_ids"] != ["vlm-workqueue-99"] or redelivery["deliveries"] != [2]:
            raise VerificationError("workqueue_ackwait_redelivery_invalid")
        return {
            "competition_consumed": len(consumed),
            "competition_workers": 2,
            "redelivery_count": redelivery["deliveries"][0],
        }
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
        if stream_created:
            await js.delete_stream(arguments.stream)
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--stream", default=f"sensoryplex-vlm-wq-check-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--subject", default="sensoryplex.acceptance.tasks.vlm.v1")
    parser.add_argument("--durable", default="vlm-workqueue-acceptance")
    parser.add_argument(
        "--workspace",
        type=pathlib.Path,
        default=pathlib.Path(f"/tmp/vlm-workqueue-check-{uuid.uuid4().hex}"),
    )
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--expected", type=int, default=1)
    parser.add_argument("--without-ack", action="store_true")
    arguments = parser.parse_args()
    if arguments.worker:
        if arguments.output is None or arguments.expected < 1:
            raise SystemExit("workqueue_worker_arguments_invalid")
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        return _worker_main(arguments)
    if not arguments.nats_url:
        raise SystemExit("workqueue_nats_url_required")
    arguments.workspace.mkdir(parents=True, exist_ok=True)
    for path in arguments.workspace.glob("*.json*"):
        path.unlink()
    try:
        print(json.dumps({"event": "vlm.workqueue.check", **asyncio.run(_verify(arguments))}))
    except Exception as error:  # noqa: BLE001 - 顶层只输出稳定失败码
        raise SystemExit(getattr(error, "code", str(error).split(":", 1)[0])) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

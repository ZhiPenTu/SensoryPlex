"""模型 worker（调度侧）：发现数据面里的输入（帧或音频段），按契约调用插件，收集 observation。

三个角色必须分开，否则"跨进程"只是口号：
1. Runtime（生产者，持有字节与保留表）；
2. 本进程（worker：只做发现、构造请求、调用插件、对账）；
3. 插件进程（`edge_material_plugin_vlm_moondream`：自己按 lease 读字节、跑模型）。

本进程**不读**任何输入字节：它从 `List` 只拿到不透明句柄、区间与摘要；真正的字节由插件
通过 `BufferHandoffService.Acquire` 领取。因此 worker 的日志与报告里不可能出现原始媒体。

一个 worker 服务所有插件：`--input-kind` 决定它挑哪种 buffer，`--plugin-config` 把插件自己的
Start 配置整份传下去，而不是让 worker 猜每个插件要什么键。

worker 支持契约里的**两条输入路径**，且在报告里写明用的是哪条：

- `buffer`（默认）：`--data-plane` + `--input-kind`，去数据面按种类挑保留项；
- `observation`：`--input-observations <json>`，把上游插件已经产出的事实（例如 OCR 的文字块）
  直接交给下游插件。这条路径**不碰数据面、也没有 lease 可还**，因此报告的 `drain` 是空账目，
  而不是"漏了没还"——账目里 `leases` 明确写 0。
"""

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
import pathlib
import socket
import sys
import time
from dataclasses import dataclass

import grpc
from edge_material_sdk import BufferReadError, LeaseBufferReader
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.media.v1 import handoff_pb2, handoff_pb2_grpc
from edge_material_sdk.generated.runtime.v1 import runtime_pb2, runtime_pb2_grpc
from google.protobuf import json_format, struct_pb2

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 以脚本方式运行时 `sys.path[0]` 是 tools/ 本身，`tools.model_limits` 需要仓库根。
    sys.path.insert(0, str(ROOT))

from tools import model_limits  # noqa: E402  - 必须在 sys.path 调整之后

DEFAULT_DEADLINE_MS = 180_000
DEFAULT_TIMEOUT_S = 300.0
# 可重试拒绝的重试预算与退避初值；次数有上限（不做无限重试），退避指数增长并封顶 1s。
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_MS = 100


def as_struct(config: dict) -> struct_pb2.Struct:
    return json_format.ParseDict(config, struct_pb2.Struct())


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def to_descriptor(entry) -> common.BufferDescriptor:
    """把清单里的一条保留项变成插件输入。字节不在消息里，只有句柄、区间与摘要。"""
    return common.BufferDescriptor(
        buffer_id=entry.buffer_id,
        kind=entry.kind,
        memory_kind="cpu_shared_memory",
        locator=common.BufferLocator(offset=entry.offset_bytes, length=entry.length_bytes),
        format=entry.format,
        stream_id=entry.stream_id,
        time_range=entry.time_range,
        content_hash=entry.content_hash,
    )


def make_request(entry, source_id: str, run_id: str, release_id: str, deadline_ms: int):
    descriptor = to_descriptor(entry)
    context = common.RequestContext(
        request_id=f"req_{stable_id(entry.buffer_id, entry.content_hash)}",
        trace_id=f"trace_{run_id}",
        pipeline_run_id=run_id,
        stream_id=entry.stream_id,
        source_id=source_id,
        deadline_unix_ms=int(time.time() * 1000) + deadline_ms,
        attempt=1,
        idempotency_key=stable_id(entry.buffer_id, entry.content_hash),
        privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
    )
    return runtime_pb2.ProcessRequest(
        context=context,
        inputs=[runtime_pb2.PluginInput(buffer=descriptor)],
        processor_release_id=release_id,
    )


def make_observation_request(
    observation,
    source_id: str,
    run_id: str,
    release_id: str,
    deadline_ms: int,
):
    """`observation` 输入路径：把上游事实原样转发，worker 不解释 payload 语义。"""
    context = common.RequestContext(
        request_id=f"req_{stable_id(observation.observation_id, observation.content_hash)}",
        trace_id=f"trace_{run_id}",
        pipeline_run_id=run_id,
        stream_id=observation.stream_id,
        source_id=source_id,
        deadline_unix_ms=int(time.time() * 1000) + deadline_ms,
        attempt=1,
        idempotency_key=stable_id(observation.observation_id, observation.content_hash),
        privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
    )
    return runtime_pb2.ProcessRequest(
        context=context,
        inputs=[runtime_pb2.PluginInput(observation=observation)],
        processor_release_id=release_id,
    )


def load_input_observations(path: str) -> list:
    """读一批上游观测：接受数组，或含 `observations` 键的对象（例如上一段 worker 的报告）。"""
    try:
        document = json.loads(pathlib.Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        raise ValueError("input_observations_unreadable") from None
    if isinstance(document, dict) and "observations" in document:
        document = document["observations"]
    if not isinstance(document, list) or not document:
        raise ValueError("input_observations_empty")
    observations = []
    for entry in document:
        observation = material.Observation()
        try:
            json_format.ParseDict(entry, observation)
        except Exception:  # noqa: BLE001 - 解析细节不进报告，原因串保持稳定
            raise ValueError("input_observation_unparsable") from None
        observations.append(observation)
    return observations


@dataclass(frozen=True)
class Job:
    """一次插件调用：请求 + 报告里那一行账目 + 来源摘要（用于对账 observation 是否同一批字节）。"""

    request: object
    frame: dict
    source_digest: str
    # 输入自己的半开区间（buffer 是字节窗口，observation 是上游事实的锚点）。
    source_range_ms: tuple


class AdmissionFailure(Exception):
    """准入阶段的失败：稳定原因串进报告；配置错误用与运行失败不同的退出码。"""


def runtime_tier(arguments) -> model_limits.TierCap | None:
    """运行时是分级上限的权威（ADR-015 §5 / ADR-019 §5）；给了 `--runtime` 就必须拿到答案。"""
    if not arguments.runtime:
        return None
    channel = grpc.insecure_channel(arguments.runtime)
    try:
        stub = runtime_pb2_grpc.RuntimeServiceStub(channel)
        try:
            described = stub.DescribeCapabilities(
                runtime_pb2.DescribeCapabilitiesRequest(), timeout=arguments.timeout_s
            )
        except grpc.RpcError as error:
            # 连不上不能退化成"没有上限"：那正好是这条链要禁止的静默降级。
            raise AdmissionFailure(
                f"runtime_capabilities_unavailable:{error.code().name}"
            ) from None
    finally:
        channel.close()
    return model_limits.tier_from_residency(described.residency)


def process_one(
    plugin, job: Job, *, ledger, timeout_s: float, deadline_ms: int, base_backoff_ms: int
) -> tuple[list[dict], list[dict]]:
    """一次输入：有界重试；返回（报告里的 frames 条目，产出的观测）。

    可重试的拒绝不再是终态。旧实现把它当成单条失败记一行就过去了，于是这条输入**静默消失**
    而运行照样成功；现在先按 `max_attempts` 重试，预算用尽才落成 `retry_exhausted:<原因>`。
    """
    attempt = 0
    while True:
        attempt += 1
        # 每次尝试都刷新 deadline：否则第 2 次会带着一条已经过期的 deadline 上去，
        # 插件的 `deadline_expired`（可重试）会让"重试"退化成永远失败。
        job.request.context.deadline_unix_ms = int(time.time() * 1000) + deadline_ms
        started = time.time()
        with ledger.call():
            response = plugin.Process(job.request, timeout=timeout_s)
        elapsed_ms = round((time.time() - started) * 1000, 1)
        if not response.HasField("error"):
            if not response.observations:
                # 契约保证"没有 error 就至少有一条观测"：违反时必须显式失败，
                # 否则这条输入在账目里既不算成功也不算失败。
                ledger.record_failed()
                return (
                    [
                        {
                            **job.frame,
                            "error": {
                                "code": common.ErrorCode.Name(common.INTERNAL_PLUGIN_ERROR),
                                "reason": "empty_plugin_result",
                                "retryable": False,
                                "attempts": attempt,
                            },
                            "elapsed_ms": elapsed_ms,
                            "attempts": attempt,
                        }
                    ],
                    [],
                )
            entries: list[dict] = []
            observations: list[dict] = []
            for observation in response.observations:
                observations.append(json_format.MessageToDict(observation))
                entries.append(
                    {
                        **job.frame,
                        "observation_id": observation.observation_id,
                        "time_range_ms": [
                            observation.time_range.start_ms,
                            observation.time_range.end_ms,
                        ],
                        "source_time_range_ms": list(job.source_range_ms),
                        "source_digest": job.source_digest,
                        "observation_digest": observation.content_hash,
                        "elapsed_ms": elapsed_ms,
                        "attempts": attempt,
                    }
                )
            ledger.record_completed()
            return entries, observations
        error = response.error
        if model_limits.should_retry(
            retryable=error.retryable, attempt=attempt, max_attempts=ledger.max_attempts
        ):
            ledger.record_retry(error.reason_code)
            delay_ms = model_limits.retry_delay_ms(attempt, base_ms=base_backoff_ms)
            if delay_ms:
                time.sleep(delay_ms / 1000)
            continue
        if error.retryable:
            ledger.record_exhausted()
            reason = f"retry_exhausted:{error.reason_code}"
        else:
            ledger.record_failed()
            reason = error.reason_code
        return (
            [
                {
                    **job.frame,
                    "error": {
                        "code": common.ErrorCode.Name(error.code),
                        "reason": reason,
                        "retryable": bool(error.retryable),
                        "attempts": attempt,
                    },
                    "elapsed_ms": elapsed_ms,
                    "attempts": attempt,
                }
            ],
            [],
        )


def process_inputs(
    plugin,
    jobs: list[Job],
    report: dict,
    timeout_s: float,
    *,
    ledger,
    deadline_ms: int = DEFAULT_DEADLINE_MS,
    base_backoff_ms: int = DEFAULT_RETRY_BACKOFF_MS,
) -> str | None:
    """有界并发地处理输入：在飞的插件调用与提交窗口都不超过 `ledger.limit`。

    单条失败仍记进报告后继续；无法确认插件状态的 RPC 失败中止整轮（此时"插件到底收没收"
    不可判定，继续跑只会写出更不可信的报告）。报告里的顺序仍等于输入顺序——并发不该让
    报告变成不可复盘的东西。
    """
    outcomes: list[tuple[list[dict], list[dict]] | None] = [None] * len(jobs)
    fatal: str | None = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=ledger.limit) as pool:
        pending: dict[concurrent.futures.Future, int] = {}
        queue = collections.deque(range(len(jobs)))
        while (queue or pending) and fatal is None:
            while queue and len(pending) < ledger.limit:
                index = queue.popleft()
                pending[
                    pool.submit(
                        process_one,
                        plugin,
                        jobs[index],
                        ledger=ledger,
                        timeout_s=timeout_s,
                        deadline_ms=deadline_ms,
                        base_backoff_ms=base_backoff_ms,
                    )
                ] = index
            done, _ = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                index = pending.pop(future)
                try:
                    outcomes[index] = future.result()
                except grpc.RpcError as error:
                    fatal = f"plugin_process_failed:{error.code().name}"
        if fatal is not None:
            for future in pending:
                future.cancel()
    for outcome in outcomes:
        if outcome is None:
            continue
        entries, observations = outcome
        report["observations"].extend(observations)
        report["frames"].extend(entries)
    return fatal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-plane", default=None, help="Runtime 数据面 gRPC 地址（buffer 模式必需）"
    )
    parser.add_argument("--plugin", required=True, help="插件 gRPC 地址")
    parser.add_argument("--source-id", default="source_local_file")
    parser.add_argument("--model-endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="moondream:v2")
    parser.add_argument(
        "--input-kind",
        default="video_frame",
        help="要处理的 buffer 种类（video_frame / audio_segment）",
    )
    parser.add_argument(
        "--plugin-config",
        default=None,
        help="插件 Start 配置的 JSON 文件；给出时替代 --model-endpoint/--model",
    )
    parser.add_argument("--max-inputs", type=int, default=None, help="最多处理多少个输入")
    parser.add_argument("--max-frames", type=int, default=2, help="--max-inputs 的兼容别名")
    parser.add_argument(
        "--input-observations",
        default=None,
        help="observation 模式：上游观测的 JSON（数组，或含 observations 键的上游报告）",
    )
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--runtime",
        default=None,
        help="Runtime 控制端点；给出时按 DescribeCapabilities.residency 核对分级并发上限",
    )
    parser.add_argument(
        "--model-parallelism",
        default=None,
        help="本次请求的并发路数；缺省时读 SENSORYPLEX_MODEL_PARALLELISM，再缺省按运行时分级",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="可重试拒绝的重试预算（含首次调用，至少 1）",
    )
    parser.add_argument(
        "--retry-backoff-ms",
        type=int,
        default=DEFAULT_RETRY_BACKOFF_MS,
        help="重试退避初值（毫秒，指数增长并封顶 1s；0 表示不等待）",
    )
    parser.add_argument("--report", required=True)
    arguments = parser.parse_args()
    max_inputs = arguments.max_inputs if arguments.max_inputs is not None else arguments.max_frames
    observation_mode = arguments.input_observations is not None
    if not observation_mode and not arguments.data_plane:
        print("ai worker failed: data_plane_required_in_buffer_mode")
        return 2

    report = {
        "worker_pid": os.getpid(),
        "worker_host": socket.gethostname(),
        # 报告必须写清走的是哪条输入路径：两条路径的账目含义不同（见模块说明）。
        "input_mode": "observation" if observation_mode else "buffer",
        "data_plane": arguments.data_plane,
        "plugin": arguments.plugin,
        "input_kind": None if observation_mode else arguments.input_kind,
        "observations": [],
        # 处理过的输入清单（视频帧或音频段）。保留 `frames` 这个键名，验收脚本依赖它。
        "frames": [],
        "failures": [],
        # 模型并发准入与账目（ADR-021）；准入结果先写，处理结束后再补结算数字。
        "model_concurrency": {"state": "pending"},
    }

    handoff = None
    reader = None
    if not observation_mode:
        data_channel = grpc.insecure_channel(arguments.data_plane)
        handoff = handoff_pb2_grpc.BufferHandoffServiceStub(data_channel)
        reader = LeaseBufferReader(
            arguments.data_plane, ttl_ms=30_000, timeout_s=arguments.timeout_s
        )
    plugin_channel = grpc.insecure_channel(arguments.plugin)
    plugin = runtime_pb2_grpc.ProcessorPluginServiceStub(plugin_channel)

    def fail(reason: str, *, exit_code: int = 1) -> int:
        report["failures"].append(reason)
        pathlib.Path(arguments.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        print(f"ai worker failed: {reason}")
        return exit_code

    # 准入在连接插件之前：坏值与越界都不该跑起来，更不该跑了一半才说。
    try:
        admission = model_limits.admit(
            env_value=os.environ.get(model_limits.MODEL_PARALLELISM_ENV),
            flag_value=arguments.model_parallelism,
            tier=runtime_tier(arguments),
        )
    except (model_limits.ResidentLimitError, AdmissionFailure) as error:
        # 报告里写明"被拒"而不是留一个看起来像成功的小数字；退出码 2 = 配置错误。
        report["model_concurrency"] = {"state": "rejected", "reason": str(error)}
        return fail(str(error), exit_code=2)
    if arguments.max_attempts < 1:
        report["model_concurrency"] = {"state": "rejected", "reason": "invalid_max_attempts"}
        return fail(f"invalid_max_attempts: --max-attempts={arguments.max_attempts}", exit_code=2)
    if arguments.retry_backoff_ms < 0:
        report["model_concurrency"] = {
            "state": "rejected",
            "reason": "invalid_retry_backoff_ms",
        }
        return fail(
            f"invalid_retry_backoff_ms: --retry-backoff-ms={arguments.retry_backoff_ms}",
            exit_code=2,
        )
    ledger = model_limits.InFlightLedger(limit=admission.limit, max_attempts=arguments.max_attempts)
    report["model_concurrency"] = {**admission.as_report(), **ledger.as_report(submitted=0)}

    try:
        description = plugin.Describe(runtime_pb2.DescribeRequest(), timeout=arguments.timeout_s)
    except grpc.RpcError as error:
        return fail(f"plugin_describe_failed:{error.code().name}")
    report["plugin_description"] = {
        "name": description.name,
        "version": description.version,
        "protocol": description.protocol,
        "consumes": list(description.consumes),
        "produces": list(description.produces),
        "memory_kinds": list(description.memory_kinds),
    }

    start_config = {"ttl_ms": 30_000, "timeout_s": arguments.timeout_s}
    if arguments.data_plane:
        # 只有挂数据面的插件才需要 handoff_endpoint；observation 路径的插件不接数据面，
        # 多塞一个它不认识的键会被它自己的 schema 拒绝（additionalProperties: false）。
        start_config["handoff_endpoint"] = arguments.data_plane
    if arguments.plugin_config:
        # 插件自己的配置整份传下去；数据面地址由 worker 决定，不被文件覆盖。
        try:
            supplied = json.loads(pathlib.Path(arguments.plugin_config).read_text())
        except (OSError, json.JSONDecodeError) as error:
            return fail(f"plugin_config_unreadable:{type(error).__name__}")
        if not isinstance(supplied, dict):
            return fail("plugin_config_not_an_object")
        start_config.update(supplied)
    else:
        start_config["endpoint"] = arguments.model_endpoint
        start_config["model"] = arguments.model
    validation = plugin.ValidateConfig(
        runtime_pb2.ValidateConfigRequest(config=as_struct(start_config)),
        timeout=arguments.timeout_s,
    )
    report["config_valid"] = validation.valid
    if not validation.valid:
        return fail(f"config_rejected:{list(validation.field_errors)}")

    lifecycle = plugin.Start(
        runtime_pb2.StartRequest(config=as_struct(start_config)), timeout=arguments.timeout_s
    )
    report["start_state"] = lifecycle.state
    if lifecycle.state != "ready":
        return fail(f"plugin_start_failed:{lifecycle.state}:{lifecycle.error.reason_code}")

    jobs: list[Job] = []
    if observation_mode:
        try:
            observations = load_input_observations(arguments.input_observations)[:max_inputs]
        except ValueError as error:
            return fail(str(error))
        # 沿用 buffer 模式的键名：验收脚本用同一个字段比较"这条路径上有多少输入"。
        report["input_buffers_available"] = len(observations)
        run_id = "run_" + stable_id(
            arguments.source_id, *[observation.observation_id for observation in observations]
        )
        for observation in observations:
            jobs.append(
                Job(
                    request=make_observation_request(
                        observation,
                        arguments.source_id,
                        run_id,
                        description.version,
                        DEFAULT_DEADLINE_MS,
                    ),
                    frame={
                        "input_observation_id": observation.observation_id,
                        "kind": observation.modality,
                        "stream_id": observation.stream_id,
                    },
                    source_digest=observation.content_hash,
                    source_range_ms=(
                        observation.time_range.start_ms,
                        observation.time_range.end_ms,
                    ),
                )
            )
    else:
        try:
            listing = handoff.List(handoff_pb2.ListRetainedRequest(), timeout=arguments.timeout_s)
        except grpc.RpcError as error:
            return fail(f"data_plane_list_failed:{error.code().name}")

        stats = listing.stats
        report["runtime_stats"] = {
            "retained_total": stats.retained_total,
            "retained": stats.retained,
            "released_total": stats.released_total,
            "expired_total": stats.expired_total,
            "arena_live_slabs": stats.arena_live_slabs,
            "retained_by_kind": dict(stats.retained_by_kind),
        }
        report["segment_name_hint"] = "opaque" if listing.segment_name else "missing"

        inputs = [entry for entry in listing.buffers if entry.kind == arguments.input_kind]
        report["input_buffers_available"] = len(inputs)
        if arguments.input_kind == "video_frame":
            # 兼容既有验收脚本的键名。
            report["video_buffers_available"] = len(inputs)
        if not inputs:
            return fail(f"no_{arguments.input_kind}_buffer_to_process")
        # 取"最大输入优先"：同一条流里它最有信息量；仍然只处理有限的 max_inputs 条。
        selected = sorted(inputs, key=lambda entry: entry.length_bytes, reverse=True)[:max_inputs]
        run_id = "run_" + stable_id(arguments.source_id, *[entry.buffer_id for entry in selected])
        for entry in selected:
            jobs.append(
                Job(
                    request=make_request(
                        entry,
                        arguments.source_id,
                        run_id,
                        description.version,
                        DEFAULT_DEADLINE_MS,
                    ),
                    frame={"buffer_id": entry.buffer_id, "kind": entry.kind},
                    source_digest=entry.content_hash,
                    source_range_ms=(entry.time_range.start_ms, entry.time_range.end_ms),
                )
            )

    failure = process_inputs(
        plugin,
        jobs,
        report,
        arguments.timeout_s,
        ledger=ledger,
        base_backoff_ms=arguments.retry_backoff_ms,
    )
    # 结算数字统一在最后写入：不管后面哪一步失败，报告里的准入结果与账目都是同一份。
    report["model_concurrency"] = {**admission.as_report(), **ledger.as_report(submitted=len(jobs))}
    if failure:
        return fail(failure)

    if observation_mode:
        # 这条路径不碰数据面：没有保留项要丢弃，也没有 lease 要归还。账目显式写 0，
        # 而不是留空——空账目和"忘了对账"在报告里看起来一样。
        report["drain"] = {"discarded": 0, "failures": [], "leases": 0}
        report["runtime_stats_after"] = {"leased": 0, "leases": 0}
    else:
        # 数据面要求每条保留都有归宿：调度方必须显式确认"已投递、不消费"的 buffer，
        # 不能用自己的过滤规则把剩下的条目留在保留表里（否则运行以账目失败收尾）。
        drain = {"discarded": 0, "failures": []}
        processed_ids = {frame["buffer_id"] for frame in report["frames"] if "buffer_id" in frame}
        for entry in listing.buffers:
            if entry.buffer_id in processed_ids:
                continue
            try:
                reader.discard(entry.buffer_id)
                drain["discarded"] += 1
            except BufferReadError as error:
                drain["failures"].append(
                    {"buffer_id": entry.buffer_id, "reason": error.reason_code}
                )
            except grpc.RpcError as error:
                drain["failures"].append(
                    {"buffer_id": entry.buffer_id, "reason": error.code().name}
                )
        report["drain"] = drain

        # 处理器必须自己归还 lease：这里读到的账目是"插件读完就还了"的证据。
        try:
            after = handoff.Stats(handoff_pb2.HandoffStatsRequest(), timeout=arguments.timeout_s)
        except grpc.RpcError as error:
            return fail(f"data_plane_stats_failed:{error.code().name}")
        report["runtime_stats_after"] = {
            "retained_total": after.stats.retained_total,
            "retained": after.stats.retained,
            "released_total": after.stats.released_total,
            "expired_total": after.stats.expired_total,
            "leased": after.stats.leased,
            "arena_live_slabs": after.stats.arena_live_slabs,
        }

    if reader is not None:
        reader.close()
    stopped = plugin.Stop(runtime_pb2.StopRequest(), timeout=arguments.timeout_s)
    report["stop_state"] = stopped.state
    report["checks_failed"] = len(report["failures"])
    pathlib.Path(arguments.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"ai worker ok: observations={len(report['observations'])} "
        f"frames={len(jobs)} plugin={description.name} "
        f"model_parallelism={admission.limit} peak_in_flight={ledger.peak_in_flight}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

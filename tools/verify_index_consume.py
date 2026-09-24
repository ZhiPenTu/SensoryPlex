"""常驻消费端到端验收（ADR-025）：真实 outbox → 真实 relay → 真实 JetStream → 消费 → 向量 → 检索。

这是 ① 的闭环证据：`tools/verify_outbox_relay.py` 只验"发布这一跳"（事件确认发到 JetStream），
本脚本验"事件被消费成事实"这一跳，并且**在同一个进程里**把新写入的向量检索出来。

角色（除了那一条刻意手写的坏事件，其余都是真实进程/真实服务，没有替身）：

1. 本脚本（编排 + 对账，在**主机**执行：Milvus Lite 的数据目录是进程独占的本地文件，
   BGE 权重也只在 macOS 主机上——理由同 `semantic-check` / `index-check`）；
2. 真实写侧 `sensoryplex_api.infrastructure.materials.append_material`
   （素材 + 观测 + outbox 行在同一个事务里落下，事件由它产生，不是本脚本手写的）；
3. `python -m sensoryplex_relay.cli --once`（真实 relay 进程：outbox → JetStream）；
4. 真实 NATS JetStream（每个场景自建独立 stream 与前缀，用完删掉，不碰开发用的
   `sensoryplex-events`）；
5. `python -m sensoryplex_index_worker.cli serve --consume`（**常驻消费 + 检索面同进程**：
   Milvus Lite 的目录锁是进程级的，所以"事件驱动写入的向量"必须由持有向量库的那个进程
   自己消费出来——拆两个进程要么新向量对检索面不可见，要么第二个进程直接
   `vector_store_locked`）；
6. 真实 BGE 权重（编码发生在 serve 进程里，用与检索面**同一个**编码器实例）；
7. 真实 PostgreSQL（本次新建隔离 schema，跑真实迁移）。

判定标准（全部来自真实执行）：

- 事件驱动写入：消费完成后 `embedding_record` 有 ready 行（带 `vector_ref` 与 `indexed_at`）、
  `consumed_event` 有记账行、模型身份被登记进 `model_release`；
- **同一进程**的检索面立刻能检索到这条新向量：查询文本经真实 BGE 编码后命中该素材，
  排序正确、`model_release_id` 与编码器同源、非 owner 命中被回查丢弃并计入 `unindexed_hits`；
- 重放不重复：同一批事件在**另一个 durable**（另一个消费视角）下被重新投递并重新 sink，
  向量行数不增（`embedding_id` 是确定性的），而记账按 consumer 作用域各自留痕；
- 坏事件 fail-stop：事实回查不到的事件重投到 `max_deliver` 上限后，进程以
  `event_retry_exhausted` + 退出码 3 显式停止，不 ack、不记账、不静默丢；
- 启动即失败：stream 缺失（`event_stream_missing` 且**绝不**自动建流）、durable 契约漂移
  （`event_consumer_contract_mismatch`）、NATS 不可达（`nats_unreachable`）都必须在接上之前
  以退出码 1 失败——半启动的常驻进程是最难排查的失败形态；
- 分级背压准入（ADR-027）：本脚本给消费侧**显式注入**一档（`large`），就绪行与状态行的
  `inflight_state` 都必须是 `admitted` 且带上真实的上限数字；发布侧在本脚本被显式摘掉档位
  变量，因此必须诚实地报 `not_injected`（`inflight_capacity=0`）——"没注入"与"注入后通过"
  是两种不同结论，同一个产物里各出现一次；
- 不外泄：状态行与就绪行里没有 DSN、主机路径、素材文本、令牌或向量库引用。

刻意不做的事（未验证边界，见 ADR-025 的"未验证"一节）：不验服务端 Milvus 形态下的多进程
拓扑，不验 dead-letter 分流，不验跨主机 NATS 集群，不验 `ack_wait` 到期后的自动重投
（本脚本走的是 nak 路径）。
"""

import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import grpc  # noqa: E402
import nats  # noqa: E402
import nats.js.api as jsapi  # noqa: E402
import psycopg  # noqa: E402
from edge_material_sdk.generated.common.v1 import common_pb2 as common  # noqa: E402
from edge_material_sdk.generated.common.v1.common_pb2 import EventEnvelope  # noqa: E402
from edge_material_sdk.generated.index.v1 import index_pb2, index_pb2_grpc  # noqa: E402
from edge_material_sdk.generated.material.v1 import material_pb2 as material  # noqa: E402
from google.protobuf.json_format import MessageToDict  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from sensoryplex_api.infrastructure import materials as api_materials  # noqa: E402
from sensoryplex_api.settings import Settings  # noqa: E402
from sensoryplex_index_worker import consumer  # noqa: E402
from sensoryplex_relay import residency  # noqa: E402
from sensoryplex_relay.relay import DEFAULT_BATCH as RELAY_DEFAULT_BATCH  # noqa: E402

from tools import macos_resident  # noqa: E402
from tools.migrate import migrate  # noqa: E402
from tools.verify_embed import DEFAULT_MODEL_DIR, DEFAULT_MODEL_FILE  # noqa: E402
from tools.verify_index import derive_database_url, describe_target  # noqa: E402
from tools.verify_model import Process, check, free_port  # noqa: E402

CLI_MODULE = "sensoryplex_index_worker.cli"
RELAY_MODULE = "sensoryplex_relay.cli"
PRINCIPAL = "index-consume-acceptance-owner"
INTRUDER = "index-consume-acceptance-intruder"
SOURCE = "source_index_consume_acceptance"
STREAM = "stream_index_consume_acceptance"
ASSET = "asset_index_consume_acceptance"
DIGEST = "sha256:" + "d" * 64
# 上游观测的身份：本脚本提供的是**文本**输入，不是模型输出，所以登记成一个"无推理"身份。
UPSTREAM_RELEASE = "acceptance:upstream-text-input@0"
UPSTREAM_MODEL = "acceptance-upstream-text"
# collection 名由 API 配置给出：检索面会用编码器实测出来的 key 与它比对，不等即拒绝启动。
KEY = Settings(database_url="postgresql://placeholder/none").index_vector_index_key
DIMENSION = int(KEY.rsplit("_d", 1)[1].split("_", 1)[0])
SURFACE_TOKEN = "index-consume-acceptance-token-0123456789abcdef"
# 分级背压准入（ADR-027）：本脚本**显式注入**一次档位，让"准入通过"这件事有真实执行证据，
# 而不是"没注入所以没判"的空过。档位数值只从 `tools/macos_resident.py` 的分级表取；
# 消费侧"在飞未 ack"的深度就是 `--consume-batch` 的默认值，必须落在本档上限之内。
ACCEPTANCE_TIER = next(tier for tier in macos_resident.TIERS if tier.name == "large")
ACCEPTANCE_INFLIGHT = consumer.DEFAULT_BATCH
UNREACHABLE_NATS = "nats://127.0.0.1:1"
CLI_TIMEOUT_S = 900.0
STATUS_TIMEOUT_S = 300.0
READY_TIMEOUT_S = 600.0
# 两条素材：一条与查询语义相关，一条无关。查询必须把相关的那条排第一，而不是"返回点什么"。
DOCUMENTS = (
    ("material_consume_alpha", "设备巡检记录：三号机组轴承温度偏高，需要更换润滑油并复测振动"),
    ("material_consume_beta", "安全培训通知：本周五下午两点在二楼会议室进行消防演练，全员参加"),
)
QUERY = "机组轴承温度异常"
EXPECTED_MATERIAL = "material_consume_alpha"
# 状态行的字段集合是契约：多一个字段就意味着"往里塞了别的东西"。
STATUS_FIELDS = frozenset(
    {
        "event",
        "cycle",
        "stream",
        "subject",
        "durable",
        "received",
        "consumed",
        "skipped",
        "failed",
        "embedded_total",
        "consumed_total",
        "duplicate_total",
        "skipped_total",
        "failed_total",
        "error_code",
        "error_detail",
        # 分级背压（ADR-027）：只放状态串、档位名与两个数字。
        "inflight_state",
        "inflight_declared",
        "inflight_capacity",
        "resident_tier",
    }
)
LEAK_NEEDLES = ("postgresql://", "/Users/", "password", "Bearer", SURFACE_TOKEN, "milvus://")


def residency_environ(tier: macos_resident.Tier | None) -> dict[str, str]:
    """给子进程的环境：注入档位就是 `admitted`，摘掉就是 `not_injected`。

    摘掉而不是"随宿主环境"：开发者终端里若加载过 `resident.env`，发布这一跳默认的
    `--batch 200` 会撞上任何一档上限而拒绝启动——验收结论不能取决于跑验收的那个 shell。
    """

    environ = dict(os.environ)
    environ.pop(residency.TIER_VAR, None)
    environ.pop(residency.CAPACITY_VAR, None)
    if tier is not None:
        environ[residency.TIER_VAR] = tier.name
        environ[residency.CAPACITY_VAR] = str(tier.event_queue_capacity)
    return environ


# ── 事实落库：真实写侧（素材 + 观测 + outbox 同一个事务） ────────────────────


def upstream_observation(material_id: str, text: str):
    observation = material.Observation(
        observation_id=f"obs_consume_{material_id}",
        modality="ocr_blocks",
        stream_id=STREAM,
        source_id=SOURCE,
        source_item_id=f"item_consume_{material_id}",
        time_range=common.TimeRange(start_ms=0, end_ms=1000),
        confidence_unavailable_reason="acceptance_text_input_has_no_model_confidence",
        content_hash=DIGEST,
        quality_state="final",
        timing_source="media_pts",
        created_at_unix_ms=int(time.time() * 1000),
        provenance=material.Provenance(
            plugin="org.sensoryplex.verify-index-consume",
            plugin_version="0.1.0",
            artifact_digest=DIGEST,
            model_release_id=UPSTREAM_RELEASE,
            model_id=UPSTREAM_MODEL,
            model_version="0.1.0",
            config_hash=DIGEST,
            execution_backend="no_inference",
            model_artifact_digest=DIGEST,
        ),
    )
    observation.payload.update({"blocks": [{"text": text}]})
    return observation


def seed_references(conn) -> None:
    conn.execute(
        "INSERT INTO media_source(source_id,type,uri_redacted,owner) "
        "VALUES (%s,'file','private://not-exposed',%s)",
        (SOURCE, PRINCIPAL),
    )
    conn.execute(
        "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
        "VALUES (%s,%s,now(),'stopped')",
        (STREAM, SOURCE),
    )
    conn.execute(
        "INSERT INTO media_asset(asset_id,stream_id,object_uri,sha256,codec,duration_ms) "
        "VALUES (%s,%s,'private://not-exposed',%s,'acceptance',%s)",
        (ASSET, STREAM, DIGEST, len(DOCUMENTS) * 1000),
    )
    for material_id, _ in DOCUMENTS:
        conn.execute(
            "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) "
            "VALUES (%s,%s,'video_frame',0,1000)",
            (f"item_consume_{material_id}", STREAM),
        )
    conn.execute(
        "INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,"
        "config_hash) VALUES (%s,%s,'0.1.0',%s,'no_inference',%s)",
        (UPSTREAM_RELEASE, UPSTREAM_MODEL, DIGEST, DIGEST),
    )


def append_materials(conn) -> list[str]:
    """用**真实写侧**追加素材：事实与 outbox 事件在同一个事务里落下。"""
    seed_references(conn)
    event_ids: list[str] = []
    for material_id, text in DOCUMENTS:
        unit = material.MaterialUnit(
            material_unit_id=material_id,
            stream_id=STREAM,
            time_range=common.TimeRange(start_ms=0, end_ms=1000),
            status="fast_ready",
            revision=1,
            observations=[upstream_observation(material_id, text)],
            tags=["acceptance"],
            pipeline_version="index-consume-acceptance-v1",
            created_at_unix_ms=int(time.time() * 1000),
            source_refs=[
                material.SourceReference(
                    asset_id=ASSET,
                    time_range=common.TimeRange(start_ms=0, end_ms=1000),
                    content_hash=DIGEST,
                )
            ],
        )
        if not api_materials.append_material(conn, unit, trace_id="index-consume-acceptance"):
            raise RuntimeError(f"the acceptance material {material_id} was not appended")
        event_ids.append(f"material:{material_id}:1")
    return event_ids


def add_phantom_event(conn, material_id: str) -> str:
    """刻意手写一条**坏事件**：指向一个不存在的素材（写侧同事务不会产生它）。

    这是唯一一处不是由真实写侧产生的 outbox 行，用途是验消费侧的 fail-stop：事实回查不到时
    必须不记账、不 ack，重投到上限后显式停止——而不是把"丢了一条向量"写成"已消费"。
    """
    event_id = f"material:{material_id}:1"
    envelope = EventEnvelope(
        event_id=event_id,
        event_type=consumer.SUPPORTED_EVENT_TYPE,
        stream_id=STREAM,
        trace_id="index-consume-acceptance-phantom",
        payload_ref=event_id,
        created_at_unix_ms=1_700_000_000_000,
        schema_version=1,
    )
    conn.execute(
        "INSERT INTO event_outbox(event_id,event_type,contract_bytes) VALUES (%s,%s,%s)",
        (event_id, envelope.event_type, envelope.SerializeToString(deterministic=True)),
    )
    return event_id


# ── 真实进程编排 ────────────────────────────────────────────────────────────


def run_cli(arguments: list[str], expected_exit: int, failures: list[str], *, label: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *arguments],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=CLI_TIMEOUT_S,
    )
    if completed.returncode != expected_exit:
        failures.append(
            f"{label}: {CLI_MODULE} exited {completed.returncode} (expected {expected_exit}): "
            f"stdout={completed.stdout.strip()[-300:]!r} stderr={completed.stderr.strip()[-300:]!r}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        failures.append(
            f"{label}: {CLI_MODULE} produced no JSON: stdout={completed.stdout[-200:]!r}"
        )
        return {}


def run_relay(
    arguments: list[str], expected_exit: int, failures: list[str], *, label: str
) -> list[dict]:
    """跑一次真实 relay 进程，把它打的 JSON 行解析回来。

    发布这一跳在本脚本里**钉死**为 `not_injected`（显式摘掉档位变量）：relay 的 `--batch`
    默认 200 超出所有档位，注入档位会让它按准入拒绝启动；relay 侧的 `admitted` 路径由
    `tests/contracts/test_event_backpressure_contract.py` 与容器内的
    `make event-pipeline-check` 各自负责。
    """
    completed = subprocess.run(
        [sys.executable, "-m", RELAY_MODULE, *arguments],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=CLI_TIMEOUT_S,
        env=residency_environ(None),
    )
    documents: list[dict] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            documents.append(json.loads(line))
        except json.JSONDecodeError:
            failures.append(f"{label}: relay printed a non-JSON line: {line[:200]!r}")
    if completed.returncode != expected_exit:
        failures.append(
            f"{label}: relay exited {completed.returncode} (expected {expected_exit}): "
            f"stdout={completed.stdout[-300:]!r} stderr={completed.stderr.strip()[-400:]!r}"
        )
    return documents


def published_document(documents: list[dict]) -> dict:
    for document in documents:
        if document.get("event") == "relay.status":
            return document
    return {}


def read_status(path: pathlib.Path) -> dict:
    """读消费状态文件：每次写入都是原子替换，所以读到的必然是一份完整文档。"""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8").strip() or "{}")
    except json.JSONDecodeError:
        return {}


def wait_for_status(
    path: pathlib.Path,
    predicate,
    *,
    process: Process,
    timeout: float = STATUS_TIMEOUT_S,
) -> dict:
    """等状态文件满足判定；进程先退出也算终点（退出原因由调用方读状态文件判定）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        document = read_status(path)
        if document and predicate(document):
            return document
        if process.process is not None and process.process.poll() is not None:
            time.sleep(0.2)
            return read_status(path)
        time.sleep(0.1)
    return {}


def consumed_at_least(target: int):
    """判定：这一份状态行已经消费完 `target` 条事件（累计口径，不是本轮计数）。"""

    def predicate(document: dict) -> bool:
        return (
            document.get("event") == "consume.status"
            and document.get("consumed_total", 0) >= target
        )

    return predicate


def surface_process(
    *,
    label: str,
    uri: str,
    database_url: str,
    nats_url: str,
    stream: str,
    prefix: str,
    durable: str,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
    port: int,
    out: pathlib.Path,
    status_out: pathlib.Path,
    max_deliver: int | None = None,
    nak_delay_s: float | None = None,
    connect_timeout_s: float | None = None,
    consume_idle_exit: int | None = None,
    tier: macos_resident.Tier | None = ACCEPTANCE_TIER,
) -> Process:
    command = [
        sys.executable,
        "-m",
        CLI_MODULE,
        "--uri",
        uri,
        "--database-url",
        database_url,
        "serve",
        "--vector-index-key",
        KEY,
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--model-dir",
        str(model_dir),
        "--model-file",
        model_file,
        "--provider",
        provider,
        "--auth-token",
        SURFACE_TOKEN,
        "--out",
        str(out),
        # 常驻消费挂在检索面同一个进程上（ADR-025 §1）。
        "--consume",
        "--nats-url",
        nats_url,
        "--stream",
        stream,
        "--subject-prefix",
        prefix,
        "--durable",
        durable,
        "--consume-status-out",
        str(status_out),
    ]
    if max_deliver is not None:
        command += ["--max-deliver", str(max_deliver)]
    if nak_delay_s is not None:
        command += ["--nak-delay-s", str(nak_delay_s)]
    if connect_timeout_s is not None:
        command += ["--connect-timeout-s", str(connect_timeout_s)]
    if consume_idle_exit is not None:
        command += ["--consume-idle-exit", str(consume_idle_exit)]
    # 消费侧带档位（默认 `large`），准入结论必须是 `admitted` 而不是 `not_injected`。
    process = Process(label, command, env=residency_environ(tier))
    process.start()
    return process


def wait_for_surface(
    process: Process, out: pathlib.Path, status_out: pathlib.Path, label: str
) -> dict:
    """等"真的接上了"：消费侧一条状态行落地 + 检索面就绪行写出。"""
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        document = read_status(status_out)
        if document.get("event") in ("consume.ready", "consume.status") and out.is_file():
            return json.loads(out.read_text(encoding="utf-8"))
        if process.process is not None and process.process.poll() is not None:
            raise AssertionError(
                f"{label}: the surface exited before the consumer connected "
                f"(code={process.process.returncode}): status={read_status(status_out)} "
                f"stdout={process.lines[-5:]} stderr={process.stderr[-5:]}"
            )
        time.sleep(0.1)
    raise AssertionError(f"{label}: the consumer never reported a status line")


def stop_process(process: Process, *, graceful: bool, failures: list[str], label: str) -> None:
    process.kill()
    try:
        code = process.finish(timeout=60)
    except subprocess.TimeoutExpired:
        failures.append(f"{label}: the process did not stop within 60s")
        return
    if graceful:
        check(code == 0, f"{label}: graceful shutdown exited {code}", failures)


def query_surface(port: int, *, query: str, principal: str, limit: int = 20) -> dict:
    """走真实 gRPC 查询检索面：这是"同进程消费出来的向量能被检索到"的证据形式。"""
    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        stub = index_pb2_grpc.IndexSearchServiceStub(channel)
        response = stub.SearchSemantic(
            index_pb2.SemanticSearchRequest(
                query=query, principal=principal, vector_index_key=KEY, limit=limit
            ),
            metadata=(("authorization", f"Bearer {SURFACE_TOKEN}"),),
            timeout=60,
        )
    return MessageToDict(response)


# ── JetStream 编排（每个场景独立 stream / 前缀） ────────────────────────────


def isolated_scope(label: str) -> tuple[str, str]:
    """独立 stream 与前缀：不碰开发用的 `sensoryplex-events`。

    前缀刻意**不在** `sensoryplex.events.>` 之下：JetStream 不允许两个 stream 的 subject
    互相重叠，挂在开发前缀下面会直接 `subjects overlap with an existing stream`。
    """
    token = uuid.uuid4().hex[:10]
    return f"sensoryplex-events-verify-{label}-{token}", f"sensoryplex.verify.{label}.{token}"


async def jetstream_admin(nats_url: str, action: str, stream: str, **kwargs):
    """建流/删流/查状态：只做编排，让 relay 与消费侧各自按契约去校验。"""
    client = await nats.connect(
        nats_url,
        name="sensoryplex-index-consume-acceptance",
        connect_timeout=5,
        max_reconnect_attempts=1,
    )
    try:
        js = client.jetstream()
        if action == "delete":
            try:
                await js.delete_stream(stream)
            except Exception:  # noqa: BLE001 - 不存在就是干净的
                pass
            return None
        if action == "drift_durable":
            await js.add_consumer(
                stream,
                config=jsapi.ConsumerConfig(
                    durable_name=kwargs["durable"],
                    filter_subject=kwargs["subject"],
                    ack_policy=jsapi.AckPolicy.EXPLICIT,
                    ack_wait=consumer.DEFAULT_ACK_WAIT_S,
                    # 只漂移重投上限：它决定坏事件会不会被无限重投，被改小同样是静默变质。
                    max_deliver=1,
                    max_ack_pending=consumer.DEFAULT_BATCH,
                ),
            )
            return None
        if action == "ack_pending":
            info = await js.consumer_info(stream, kwargs["durable"])
            return int(info.num_ack_pending or 0)
        if action == "messages":
            info = await js.stream_info(stream)
            return int(info.state.messages)
        if action == "exists":
            try:
                await js.stream_info(stream)
                return True
            except Exception:  # noqa: BLE001 - NotFoundError 就是不存在
                return False
        raise AssertionError(f"unknown jetstream action: {action}")
    finally:
        await client.close()


def jetstream(nats_url: str, action: str, stream: str, **kwargs):
    return asyncio.run(asyncio.wait_for(jetstream_admin(nats_url, action, stream, **kwargs), 30))


# ── 场景 ────────────────────────────────────────────────────────────────────


def scenario_event_driven(
    *,
    uri: str,
    database_url: str,
    nats_url: str,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
    workspace: pathlib.Path,
    failures: list[str],
) -> None:
    """场景 1：真实 outbox → relay → JetStream → 常驻消费 → 向量 → 同一进程的检索面。"""
    stream, prefix = isolated_scope("main")
    durable = "sensoryplex-index-sink-verify-main"
    jetstream(nats_url, "delete", stream)
    relay = [
        "--database-url",
        database_url,
        "--nats-url",
        nats_url,
        "--stream",
        stream,
        "--subject-prefix",
        prefix,
    ]
    with psycopg.connect(database_url) as conn:
        event_ids = append_materials(conn)
    print(f"[1] outbox: {len(event_ids)} events appended by the real write path")
    documents = run_relay([*relay, "--once"], 0, failures, label="relay")
    status = published_document(documents)
    check(
        status.get("published") == len(event_ids) and status.get("failed") == 0,
        f"relay did not publish every event: {status}",
        failures,
    )
    check(
        jetstream(nats_url, "messages", stream) == len(event_ids),
        "JetStream does not hold exactly the published events",
        failures,
    )
    check(
        status.get("inflight_state") == residency.NOT_INJECTED
        and status.get("inflight_declared") == RELAY_DEFAULT_BATCH
        and status.get("inflight_capacity") == 0,
        f"the publishing hop did not report the honest not_injected state: {status}",
        failures,
    )

    port = free_port()
    out = workspace / "consume-surface.json"
    status_out = workspace / "consume-status.json"
    process = surface_process(
        label="consume-surface",
        uri=uri,
        database_url=database_url,
        nats_url=nats_url,
        stream=stream,
        prefix=prefix,
        durable=durable,
        model_dir=model_dir,
        model_file=model_file,
        provider=provider,
        port=port,
        out=out,
        status_out=status_out,
    )
    try:
        ready = wait_for_surface(process, out, status_out, "consume-surface")
        consume_block = ready.get("consume") or {}
        check(
            consume_block.get("subject") == f"{prefix}.material.upserted"
            and consume_block.get("stream") == stream
            and consume_block.get("durable") == durable,
            f"the readiness line does not describe the consumer contract: {consume_block}",
            failures,
        )
        check(
            consume_block.get("idle_exit_cycles") == 0,
            "the resident consumer reported a bounded idle exit",
            failures,
        )
        check(
            consume_block.get("inflight_state") == residency.ADMITTED
            and consume_block.get("inflight_declared") == ACCEPTANCE_INFLIGHT
            and consume_block.get("inflight_capacity") == ACCEPTANCE_TIER.event_queue_capacity
            and consume_block.get("resident_tier") == ACCEPTANCE_TIER.name,
            f"the consuming hop did not report the injected tier admission: {consume_block}",
            failures,
        )
        release_id = str((ready.get("encoder") or {}).get("release_id", ""))
        check(bool(release_id), f"the surface reported no encoder identity: {ready}", failures)

        final = wait_for_status(
            status_out,
            consumed_at_least(len(event_ids)),
            process=process,
        )
        check(
            final.get("consumed_total") == len(event_ids) and final.get("failed_total") == 0,
            f"the consumer did not account for every event: {final}",
            failures,
        )
        check(
            final.get("embedded_total", 0) >= len(event_ids),
            f"fewer vectors were written than events consumed: {final}",
            failures,
        )
        check(
            set(final) == STATUS_FIELDS,
            f"the status line shape drifted: {sorted(final)}",
            failures,
        )
        check(
            final.get("inflight_state") == residency.ADMITTED
            and final.get("inflight_declared") == ACCEPTANCE_INFLIGHT
            and final.get("inflight_capacity") == ACCEPTANCE_TIER.event_queue_capacity
            and final.get("resident_tier") == ACCEPTANCE_TIER.name,
            f"the status line does not carry the admitted tier: {final}",
            failures,
        )

        # ── 事实库对账：ready 行 + 记账行 + 模型身份 ─────────────────────
        with psycopg.connect(database_url) as conn:
            rows = conn.execute(
                "SELECT embedding_id,state,vector_ref,indexed_at,model_release_id,dimension "
                "FROM embedding_record ORDER BY embedding_id"
            ).fetchall()
            check(
                len(rows) == len(event_ids),
                f"embedding_record holds {len(rows)} rows for {len(event_ids)} events",
                failures,
            )
            for embedding_id, state, vector_ref, indexed_at, record_release, dimension in rows:
                check(
                    state == "ready" and indexed_at is not None,
                    f"a consumed embedding is not ready: {(embedding_id, state)}",
                    failures,
                )
                check(
                    vector_ref == f"milvus://{KEY}/{embedding_id}",
                    f"vector_ref is not a logical reference: {vector_ref}",
                    failures,
                )
                check(
                    record_release == release_id and dimension == DIMENSION,
                    f"the vector identity is not the encoder's: {(record_release, release_id)}",
                    failures,
                )
            check(
                conn.execute("SELECT count(*) FROM consumed_event").fetchone()[0] == len(event_ids),
                "consumed_event does not hold exactly one row per event",
                failures,
            )
            identity = conn.execute(
                "SELECT backend,artifact_hash FROM model_release WHERE model_release_id=%s",
                (release_id,),
            ).fetchone()
            check(
                identity is not None and bool(identity[1]),
                "the consumer did not register the model identity it wrote",
                failures,
            )

        # ── 同一进程的检索面：事件驱动写入的向量必须**立刻**可检索 ────────
        response = query_surface(port, query=QUERY, principal=PRINCIPAL)
        check(
            not response.get("error"),
            f"a semantic query failed: {response.get('error')}",
            failures,
        )
        hits = response.get("hits", [])
        check(
            len(hits) == len(event_ids),
            f"the surface returned {len(hits)} hits for {len(event_ids)} indexed vectors",
            failures,
        )
        ranked = [hit.get("materialUnitId") for hit in hits]
        check(
            ranked[:1] == [EXPECTED_MATERIAL],
            f"the event-driven vector is not ranked first: {ranked}",
            failures,
        )
        check(
            response.get("queryModelReleaseId") == release_id
            and response.get("queryDimension") == DIMENSION,
            f"the query identity does not match the index: {response}",
            failures,
        )
        check(
            # protobuf 的 JSON 视图会省略 0：缺字段就是 0，不能把"没这个键"读成"丢了 None 条"。
            int(response.get("unindexedHits", 0)) == 0,
            f"an owner query dropped hits: {response.get('unindexedHits')}",
            failures,
        )
        for hit in hits:
            check(
                str(hit.get("vectorRef", "")).startswith("milvus://")
                and hit.get("modelReleaseId") == release_id
                and hit.get("dimension") == DIMENSION,
                f"a hit does not carry the index identity: {hit}",
                failures,
            )

        # 非 owner 命中必须被回查丢弃并单独计数（不是"没有命中"）。
        alien = query_surface(port, query=QUERY, principal=INTRUDER)
        check(
            alien.get("hits", []) == [],
            f"a non-owner query returned hits: {alien.get('hits')}",
            failures,
        )
        check(
            int(alien.get("unindexedHits", 0)) == len(event_ids),
            f"dropped non-owner hits are not counted: {alien.get('unindexedHits')}",
            failures,
        )

        # ── 单写进程：向量库的目录锁由消费进程持有（这就是"必须同进程"的理由） ──
        locked = run_cli(
            ["--uri", uri, "--database-url", database_url, "inspect", "--vector-index-key", KEY],
            1,
            failures,
            label="locked-inspect",
        )
        check(
            locked.get("error_code") == "vector_store_locked",
            f"a second opener did not report the lock: {locked}",
            failures,
        )

        # ── 不外泄 ────────────────────────────────────────────────────────
        emitted = json.dumps({"ready": ready, "status": final}, ensure_ascii=False)
        for needle in LEAK_NEEDLES:
            check(needle not in emitted, f"the consumer lines leaked {needle!r}", failures)
        for _, text in DOCUMENTS:
            check(text not in emitted, "the consumer lines leaked the material text", failures)

        # ── 优雅停止：SIGTERM 后按 0 退出，把未完成的投递交还队列 ──────────
        process.kill()
        check(process.finish(timeout=60) == 0, "the surface did not shut down gracefully", failures)
    finally:
        process.kill()
        try:
            process.finish(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - 收尾尽力而为
            pass

    # ── 锁已释放：另一个进程能重新打开同一个数据目录 ────────────────────
    released = run_cli(
        ["--uri", uri, "inspect", "--vector-index-key", KEY], 0, failures, label="released-inspect"
    )
    check(
        released.get("rows") == len(event_ids),
        f"the released data directory does not hold the consumed vectors: {released.get('rows')}",
        failures,
    )

    # ── 重放：换一个 durable（新的消费视角）重投同一批事件，向量不许变两份 ──
    replay_out = workspace / "replay-surface.json"
    replay_status = workspace / "replay-status.json"
    replay = surface_process(
        label="replay-surface",
        uri=uri,
        database_url=database_url,
        nats_url=nats_url,
        stream=stream,
        prefix=prefix,
        durable="sensoryplex-index-sink-verify-replay",
        model_dir=model_dir,
        model_file=model_file,
        provider=provider,
        port=free_port(),
        out=replay_out,
        status_out=replay_status,
    )
    try:
        wait_for_surface(replay, replay_out, replay_status, "replay-surface")
        replayed = wait_for_status(
            replay_status,
            consumed_at_least(len(event_ids)),
            process=replay,
        )
        check(
            replayed.get("consumed_total") == len(event_ids),
            f"the new consumer view did not consume the redelivered events: {replayed}",
            failures,
        )
        with psycopg.connect(database_url) as conn:
            # 记账按 consumer 作用域各自留痕（N 事件 × 2 视角），但向量**只有一份**。
            check(
                conn.execute("SELECT count(*) FROM consumed_event").fetchone()[0]
                == 2 * len(event_ids),
                "the redelivery did not keep one accounting row per consumer view",
                failures,
            )
            check(
                conn.execute("SELECT count(*) FROM embedding_record").fetchone()[0]
                == len(event_ids),
                "a redelivered event wrote a second embedding row",
                failures,
            )
    finally:
        stop_process(replay, graceful=False, failures=failures, label="replay-surface")
    released = run_cli(
        ["--uri", uri, "inspect", "--vector-index-key", KEY], 0, failures, label="replay-inspect"
    )
    check(
        released.get("rows") == len(event_ids),
        f"the redelivery changed the vector rows: {released.get('rows')}",
        failures,
    )
    jetstream(nats_url, "delete", stream)
    print("[1] event-driven write -> same-process retrieval -> replay dedupe passed")


def scenario_fail_stop(
    *,
    uri: str,
    database_url: str,
    nats_url: str,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
    workspace: pathlib.Path,
    failures: list[str],
) -> None:
    """场景 2：坏事件重投到上限后必须 fail-stop（不 ack、不记账、按原因退出）。"""
    stream, prefix = isolated_scope("failstop")
    durable = "sensoryplex-index-sink-verify-failstop"
    jetstream(nats_url, "delete", stream)
    with psycopg.connect(database_url) as conn:
        add_phantom_event(conn, "material_consume_phantom")
    documents = run_relay(
        [
            "--database-url",
            database_url,
            "--nats-url",
            nats_url,
            "--stream",
            stream,
            "--subject-prefix",
            prefix,
            "--once",
        ],
        0,
        failures,
        label="relay-failstop",
    )
    check(
        published_document(documents).get("published") == 1,
        f"the phantom event was not published: {published_document(documents)}",
        failures,
    )
    out = workspace / "failstop-surface.json"
    status_out = workspace / "failstop-status.json"
    process = surface_process(
        label="failstop-surface",
        uri=uri,
        database_url=database_url,
        nats_url=nats_url,
        stream=stream,
        prefix=prefix,
        durable=durable,
        model_dir=model_dir,
        model_file=model_file,
        provider=provider,
        port=free_port(),
        out=out,
        status_out=status_out,
        max_deliver=2,
        nak_delay_s=0.2,
    )
    try:
        wait_for_surface(process, out, status_out, "failstop-surface")
        deadline = time.monotonic() + STATUS_TIMEOUT_S
        code = None
        while time.monotonic() < deadline:
            if process.process is not None and process.process.poll() is not None:
                code = process.process.returncode
                break
            time.sleep(0.2)
        check(
            code == consumer.FATAL_EXIT_CODE,
            f"the consumer did not fail-stop: exit={code}",
            failures,
        )
        fatal = read_status(status_out)
        check(
            fatal.get("event") == "consume.fatal"
            and fatal.get("error_code") == "event_retry_exhausted"
            and fatal.get("error_detail") == "event_missing_facts"
            and fatal.get("exit_code") == consumer.FATAL_EXIT_CODE,
            f"the fatal line does not explain the stop: {fatal}",
            failures,
        )
        with psycopg.connect(database_url) as conn:
            check(
                conn.execute(
                    "SELECT count(*) FROM consumed_event WHERE consumer_name=%s", (durable,)
                ).fetchone()[0]
                == 0,
                "a refused event was recorded as consumed",
                failures,
            )
            check(
                conn.execute(
                    "SELECT count(*) FROM embedding_record WHERE material_unit_id=%s",
                    ("material_consume_phantom",),
                ).fetchone()[0]
                == 0,
                "a refused event still wrote an embedding row",
                failures,
            )
        check(
            jetstream(nats_url, "ack_pending", stream, durable=durable) >= 1,
            "the refused event was acked away instead of staying in the queue",
            failures,
        )
    finally:
        stop_process(process, graceful=False, failures=failures, label="failstop-surface")
        jetstream(nats_url, "delete", stream)
    print("[2] fail-stop on a refused event: exit code 3, no ack, no accounting")


def scenario_startup_failures(
    *,
    uri: str,
    database_url: str,
    nats_url: str,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
    workspace: pathlib.Path,
    failures: list[str],
) -> None:
    """场景 3：启动期契约失败必须显式失败（绝不用"看起来在跑"的半启动状态糊过去）。"""
    stream, prefix = isolated_scope("startup")
    jetstream(nats_url, "delete", stream)

    def expect(code: int, reason: str, *, label: str, **kwargs) -> None:
        out = workspace / f"{label}.json"
        status_out = workspace / f"{label}-status.json"
        process = surface_process(
            label=label,
            uri=uri,
            database_url=database_url,
            nats_url=kwargs.pop("nats_url", nats_url),
            stream=kwargs.pop("stream", stream),
            prefix=kwargs.pop("prefix", prefix),
            durable=kwargs.pop("durable", f"verify-{label}"),
            model_dir=model_dir,
            model_file=model_file,
            provider=provider,
            port=free_port(),
            out=out,
            status_out=status_out,
            **kwargs,
        )
        try:
            deadline = time.monotonic() + STATUS_TIMEOUT_S
            while time.monotonic() < deadline:
                if process.process is not None and process.process.poll() is not None:
                    break
                if read_status(status_out).get("command") == "consume":
                    break
                time.sleep(0.1)
            # 状态行是**先写、进程后退出**：看到失败原因后再等进程真的退出，否则读到的是 None。
            try:
                process.finish(timeout=60)
            except subprocess.TimeoutExpired:
                failures.append(f"{label}: the process did not exit after reporting the failure")
            exited = process.process.poll() if process.process is not None else None
            check(exited == code, f"{label}: exit={exited} (expected {code})", failures)
            document = read_status(status_out)
            reported = json.dumps(document)
            check(
                document.get("command") == "consume" and document.get("error_code") == reason,
                f"{label}: the startup failure was not reported as {reason}: {document}",
                failures,
            )
            check(
                "postgresql://" not in reported and "/Users/" not in reported,
                f"{label}: the startup failure line leaked the DSN or a host path",
                failures,
            )
        finally:
            stop_process(process, graceful=False, failures=failures, label=label)
        print(f"[3] startup refused explicitly: {label} -> {reason}")

    # stream 缺失：消费端**绝不**自动建流（那会把"发布端还没部署"伪装成"链路通了"）。
    expect(1, "event_stream_missing", label="stream-missing")
    check(
        jetstream(nats_url, "exists", stream) is False,
        "the consumer created the missing stream instead of refusing to start",
        failures,
    )

    # stream 存在但 durable 契约漂移：启动即失败，不悄悄改配置。
    run_relay(
        [
            "--database-url",
            database_url,
            "--nats-url",
            nats_url,
            "--stream",
            stream,
            "--subject-prefix",
            prefix,
            "--once",
        ],
        0,
        failures,
        label="relay-startup",
    )
    jetstream(
        nats_url,
        "drift_durable",
        stream,
        durable="verify-durable-drift",
        subject=f"{prefix}.material.upserted",
    )
    expect(
        1,
        "event_consumer_contract_mismatch",
        label="durable-drift",
        durable="verify-durable-drift",
        max_deliver=consumer.DEFAULT_MAX_DELIVER,
    )

    # NATS 不可达：启动连接必须有界失败，不进入"重连到天荒地老"的半启动状态。
    expect(
        1,
        "nats_unreachable",
        label="nats-unreachable",
        nats_url=UNREACHABLE_NATS,
        connect_timeout_s=1.0,
    )
    jetstream(nats_url, "delete", stream)


# ── 入口 ────────────────────────────────────────────────────────────────────


def verify(
    database_url: str,
    nats_url: str,
    workspace: pathlib.Path,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
    keep_workspace: bool,
) -> list[str]:
    failures: list[str] = []
    uri = str(workspace / "vector-consume.db")
    print(f"workspace: {workspace}")
    print(f"vector uri: {uri}  collection: {KEY}  dimension: {DIMENSION}")

    schema = "consume_" + uuid.uuid4().hex
    admin = psycopg.connect(database_url, autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(database_url, options=f"-c search_path={schema},public")
    try:
        migrate(isolated)
        scenario_event_driven(
            uri=uri,
            database_url=isolated,
            nats_url=nats_url,
            model_dir=model_dir,
            model_file=model_file,
            provider=provider,
            workspace=workspace,
            failures=failures,
        )
        scenario_fail_stop(
            uri=uri,
            database_url=isolated,
            nats_url=nats_url,
            model_dir=model_dir,
            model_file=model_file,
            provider=provider,
            workspace=workspace,
            failures=failures,
        )
        scenario_startup_failures(
            uri=uri,
            database_url=isolated,
            nats_url=nats_url,
            model_dir=model_dir,
            model_file=model_file,
            provider=provider,
            workspace=workspace,
            failures=failures,
        )
    finally:
        if not keep_workspace:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--nats-url", default="nats://127.0.0.1:24222")
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument("--model-file", default=DEFAULT_MODEL_FILE)
    parser.add_argument("--provider", default="cpu")
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()

    model_dir = pathlib.Path(arguments.model_dir).expanduser()
    if not model_dir.is_dir():
        raise SystemExit(f"model dir not found: {model_dir}")
    database_url, origin = derive_database_url(arguments.database_url)
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-index-consume-"))
    print(f"database: {describe_target(database_url)} (from {origin})")
    print(f"nats: {arguments.nats_url}")
    print(f"weights: {model_dir} ({arguments.provider})")
    started = time.monotonic()
    failures: list[str] = []
    try:
        failures = verify(
            database_url,
            arguments.nats_url,
            workspace,
            model_dir,
            arguments.model_file,
            arguments.provider,
            arguments.keep_workspace,
        )
    except Exception as error:  # noqa: BLE001 - 起不来就是验收失败，不跳过
        failures.append(f"{type(error).__name__}: {str(error)[:300]}")
    if failures:
        print("\nindex consume acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        "index consume acceptance: real outbox -> JetStream -> resident consume -> Milvus Lite -> "
        f"same-process retrieval passed in {round(time.monotonic() - started, 1)}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""index-worker 命令行入口；进程边界与其它 worker 一致（显式参数、显式失败）。

形态：
- `index` / `search` / `inspect`：显式调用短命进程；
- `serve`：常驻检索面（gRPC）+ 可选同进程常驻消费（针对 Milvus Lite 独占锁模式）；
- `consume`：独立常驻消费 Worker（适用于 Milvus Standalone / pgvector 等解耦共享存储模式）；
- `reconcile`：后台事实对账补偿与向量垃圾回收 Worker (Vector GC & Tombstone)。
"""

import argparse
import json
import os
import pathlib
import signal
import sys
import tempfile
import threading

import psycopg
from sensoryplex_relay.contract import DEFAULT_STREAM, DEFAULT_SUBJECT_PREFIX
from sensoryplex_relay.residency import (
    EventBackpressure,
    ResidencyError,
    read_event_backpressure,
)

from .consumer import (
    DEFAULT_ACK_WAIT_S,
    DEFAULT_BATCH,
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_DURABLE,
    DEFAULT_FETCH_TIMEOUT_S,
    DEFAULT_MAX_DELIVER,
    DEFAULT_NAK_DELAY_S,
    ConsumerError,
    ConsumerOptions,
    ConsumerRunner,
    json_line,
)
from .environ import BASE_ENVIRON
from .errors import IndexContractError, VectorStoreError
from .milvus_store import VectorIndex
from .query_encoder import QueryEncoderError, build_query_encoder
from .reconciliation import ReconciliationRunner, reconcile_cycle
from .service import INDEX_VERSION, start_server
from .worker import index_embedding, search_embeddings

SERVE_GRACE_SECONDS = 3


def _load_observations(path: pathlib.Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, dict) and "observations" in document:
        document = document["observations"]
    if not isinstance(document, list) or not document:
        raise SystemExit("input_observations_empty")
    return document


def _connect(database_url: str):
    if not database_url:
        raise SystemExit("database_url_required")
    return psycopg.connect(database_url)


def _emit(document: dict, out: pathlib.Path | None) -> None:
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print(text, end="")


def _emit_run_failure(arguments, error: VectorStoreError | IndexContractError) -> int:
    _emit(
        {
            "command": arguments.command,
            "uri": arguments.uri,
            "error_code": error.code,
            "detail": error.detail,
            "indexed": [],
            "failed": [{"observation_id": None, "reason_code": error.code}],
            "results": [],
            "unindexed_hits": 0,
        },
        getattr(arguments, "out", None),
    )
    return 1


def run_index(arguments) -> int:
    observations = _load_observations(arguments.input)
    selected = observations[: arguments.max_inputs] if arguments.max_inputs else observations
    try:
        index = VectorIndex(
            arguments.uri, arguments.vector_index_key, engine=getattr(arguments, "engine", None)
        )
    except (VectorStoreError, IndexContractError) as error:
        return _emit_run_failure(arguments, error)
    outcomes = []
    failures = []
    with _connect(arguments.database_url) as conn:
        for entry in selected:
            payload = entry.get("payload")
            observation_id = entry.get("observationId") or entry.get("observation_id")
            try:
                outcome = index_embedding(
                    conn,
                    index,
                    payload=payload,
                    material_unit_id=arguments.material_unit_id,
                    material_revision=arguments.material_revision,
                    model_release_id=arguments.model_release_id,
                    stream_id=arguments.stream_id,
                    start_ms=arguments.start_ms,
                    end_ms=arguments.end_ms,
                    modality=arguments.modality,
                )
            except (VectorStoreError, IndexContractError) as error:
                failures.append(
                    {
                        "observation_id": observation_id,
                        "reason_code": error.code,
                        "detail": error.detail,
                    }
                )
                continue
            outcomes.append(
                {
                    "observation_id": observation_id,
                    "embedding_id": outcome.embedding_id,
                    "vector_ref": outcome.vector_ref,
                    "state": outcome.state,
                }
            )
        conn.commit()
    index.close()
    _emit(
        {
            "command": "index",
            "uri": arguments.uri,
            "vector_index_key": arguments.vector_index_key,
            "collection": index.collection,
            "material_unit_id": arguments.material_unit_id,
            "material_revision": arguments.material_revision,
            "indexed": outcomes,
            "failed": failures,
            "unindexed_hits": 0,
        },
        getattr(arguments, "out", None),
    )
    return 1 if failures and not outcomes else 0


def run_inspect(arguments) -> int:
    try:
        index = VectorIndex(
            arguments.uri, arguments.vector_index_key, engine=getattr(arguments, "engine", None)
        )
        index.ensure_collection()
        count = index.count()
    except (VectorStoreError, IndexContractError) as error:
        return _emit_run_failure(arguments, error)
    finally:
        try:
            index.close()
        except Exception:  # noqa: BLE001
            pass
    _emit(
        {
            "command": "inspect",
            "uri": arguments.uri,
            "vector_index_key": arguments.vector_index_key,
            "collection": index.collection,
            "dimension": index.dimension,
            "contract_version": index.contract_version,
            "rows": count,
            "is_shared": index.is_shared,
            "engine_name": index.engine_name,
        },
        getattr(arguments, "out", None),
    )
    return 0


def _parse_query_vector(raw: str) -> list[float]:
    parts = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not parts:
        raise SystemExit("query_vector_empty")
    try:
        return [float(part) for part in parts]
    except ValueError as error:
        raise SystemExit("query_vector_invalid_float") from error


def _search_once(index: VectorIndex, arguments, vector: list[float]):
    with _connect(arguments.database_url) as conn:
        outcome = search_embeddings(
            conn, index, vector=vector, limit=arguments.limit, principal=arguments.principal
        )
    return outcome


def run_search(arguments) -> int:
    vector = _parse_query_vector(arguments.query_vector)
    try:
        index = VectorIndex(
            arguments.uri, arguments.vector_index_key, engine=getattr(arguments, "engine", None)
        )
        outcome = _search_once(index, arguments, vector)
    except (VectorStoreError, IndexContractError) as error:
        return _emit_run_failure(arguments, error)
    finally:
        try:
            index.close()
        except Exception:  # noqa: BLE001
            pass
    _emit(
        {
            "command": "search",
            "uri": arguments.uri,
            "vector_index_key": arguments.vector_index_key,
            "collection": index.collection,
            "principal": arguments.principal,
            "limit": arguments.limit,
            "results": outcome.results,
            "unindexed_hits": outcome.unindexed_hits,
        },
        getattr(arguments, "out", None),
    )
    return 0


def consume_options_of(arguments) -> ConsumerOptions | None:
    """把命令行折成消费参数面； 未开时返回 None（这就是"只做检索面"）。"""
    if not getattr(arguments, "consume", False):
        return None
    options = ConsumerOptions(
        stream=arguments.stream,
        subject_prefix=arguments.subject_prefix,
        durable=arguments.durable,
        batch=arguments.consume_batch,
        ack_wait_s=arguments.ack_wait_s,
        fetch_timeout_s=arguments.fetch_timeout_s,
        max_deliver=arguments.max_deliver,
        nak_delay_s=arguments.nak_delay_s,
        connect_timeout_s=arguments.connect_timeout_s,
        idle_exit_cycles=arguments.consume_idle_exit,
    )
    options.validate()
    read_event_backpressure(options.batch, environ=BASE_ENVIRON)
    return options


_build_consume_options = consume_options_of


def consume_backpressure_of(options: ConsumerOptions | None) -> EventBackpressure | None:
    """消费开着时把准入结论取出来放进 ready 行；关着时是 None（没有"看起来在跑"的字段）。"""
    if options is None:
        return None
    return read_event_backpressure(options.batch, environ=BASE_ENVIRON)


def run_serve(arguments) -> int:
    """常驻节点：先建编码器（失败就不占向量库的锁），再开库、开库成功才对外服务。"""
    if not getattr(arguments, "database_url", None):
        raise SystemExit("database_url_required")
    auth_token = getattr(arguments, "auth_token", None) or BASE_ENVIRON.get(
        "SENSORYPLEX_INDEX_AUTH_TOKEN", ""
    )
    if not auth_token:
        raise SystemExit("index_auth_token_required")
    try:
        consume_options = consume_options_of(arguments)
        backpressure = consume_backpressure_of(consume_options)
    except ResidencyError as error:
        raise SystemExit(error.code) from error
    try:
        encoder = build_query_encoder(
            model_dir=arguments.model_dir,
            model_file=arguments.model_file,
            provider=arguments.provider,
            max_length=arguments.max_length,
        )
        index = VectorIndex(
            arguments.uri, arguments.vector_index_key, engine=getattr(arguments, "engine", None)
        )
        index.ensure_collection()
        server, pool = start_server(
            database_url=arguments.database_url,
            index=index,
            encoder=encoder,
            auth_token=auth_token,
            bind=arguments.bind,
            port=arguments.port,
            max_concurrency=arguments.max_concurrency,
        )
    except (VectorStoreError, IndexContractError, QueryEncoderError) as error:
        return _emit_run_failure(arguments, error)

    runner = None
    if consume_options is not None:
        runner = ConsumerRunner(
            database_url=arguments.database_url,
            nats_url=arguments.nats_url,
            options=consume_options,
            backpressure=backpressure,
            index=index,
            encoder=encoder,
            emit=StatusWriter(arguments.consume_status_out),
            on_fatal=lambda: server.stop(SERVE_GRACE_SECONDS),
        )
        runner.start()
        if not runner.wait_ready(timeout=consume_options.connect_timeout_s * 3):
            failure = ConsumerError(
                "consumer_start_timeout", f"{consume_options.connect_timeout_s * 3}s"
            )
        else:
            failure = runner.error
        if failure is not None:
            _emit(
                {
                    "command": "consume",
                    "stream": consume_options.stream,
                    "subject": consume_options.subject,
                    "durable": consume_options.durable,
                    "error_code": failure.code,
                    "error_detail": failure.detail,
                },
                arguments.consume_status_out,
            )
            runner.stop()
            server.stop(0)
            pool.close()
            index.close()
            return 1

    # 可选在 serve 进程启动后台巡检对账线程
    reconcile_runner = None
    if getattr(arguments, "reconcile", False):
        reconcile_runner = ReconciliationRunner(
            database_url=arguments.database_url,
            index=index,
            interval_s=getattr(arguments, "reconcile_interval_s", 300.0),
            batch=getattr(arguments, "reconcile_batch", 100),
            emit=StatusWriter(getattr(arguments, "reconcile_status_out", None)),
        )
        reconcile_runner.start()

    _emit(
        {
            "command": "serve",
            "uri": arguments.uri,
            "address": f"{arguments.bind}:{arguments.port}",
            "collection": index.collection,
            "vector_index_key": arguments.vector_index_key,
            "index_version": INDEX_VERSION,
            "max_concurrency": arguments.max_concurrency,
            "encoder": encoder.describe(),
            "is_shared": index.is_shared,
            "engine_name": index.engine_name,
            "consume": None
            if consume_options is None
            else {
                "stream": consume_options.stream,
                "subject": consume_options.subject,
                "durable": consume_options.durable,
                "batch": consume_options.batch,
                "max_deliver": consume_options.max_deliver,
                "idle_exit_cycles": consume_options.idle_exit_cycles,
                **backpressure.document(),
            },
        },
        getattr(arguments, "out", None),
    )

    def stop_everything(*_):
        server.stop(SERVE_GRACE_SECONDS)
        if runner is not None:
            runner.stop()
        if reconcile_runner is not None:
            reconcile_runner.stop()

    signal.signal(signal.SIGTERM, stop_everything)
    signal.signal(signal.SIGINT, stop_everything)
    server.wait_for_termination()
    if runner is not None:
        runner.stop()
    if reconcile_runner is not None:
        reconcile_runner.stop()
    pool.close()
    index.close()
    return runner.exit.code if runner is not None else 0


def run_consume(arguments) -> int:
    """独立常驻消费 Worker 模式（支持与检索面分离部署，无排他锁约束）。"""
    options = ConsumerOptions(
        stream=arguments.stream,
        subject_prefix=arguments.subject_prefix,
        durable=arguments.durable,
        batch=arguments.consume_batch,
        ack_wait_s=arguments.ack_wait_s,
        fetch_timeout_s=arguments.fetch_timeout_s,
        max_deliver=arguments.max_deliver,
        nak_delay_s=arguments.nak_delay_s,
        connect_timeout_s=arguments.connect_timeout_s,
        idle_exit_cycles=arguments.consume_idle_exit,
    )
    options.validate()
    backpressure = consume_backpressure_of(options)

    try:
        index = VectorIndex(
            arguments.uri, arguments.vector_index_key, engine=getattr(arguments, "engine", None)
        )
        index.ensure_collection()
    except (VectorStoreError, IndexContractError) as error:
        return _emit_run_failure(arguments, error)

    encoder = None
    if getattr(arguments, "model_dir", None):
        encoder = build_query_encoder(
            model_dir=arguments.model_dir,
            model_file=getattr(arguments, "model_file", "") or "onnx/model_quantized.onnx",
            provider=getattr(arguments, "provider", "cpu"),
            max_length=getattr(arguments, "max_length", 0),
        )

    runner = ConsumerRunner(
        database_url=arguments.database_url,
        nats_url=arguments.nats_url,
        options=options,
        backpressure=backpressure,
        index=index,
        encoder=encoder,
        emit=StatusWriter(arguments.consume_status_out),
    )
    runner.start()
    if not runner.wait_ready(timeout=options.connect_timeout_s * 3):
        runner.stop()
        index.close()
        return 1

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    runner.stop()
    index.close()
    return runner.exit.code


def run_reconcile(arguments) -> int:
    """向量垃圾回收与事实对账巡检 (Vector GC & Tombstone)。"""
    try:
        index = VectorIndex(
            arguments.uri, arguments.vector_index_key, engine=getattr(arguments, "engine", None)
        )
        index.ensure_collection()
    except (VectorStoreError, IndexContractError) as error:
        return _emit_run_failure(arguments, error)

    emit = StatusWriter(arguments.status_out)
    if arguments.once:
        with _connect(arguments.database_url) as conn:
            outcome = reconcile_cycle(conn, index, batch=arguments.batch, dry_run=arguments.dry_run)
            emit(outcome)
        index.close()
        return 0

    runner = ReconciliationRunner(
        database_url=arguments.database_url,
        index=index,
        interval_s=arguments.interval_s,
        batch=arguments.batch,
        dry_run=arguments.dry_run,
        emit=emit,
    )
    runner.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    runner.stop()
    index.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sensoryplex-index", description=__doc__)
    parser.add_argument(
        "--uri",
        default=BASE_ENVIRON.get("SENSORYPLEX_MILVUS_URI", ""),
        help=(
            "向量库 URI：本地文件路径 (Milvus Lite)、"
            "http(s):// (Milvus Standalone/Cluster) 或 postgresql:// (pgvector)"
        ),
    )
    parser.add_argument(
        "--engine",
        default=BASE_ENVIRON.get("SENSORYPLEX_VECTOR_ENGINE", ""),
        help="指定存储引擎 (milvus / pgvector，留空按 URI 自动推断)",
    )
    parser.add_argument(
        "--database-url",
        default=BASE_ENVIRON.get("SENSORYPLEX_INDEX_DATABASE_URL", ""),
        help="PostgreSQL DSN（embedding_record 事实库）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index = subparsers.add_parser("index", help="把模型插件的向量落库并确认")
    index.add_argument("--input", type=pathlib.Path, required=True)
    index.add_argument("--vector-index-key", required=True)
    index.add_argument("--material-unit-id", required=True)
    index.add_argument("--material-revision", type=int, required=True)
    index.add_argument("--model-release-id", required=True)
    index.add_argument("--stream-id", required=True)
    index.add_argument("--start-ms", type=int, required=True)
    index.add_argument("--end-ms", type=int, required=True)
    index.add_argument("--modality", default="text_embedding")
    index.add_argument("--max-inputs", type=int, default=0)
    index.add_argument("--out", type=pathlib.Path, default=None)
    index.set_defaults(handler=run_index)

    search = subparsers.add_parser("search", help="按查询向量检索，并回查 PostgreSQL 事实")
    search.add_argument("--vector-index-key", required=True)
    search.add_argument("--query-vector", required=True, help="逗号分隔的浮点数")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--principal", default="local-developer")
    search.add_argument("--out", type=pathlib.Path, default=None)
    search.set_defaults(handler=run_search)

    inspect = subparsers.add_parser("inspect", help="查看 collection 契约与行数")
    inspect.add_argument("--vector-index-key", required=True)
    inspect.add_argument("--out", type=pathlib.Path, default=None)
    inspect.set_defaults(handler=run_inspect)

    serve = subparsers.add_parser(
        "serve", help="常驻检索面（gRPC）：持有向量库，把查询文本编码成查询向量后检索"
    )
    serve.add_argument("--vector-index-key", required=True)
    serve.add_argument("--bind", default="127.0.0.1", help="默认只绑回环；跨容器接入要显式开")
    serve.add_argument("--port", type=int, default=50077)
    serve.add_argument("--model-dir", required=True, help="BGE 权重目录（不联网下载）")
    serve.add_argument("--model-file", default="")
    serve.add_argument("--provider", default="cpu")
    serve.add_argument("--max-length", type=int, default=0)
    serve.add_argument("--max-concurrency", type=int, default=2)
    serve.add_argument(
        "--auth-token",
        default="",
        help="共享令牌；也可用环境变量 SENSORYPLEX_INDEX_AUTH_TOKEN，缺失即拒绝启动",
    )
    serve.add_argument("--out", type=pathlib.Path, default=None)
    serve.add_argument(
        "--consume",
        action="store_true",
        help="把 JetStream → sink 的常驻消费挂在本进程上（ADR-025 单进程模式）",
    )
    serve.add_argument(
        "--nats-url",
        default=BASE_ENVIRON.get("SENSORYPLEX_NATS_URL", "nats://127.0.0.1:24222"),
        help="NATS 端点；容器内是 nats://nats:4222，主机是 127.0.0.1",
    )
    serve.add_argument("--stream", default=DEFAULT_STREAM, help="JetStream stream 名")
    serve.add_argument("--subject-prefix", default=DEFAULT_SUBJECT_PREFIX)
    serve.add_argument("--durable", default=DEFAULT_DURABLE, help="durable 名 = 去重作用域")
    serve.add_argument("--consume-batch", type=int, default=DEFAULT_BATCH)
    serve.add_argument("--ack-wait-s", type=float, default=DEFAULT_ACK_WAIT_S)
    serve.add_argument("--fetch-timeout-s", type=float, default=DEFAULT_FETCH_TIMEOUT_S)
    serve.add_argument("--max-deliver", type=int, default=DEFAULT_MAX_DELIVER)
    serve.add_argument("--nak-delay-s", type=float, default=DEFAULT_NAK_DELAY_S)
    serve.add_argument("--connect-timeout-s", type=float, default=DEFAULT_CONNECT_TIMEOUT_S)
    serve.add_argument(
        "--consume-idle-exit",
        type=int,
        default=0,
        help="连续 N 轮拉不到消息就退出（0 = 常驻）；用于有界排空",
    )
    serve.add_argument("--consume-status-out", type=pathlib.Path, default=None)
    serve.add_argument("--reconcile", action="store_true", help="在后台启动向量对账巡检线程")
    serve.add_argument("--reconcile-interval-s", type=float, default=300.0)
    serve.add_argument("--reconcile-batch", type=int, default=100)
    serve.add_argument("--reconcile-status-out", type=pathlib.Path, default=None)
    serve.set_defaults(handler=run_serve)

    consume = subparsers.add_parser(
        "consume", help="独立常驻消费 Worker（支持读写分离与水平扩展架构）"
    )
    consume.add_argument("--vector-index-key", required=True)
    consume.add_argument(
        "--nats-url",
        default=BASE_ENVIRON.get("SENSORYPLEX_NATS_URL", "nats://127.0.0.1:24222"),
    )
    consume.add_argument("--stream", default=DEFAULT_STREAM)
    consume.add_argument("--subject-prefix", default=DEFAULT_SUBJECT_PREFIX)
    consume.add_argument("--durable", default=DEFAULT_DURABLE)
    consume.add_argument("--consume-batch", type=int, default=DEFAULT_BATCH)
    consume.add_argument("--ack-wait-s", type=float, default=DEFAULT_ACK_WAIT_S)
    consume.add_argument("--fetch-timeout-s", type=float, default=DEFAULT_FETCH_TIMEOUT_S)
    consume.add_argument("--max-deliver", type=int, default=DEFAULT_MAX_DELIVER)
    consume.add_argument("--nak-delay-s", type=float, default=DEFAULT_NAK_DELAY_S)
    consume.add_argument("--connect-timeout-s", type=float, default=DEFAULT_CONNECT_TIMEOUT_S)
    consume.add_argument("--consume-idle-exit", type=int, default=0)
    consume.add_argument("--consume-status-out", type=pathlib.Path, default=None)
    consume.add_argument("--model-dir", default=None)
    consume.add_argument("--model-file", default="")
    consume.add_argument("--provider", default="cpu")
    consume.add_argument("--max-length", type=int, default=0)
    consume.set_defaults(handler=run_consume)

    reconcile = subparsers.add_parser(
        "reconcile", help="向量垃圾回收与事实对账巡检 (Vector GC & Tombstone)"
    )
    reconcile.add_argument("--vector-index-key", required=True)
    reconcile.add_argument("--interval-s", type=float, default=60.0)
    reconcile.add_argument("--batch", type=int, default=100)
    reconcile.add_argument("--once", action="store_true", help="执行单轮巡检后退出")
    reconcile.add_argument(
        "--dry-run", action="store_true", help="只探测候选，不执行物理删除与标记"
    )
    reconcile.add_argument("--status-out", type=pathlib.Path, default=None)
    reconcile.set_defaults(handler=run_reconcile)

    return parser


class StatusWriter:
    """消费与对账状态行：同时进 stdout 与（可选的）文件；原子替换。"""

    def __init__(self, out: pathlib.Path | None):
        self._out = out
        self._lock = threading.Lock()

    def __call__(self, document: dict) -> None:
        line = json_line(document)
        with self._lock:
            print(line, flush=True)
            if self._out is None:
                return
            self._out.parent.mkdir(parents=True, exist_ok=True)
            handle, path = tempfile.mkstemp(dir=str(self._out.parent), prefix=".index-status-")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(line + "\n")
            os.replace(path, self._out)


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.uri:
        raise SystemExit("milvus_uri_required")
    try:
        return arguments.handler(arguments)
    except ConsumerError as error:
        raise SystemExit(error.code) from error


if __name__ == "__main__":
    sys.exit(main())

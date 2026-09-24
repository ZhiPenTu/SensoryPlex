"""index-worker 命令行入口；进程边界与其它 worker 一致（显式参数、显式失败）。

两个形态：

- `index` / `search` / `inspect`：**显式调用**，各自一个短命进程（落库、按向量检索、看契约）；
- `serve`：**常驻节点**（ADR-023 检索面 + ADR-025 常驻消费）。Milvus Lite 的数据目录是
  进程独占的，因此"持有索引的进程"与"回答语义检索的进程"必须是同一个；`--consume` 因此
  挂在同一个进程上——事件驱动写入的向量必须对同一个进程里的检索面立刻可见。加不加
  `--consume` 是两件事：不加就只是检索面，加了才是"发布端 → 消费 → 索引 → 检索"整条链路。
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
from .errors import IndexContractError, VectorStoreError
from .milvus_store import VectorIndex
from .query_encoder import QueryEncoderError, build_query_encoder
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
    """整轮失败（向量库不可达/被锁、契约不符）：给一个稳定的顶层原因码，不吐 traceback。

    这种失败不属于"某一条落库失败"——连不上库时一条都没落——所以单独一个 `error_code`，
    而不是伪造一串 per-observation 失败。
    """
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
        arguments.out,
    )
    return 1


def run_index(arguments) -> int:
    observations = _load_observations(arguments.input)
    selected = observations[: arguments.max_inputs] if arguments.max_inputs else observations
    try:
        index = VectorIndex(arguments.uri, arguments.vector_index_key)
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
                    "dimension": outcome.dimension,
                    "state": outcome.state,
                    "confirmed": outcome.confirmed,
                }
            )
    index.close()
    _emit(
        {
            "command": "index",
            "uri": arguments.uri,
            "collection": index.collection,
            "vector_index_key": arguments.vector_index_key,
            "indexed": outcomes,
            "failed": failures,
        },
        arguments.out,
    )
    return 1 if failures else 0


def run_search(arguments) -> int:
    vector = [float(value) for value in arguments.query_vector.split(",") if value.strip()]
    try:
        index = VectorIndex(arguments.uri, arguments.vector_index_key)
        outcome = _search_once(index, arguments, vector)
    except (VectorStoreError, IndexContractError) as error:
        return _emit_run_failure(arguments, error)
    count = index.count()
    index.close()
    _emit(
        {
            "command": "search",
            "uri": arguments.uri,
            "collection": index.collection,
            "vector_index_key": arguments.vector_index_key,
            "collection_rows": count,
            "unindexed_hits": outcome.unindexed_hits,
            "results": outcome.results,
        },
        arguments.out,
    )
    return 0


def _search_once(index: VectorIndex, arguments, vector: list[float]):
    with _connect(arguments.database_url) as conn:
        outcome = search_embeddings(
            conn, index, vector=vector, limit=arguments.limit, principal=arguments.principal
        )
    return outcome


def run_inspect(arguments) -> int:
    try:
        index = VectorIndex(arguments.uri, arguments.vector_index_key)
        index.ensure_collection()
        rows = index.count()
    except (VectorStoreError, IndexContractError) as error:
        return _emit_run_failure(arguments, error)
    _emit(
        {
            "command": "inspect",
            "uri": arguments.uri,
            "collection": index.collection,
            "vector_index_key": arguments.vector_index_key,
            "dimension": index.dimension,
            "contract_version": index.contract_version,
            "rows": rows,
        },
        arguments.out,
    )
    index.close()
    return 0


def consume_options_of(arguments) -> ConsumerOptions | None:
    """把命令行折成消费参数面；`--consume` 未开时返回 None（这就是"只做检索面"）。"""
    if not arguments.consume:
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
    # 参数越界在**占向量库的锁之前**失败：先崩在参数上，不要去抢一个别人正在用的目录。
    options.validate()
    return options


def run_serve(arguments) -> int:
    """常驻节点：先建编码器（失败就不占向量库的锁），再开库、开库成功才对外服务。"""
    if not arguments.database_url:
        raise SystemExit("database_url_required")
    auth_token = arguments.auth_token or os.getenv("SENSORYPLEX_INDEX_AUTH_TOKEN", "")
    if not auth_token:
        # 检索面一旦跨容器接入就必须显式开端口；没有令牌的服务不允许起来。
        raise SystemExit("index_auth_token_required")
    consume_options = consume_options_of(arguments)
    try:
        encoder = build_query_encoder(
            model_dir=arguments.model_dir,
            model_file=arguments.model_file,
            provider=arguments.provider,
            max_length=arguments.max_length,
        )
        index = VectorIndex(arguments.uri, arguments.vector_index_key)
        # 契约必须在**启动时**就对完：漂移的 collection 是配置事实，不是"稍后重试可能成功"的
        # 瞬时故障。留到第一次查询才失败，会把 `vector_collection_contract_mismatch` 伪装成
        # `vector_search_failed`（可重试），运维会被指向错误的排查方向。
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
            # 消费侧要么明确关着（null），要么把契约原样写出来：不写"看起来在跑"的 ready。
            "consume": None
            if consume_options is None
            else {
                "stream": consume_options.stream,
                "subject": consume_options.subject,
                "durable": consume_options.durable,
                "batch": consume_options.batch,
                "max_deliver": consume_options.max_deliver,
                "idle_exit_cycles": consume_options.idle_exit_cycles,
            },
        },
        arguments.out,
    )
    runner = None
    if consume_options is not None:
        runner = ConsumerRunner(
            database_url=arguments.database_url,
            nats_url=arguments.nats_url,
            options=consume_options,
            index=index,
            encoder=encoder,
            emit=StatusWriter(arguments.consume_status_out),
            # 消费侧以 `event_retry_exhausted` 结束时不允许静默降级：整个进程按原因退出。
            on_fatal=lambda: server.stop(SERVE_GRACE_SECONDS),
        )
        runner.start()
        # 有界等待"真的接上了"：NATS 不可达、流缺失、stream/durable 契约漂移都是**启动失败**，
        # 不能让进程带着一个没接上的消费侧对外服务。
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

    def stop_everything(*_):
        # SIGTERM 走优雅停止：进程退出即释放 Milvus Lite 的目录锁，锁是瞬时的容量约束。
        server.stop(SERVE_GRACE_SECONDS)
        if runner is not None:
            runner.stop()

    signal.signal(signal.SIGTERM, stop_everything)
    signal.signal(signal.SIGINT, stop_everything)
    server.wait_for_termination()
    if runner is not None:
        runner.stop()
    pool.close()
    index.close()
    # 消费侧 fatal = 整个进程 fatal：退出码把"为什么停了"带到进程边界之外。
    return runner.exit.code if runner is not None else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sensoryplex-index", description=__doc__)
    parser.add_argument(
        "--uri",
        default=os.getenv("SENSORYPLEX_MILVUS_URI", ""),
        help="Milvus URI：本地文件路径（Milvus Lite）或 http(s):// 服务端端点",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("SENSORYPLEX_INDEX_DATABASE_URL", ""),
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
    # 常驻消费（ADR-025）：默认关闭。开与不开是两个事实，绝不"看起来在跑"。
    serve.add_argument(
        "--consume",
        action="store_true",
        help="把 JetStream → sink 的常驻消费挂在本进程上（ADR-025）",
    )
    serve.add_argument(
        "--nats-url",
        default=os.getenv("SENSORYPLEX_NATS_URL", "nats://127.0.0.1:24222"),
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
    serve.set_defaults(handler=run_serve)
    return parser


class StatusWriter:
    """消费状态行：同时进 stdout 与（可选的）文件；文件用临时文件 + rename 原子替换。

    消费循环在自己的线程里，主线程也在写 stdout，所以这里加锁并逐行写完——交叉写出的
    JSON 行既不是状态行，也不是任何可解析的东西。
    """

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
            handle, path = tempfile.mkstemp(dir=str(self._out.parent), prefix=".consume-status-")
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
        # 参数越界在占向量库锁之前就失败：给原因码，不给 traceback。
        raise SystemExit(error.code) from error


if __name__ == "__main__":
    sys.exit(main())

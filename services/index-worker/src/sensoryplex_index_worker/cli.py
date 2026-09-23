"""index-worker 命令行入口；进程边界与其它 worker 一致（显式参数、显式失败）。

本切片只提供**显式调用**：常驻消费（NATS 事件 / outbox 轮询）尚未接线，因此这里不做
"看起来在跑"的服务，也不谎报 ready。
"""

import argparse
import json
import os
import pathlib
import sys

import psycopg

from .errors import IndexContractError, VectorStoreError
from .milvus_store import VectorIndex
from .worker import index_embedding, search_embeddings


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
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.uri:
        raise SystemExit("milvus_uri_required")
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())

"""M8 向量落库与检索闭环验收（ADR-020）：真实向量 → Milvus → 回查 PostgreSQL 事实。

这条链路接在 `tools/verify_embed.py` 后面：上游是**真实 OCR 文本经真实 BGE 权重**编码出来的
向量（`make embed-check` 的 `ai-worker.json`），本脚本不自己造向量，也不自己造素材事实。

角色：

1. 本脚本（编排 + 对账，在**主机**执行：Milvus Lite 是本地文件，PostgreSQL 走 .env 推导）；
2. `python -m sensoryplex_index_worker.cli`（独立进程，index-worker：落库 / 检索）；
3. 真实 PostgreSQL（本次新建隔离 schema，跑真实迁移）；
4. Milvus（本机用 Milvus Lite 的文件形态；服务端形态同一客户端、同一 collection 契约）。

判定标准（全部来自真实执行）：

- 维度是身份的一部分：payload 声明维度、`vector_index_key` 后缀维度、向量实际长度三者必须一致，
  不一致要么被拒绝（`vector_dimension_mismatch`）要么根本进不来；
- 先写向量再置 ready：`ready` 的行一定带 `vector_ref` 与 `indexed_at`，失败的行一定带原因码；
- `vector_ref` 是逻辑引用（`milvus://<collection>/<embedding_id>`）：不含主机路径与端口；
- 跨进程持久：换一个进程重新打开同一个 Milvus，行数不变、能检索到同样的记录；
- 检索必须回查事实：命中后按 principal 做 owner 过滤，非 owner 命中被丢弃并计入 `unindexed_hits`；
  被标成 `failed` 的记录即使还在向量库里也不得返回；
- 幂等：同一份输入重跑得到同一批 `embedding_id`，向量行数不增；
- 失败不静默：维度不符、向量库不可达、collection 契约不符都必须显式失败且留下 failed 记录；
- 单写进程：Milvus Lite 对数据目录加进程级 flock，目录被别人持有时必须报 `vector_store_locked`
  而不是静默重试或换路径（edge 形态的容量约束）；
- 不外泄：Milvus 里只有摘要与引用，没有媒体名/路径，也没有被编码的原文。
"""

import argparse
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

import psycopg  # noqa: E402
from edge_material_sdk.generated.common.v1.common_pb2 import TimeRange  # noqa: E402
from edge_material_sdk.generated.material.v1.material_pb2 import (  # noqa: E402
    MaterialUnit,
    Observation,
    SourceReference,
)
from google.protobuf.json_format import ParseDict  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from pymilvus import MilvusClient  # noqa: E402
from sensoryplex_api.infrastructure import materials as api_materials  # noqa: E402

from tools.migrate import migrate  # noqa: E402

PRINCIPAL = "index-acceptance-owner"
INTRUDER = "index-acceptance-intruder"
ASSET_ID = "asset_index_acceptance"
DIGEST = "sha256:" + "c" * 64
CLI_MODULE = "sensoryplex_index_worker.cli"
CONTAINER_HOST = "postgres"
HOST_PORT_KEY = "POSTGRES_PORT"


def derive_database_url(explicit: str) -> tuple[str, str]:
    """主机侧 DSN：显式传入优先；否则从仓库 `.env` 推导（容器主机名换成 127.0.0.1）。

    这是**本机验收**的固定映射，容器内运行时请显式传 `--database-url`。
    """
    if explicit:
        return explicit, "argument"
    env_file = ROOT / ".env"
    values = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    base = os.getenv("SENSORYPLEX_TEST_DATABASE_URL") or values.get(
        "SENSORYPLEX_TEST_DATABASE_URL", ""
    )
    if not base:
        raise SystemExit("database_url_required")
    parts = psycopg.conninfo.conninfo_to_dict(base)
    port = os.getenv(HOST_PORT_KEY) or values.get(HOST_PORT_KEY, "5432")
    parts["host"] = "127.0.0.1"
    parts["port"] = port
    return psycopg.conninfo.make_conninfo(**parts), "env-file"


def describe_target(database_url: str) -> str:
    parts = psycopg.conninfo.conninfo_to_dict(database_url)
    return f"{parts.get('host')}:{parts.get('port')}/{parts.get('dbname')}"


def load_entries(path: pathlib.Path, max_inputs: int) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, dict) and "observations" in document:
        document = document["observations"]
    if not isinstance(document, list) or not document:
        raise SystemExit("embedding_input_empty")
    entries = [entry for entry in document if isinstance(entry.get("payload"), dict)]
    if not entries:
        raise SystemExit("embedding_input_has_no_payload")
    return entries[:max_inputs] if max_inputs else entries


def build_material(entries: list[dict], material_unit_id: str) -> MaterialUnit:
    """用**真实的 BGE 观测**组装一条真实素材；这里只补素材层事实，不改观测内容。"""
    observations = []
    for entry in entries:
        observation = Observation()
        ParseDict(entry, observation)
        observations.append(observation)
    start_ms = min(observation.time_range.start_ms for observation in observations)
    end_ms = max(observation.time_range.end_ms for observation in observations)
    return MaterialUnit(
        material_unit_id=material_unit_id,
        stream_id=observations[0].stream_id,
        time_range=TimeRange(start_ms=start_ms, end_ms=end_ms),
        status="fast_ready",
        revision=1,
        observations=observations,
        tags=["acceptance"],
        pipeline_version="index-acceptance-v1",
        source_refs=[
            SourceReference(
                asset_id=ASSET_ID,
                time_range=TimeRange(start_ms=start_ms, end_ms=end_ms),
                content_hash=DIGEST,
            )
        ],
        created_at_unix_ms=observations[0].created_at_unix_ms,
    )


def seed_references(conn, material: MaterialUnit, *, owner: str, duration_ms: int) -> None:
    observation = material.observations[0]
    provenance = observation.provenance
    conn.execute(
        "INSERT INTO media_source(source_id,type,uri_redacted,owner) "
        "VALUES (%s,'file','[redacted]',%s)",
        (observation.source_id, owner),
    )
    conn.execute(
        "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
        "VALUES (%s,%s,now(),'stopped')",
        (observation.stream_id, observation.source_id),
    )
    conn.execute(
        "INSERT INTO media_asset VALUES (%s,%s,%s,%s,%s,%s)",
        (
            ASSET_ID,
            observation.stream_id,
            "private://not-exposed",
            DIGEST,
            "acceptance",
            duration_ms,
        ),
    )
    conn.execute(
        "INSERT INTO model_release("
        "model_release_id,name,version,artifact_hash,backend,config_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            provenance.model_release_id,
            provenance.model_id,
            provenance.model_version,
            provenance.model_artifact_digest,
            provenance.execution_backend,
            provenance.config_hash,
        ),
    )
    # 每条真实观测都指向它自己的上游 timeline item；这里按观测逐条登记，
    # 区间统一取素材区间（素材区间本就包含所有观测区间）。
    for observation in material.observations:
        conn.execute(
            "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (item_id) DO NOTHING",
            (
                observation.source_item_id,
                observation.stream_id,
                "video_frame",
                material.time_range.start_ms,
                material.time_range.end_ms,
            ),
        )


def check(condition, message: str, failures: list[str]) -> bool:
    if not condition:
        failures.append(message)
    return bool(condition)


def run_cli(arguments: list[str], expected_exit: int, failures: list[str]) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *arguments],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if completed.returncode != expected_exit:
        failures.append(
            f"index-worker exited {completed.returncode} (expected {expected_exit}): "
            f"{completed.stderr.strip()[-400:]}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        # 退出码对但 stdout 不是 JSON 时，真正的原因通常只在 stderr 里（未捕获异常）。
        failures.append(
            f"index-worker produced no JSON: stdout={completed.stdout[-200:]!r} "
            f"stderr={completed.stderr.strip()[-400:]!r}"
        )
        return {}


def index_arguments(
    uri: str,
    database_url: str,
    index_key: str,
    material: MaterialUnit,
    source: pathlib.Path,
    out: pathlib.Path,
) -> list[str]:
    return [
        "--uri",
        uri,
        "--database-url",
        database_url,
        "index",
        "--input",
        str(source),
        "--vector-index-key",
        index_key,
        "--material-unit-id",
        material.material_unit_id,
        "--material-revision",
        str(material.revision),
        "--model-release-id",
        material.observations[0].provenance.model_release_id,
        "--stream-id",
        material.stream_id,
        "--start-ms",
        str(material.time_range.start_ms),
        "--end-ms",
        str(material.time_range.end_ms),
        "--out",
        str(out),
    ]


def query_vector(entries: list[dict]) -> list[float]:
    return [float(value) for value in entries[0]["payload"]["vector"]]


STAGE_DRIFTED_SCRIPT = """
import sys

from pymilvus import DataType, MilvusClient

uri, collection, dimension = sys.argv[1], sys.argv[2], int(sys.argv[3])
client = MilvusClient(uri=uri)
schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
schema.add_field("embedding_id", DataType.VARCHAR, max_length=64, is_primary=True)
schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dimension)
params = client.prepare_index_params()
params.add_index(field_name="vector", index_type="FLAT", metric_type="COSINE")
client.create_collection(collection_name=collection, schema=schema, index_params=params)
client.close()
"""

HOLD_STORE_SCRIPT = """
import pathlib
import sys
import time

from pymilvus import MilvusClient

uri, marker = sys.argv[1], sys.argv[2]
client = MilvusClient(uri=uri)
pathlib.Path(marker).write_text("holding", encoding="utf-8")
time.sleep(120)
client.close()
"""


def stage_drifted_collection(uri: str, collection: str, dimension: int) -> None:
    """故意建一个同名但字段不齐的 collection，用来验证"契约不符必须拒绝"。

    必须由**子进程**建：Milvus Lite 对本进程开过的数据目录持有 flock，父进程自己开过之后
    子进程就打不开了（`DataDirLockedError`）。而这里要验的正是"子进程打开漂移 collection"，
    所以父进程不能先碰这个目录。
    """
    completed = subprocess.run(
        [sys.executable, "-c", STAGE_DRIFTED_SCRIPT, uri, collection, str(dimension)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"staging the drifted collection failed: {completed.stderr[-400:]}")


def stored_rows(uri: str, collection: str, fields: list[str]) -> list[dict]:
    client = MilvusClient(uri=uri)
    client.load_collection(collection)
    rows = client.query(collection_name=collection, filter="", output_fields=fields)
    client.close()
    return [dict(row) for row in rows]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    return dot / (left_norm * right_norm)


def verify(  # noqa: PLR0915 - 验收脚本按场景直排，拆函数会让"哪一步在验什么"更难读
    entries: list[dict],
    database_url: str,
    workspace: pathlib.Path,
    keep_workspace: bool,
) -> list[str]:
    failures: list[str] = []
    index_key = entries[0]["payload"]["vector_index_key"]
    dimension = int(entries[0]["payload"]["dimension"])
    collection = index_key.rsplit("_d", 1)[0] + f"_d{dimension}_v1"
    uri = str(workspace / "vector-edge.db")
    document = workspace / "embeddings.json"
    document.write_text(
        json.dumps({"observations": entries}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"vector uri: {uri}  collection: {collection}  dimension: {dimension}")

    schema = "index_" + uuid.uuid4().hex
    admin = psycopg.connect(database_url, autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(database_url, options=f"-c search_path={schema},public")
    try:
        migrate(isolated)
        material = build_material(entries, "material_index_acceptance")
        with psycopg.connect(isolated) as conn:
            seed_references(
                conn,
                material,
                owner=PRINCIPAL,
                duration_ms=material.time_range.end_ms + 1000,
            )
            check(
                api_materials.append_material(conn, material, trace_id="index-acceptance"),
                "real material facts were not appended",
                failures,
            )

        # ── 场景 1：真实落库 ──────────────────────────────────────────────
        first = run_cli(
            index_arguments(
                uri, isolated, index_key, material, document, workspace / "index-1.json"
            ),
            0,
            failures,
        )
        indexed = first.get("indexed", [])
        check(
            len(indexed) == len(entries),
            f"expected {len(entries)} indexed embeddings, got {len(indexed)}",
            failures,
        )
        for row in indexed:
            check(row.get("state") == "ready", f"embedding not ready: {row}", failures)
            check(row.get("confirmed") is True, f"embedding not confirmed: {row}", failures)
            check(
                row.get("vector_ref") == f"milvus://{collection}/{row.get('embedding_id')}",
                f"vector_ref is not the logical reference: {row.get('vector_ref')}",
                failures,
            )
        if not indexed:
            failures.append(f"nothing was indexed; index-worker reported: {first.get('failed')}")
            return failures
        embedding_ids = [row["embedding_id"] for row in indexed]

        with psycopg.connect(isolated) as conn:
            for embedding_id in embedding_ids:
                state = conn.execute(
                    "SELECT state FROM embedding_record WHERE embedding_id=%s",
                    (embedding_id,),
                ).fetchone()[0]
                check(state == "ready", f"{embedding_id} is not ready in PostgreSQL", failures)
                confirmation = conn.execute(
                    "SELECT vector_ref, error_code, indexed_at FROM embedding_record "
                    "WHERE embedding_id=%s",
                    (embedding_id,),
                ).fetchone()
                check(
                    confirmation[0] and confirmation[1] is None and confirmation[2] is not None,
                    f"{embedding_id} is ready without a confirmed write: {confirmation}",
                    failures,
                )

        # ── 场景 2：跨进程持久（新进程重新打开同一个 Milvus） ──────────────
        inspect = run_cli(
            ["--uri", uri, "inspect", "--vector-index-key", index_key],
            0,
            failures,
        )
        check(
            inspect.get("rows") == len(entries),
            f"a second process sees {inspect.get('rows')} rows, expected {len(entries)}",
            failures,
        )

        # ── 场景 3：检索 + 回查事实 ───────────────────────────────────────
        vector = query_vector(entries)
        searched = run_cli(
            [
                "--uri",
                uri,
                "--database-url",
                isolated,
                "search",
                "--vector-index-key",
                index_key,
                "--query-vector=" + ",".join(str(value) for value in vector),
                "--limit",
                str(len(entries)),
                "--principal",
                PRINCIPAL,
            ],
            0,
            failures,
        )
        results = searched.get("results", [])
        check(bool(results), "search returned nothing for an indexed vector", failures)
        if results:
            top = results[0]
            check(
                top["embedding_id"] == embedding_ids[0],
                f"self-retrieval returned {top['embedding_id']} instead of {embedding_ids[0]}",
                failures,
            )
            check(
                abs(top["distance"] - 1.0) < 1e-3,
                f"cosine self-distance is {top['distance']}, expected 1.0",
                failures,
            )
            check(
                top["material_unit_id"] == material.material_unit_id
                and top["stream_id"] == material.stream_id
                and top["start_ms"] == material.time_range.start_ms
                and top["end_ms"] == material.time_range.end_ms,
                f"retrieved provenance does not match the material: {top}",
                failures,
            )
            check(
                cosine_similarity(vector, vector) > 0.999 and top["dimension"] == dimension,
                "retrieved record does not carry the indexed dimension",
                failures,
            )

        # ── 场景 4：非 owner 命中必须被丢弃（Milvus 不是鉴权依据） ─────────
        intruder = run_cli(
            [
                "--uri",
                uri,
                "--database-url",
                isolated,
                "search",
                "--vector-index-key",
                index_key,
                "--query-vector=" + ",".join(str(value) for value in vector),
                "--limit",
                str(len(entries)),
                "--principal",
                INTRUDER,
            ],
            0,
            failures,
        )
        check(
            not intruder.get("results"),
            f"a non-owner principal received {len(intruder.get('results', []))} hits",
            failures,
        )
        check(
            intruder.get("unindexed_hits") == len(entries),
            f"non-owner hits were not accounted as dropped: {intruder.get('unindexed_hits')}",
            failures,
        )

        # ── 场景 5：non-ready 记录即使还在向量库里也不得返回 ───────────────
        dropped_id = embedding_ids[0]
        with psycopg.connect(isolated) as conn:
            conn.execute(
                "UPDATE embedding_record SET state='failed', "
                "error_code='acceptance_forced_failure' WHERE embedding_id=%s",
                (dropped_id,),
            )
        after_failure = run_cli(
            [
                "--uri",
                uri,
                "--database-url",
                isolated,
                "search",
                "--vector-index-key",
                index_key,
                "--query-vector=" + ",".join(str(value) for value in vector),
                "--limit",
                str(len(entries)),
                "--principal",
                PRINCIPAL,
            ],
            0,
            failures,
        )
        check(
            dropped_id not in [row["embedding_id"] for row in after_failure.get("results", [])],
            "a failed embedding_record was still returned as a search hit",
            failures,
        )
        check(
            after_failure.get("unindexed_hits") == 1,
            f"the dropped hit was not accounted: {after_failure.get('unindexed_hits')}",
            failures,
        )
        with psycopg.connect(isolated) as conn:
            conn.execute(
                "UPDATE embedding_record SET state='ready', error_code=NULL, indexed_at=now() "
                "WHERE embedding_id=%s",
                (dropped_id,),
            )

        # ── 场景 6：幂等重跑 ─────────────────────────────────────────────
        second = run_cli(
            index_arguments(
                uri, isolated, index_key, material, document, workspace / "index-2.json"
            ),
            0,
            failures,
        )
        check(
            [row["embedding_id"] for row in second.get("indexed", [])] == embedding_ids,
            "re-running the same input produced different embedding ids",
            failures,
        )
        check(
            second.get("collection") == collection,
            f"re-run wrote to a different collection: {second.get('collection')}",
            failures,
        )
        recheck = run_cli(["--uri", uri, "inspect", "--vector-index-key", index_key], 0, failures)
        check(
            recheck.get("rows") == len(entries),
            f"idempotent re-run changed the row count: {recheck.get('rows')}",
            failures,
        )

        # ── 场景 7：维度不符必须显式失败并留下 failed 记录 ─────────────────
        tampered = json.loads(document.read_text(encoding="utf-8"))
        tampered["observations"][0]["payload"]["dimension"] = dimension + 1
        tampered_path = workspace / "embeddings-tampered.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        bad_dimension = run_cli(
            index_arguments(
                uri, isolated, index_key, material, tampered_path, workspace / "index-3.json"
            ),
            1,
            failures,
        )
        check(
            [row.get("reason_code") for row in bad_dimension.get("failed", [])]
            == ["vector_dimension_mismatch"],
            f"tampered dimension was not rejected: {bad_dimension.get('failed')}",
            failures,
        )
        with psycopg.connect(isolated) as conn:
            failed_rows = conn.execute(
                "SELECT count(*) FROM embedding_record "
                "WHERE state='failed' AND error_code IS NOT NULL"
            ).fetchone()[0]
        check(failed_rows >= 1, "a rejected embedding left no failed record behind", failures)
        unchanged = run_cli(["--uri", uri, "inspect", "--vector-index-key", index_key], 0, failures)
        check(
            unchanged.get("rows") == len(entries),
            f"a rejected embedding still landed in the vector store: {unchanged.get('rows')}",
            failures,
        )

        # ── 场景 8：向量库不可达必须显式失败 ─────────────────────────────
        unreachable = run_cli(
            index_arguments(
                "/dev/null/milvus-edge.db",
                isolated,
                index_key,
                material,
                document,
                workspace / "index-4.json",
            ),
            1,
            failures,
        )
        check(
            bool(unreachable.get("failed")),
            "an unreachable vector store did not fail the run",
            failures,
        )
        check(
            all(row["state"] != "ready" for row in unreachable.get("indexed", [])),
            "an unreachable vector store still reported ready embeddings",
            failures,
        )

        # ── 场景 9：collection 契约不符必须显式失败 ───────────────────────
        contract_uri = str(workspace / "vector-contract.db")
        stage_drifted_collection(contract_uri, collection, dimension)
        mismatched = run_cli(
            index_arguments(
                contract_uri, isolated, index_key, material, document, workspace / "index-5.json"
            ),
            1,
            failures,
        )
        drifted_codes = {row.get("reason_code") for row in mismatched.get("failed", [])}
        check(
            drifted_codes == {"vector_collection_contract_mismatch"},
            f"a drifted collection was not rejected: {mismatched.get('failed')}",
            failures,
        )
        check(
            not mismatched.get("indexed"),
            "a drifted collection still accepted writes",
            failures,
        )

        # ── 场景 10：不外泄（向量库里既没有原文，也没有本地路径） ─────────
        sample_text = str(entries[0]["payload"].get("text", ""))[:24]
        fields = [
            "embedding_id",
            "material_unit_id",
            "stream_id",
            "content_hash",
            "observation_id",
            "model_release_id",
            "modality",
        ]
        rows = stored_rows(uri, collection, fields)
        blob = json.dumps(rows, ensure_ascii=False)
        check(
            len(rows) == len(entries),
            "the vector store does not hold exactly the indexed rows",
            failures,
        )
        for label, needle in (
            ("encoded text", sample_text),
            ("host path", str(workspace)),
            ("database url", isolated),
        ):
            if not needle:
                continue
            check(needle not in blob, f"the vector store leaks {label}", failures)

        # ── 场景 11：数据目录被别的进程持有 → vector_store_locked（可重试，不静默） ──
        locked_uri = str(workspace / "vector-locked.db")
        marker = workspace / "holder-ready"
        with psycopg.connect(isolated) as conn:
            ready_before = conn.execute(
                "SELECT count(*) FROM embedding_record WHERE state='ready'"
            ).fetchone()[0]
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLD_STORE_SCRIPT, locked_uri, str(marker)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 60
            while not marker.exists() and time.monotonic() < deadline:
                if holder.poll() is not None:
                    break
                time.sleep(0.2)
            check(marker.exists(), "the lock holder never opened the store", failures)
            contended = run_cli(
                index_arguments(
                    locked_uri, isolated, index_key, material, document, workspace / "index-6.json"
                ),
                1,
                failures,
            )
            check(
                [row.get("reason_code") for row in contended.get("failed", [])]
                == ["vector_store_locked"],
                f"a locked data directory was not reported as locked: {contended.get('failed')}",
                failures,
            )
            check(
                not contended.get("indexed"),
                "a locked data directory still reported indexed rows",
                failures,
            )
        finally:
            holder.terminate()
            holder.wait(timeout=30)
        with psycopg.connect(isolated) as conn:
            ready_after = conn.execute(
                "SELECT count(*) FROM embedding_record WHERE state='ready'"
            ).fetchone()[0]
        check(
            ready_after == ready_before,
            f"a locked run still marked embeddings ready: {ready_before} -> {ready_after}",
            failures,
        )
        # 持有者退出后同一个目录必须能用：锁定是瞬时的容量约束，不是永久不可用。
        released = run_cli(
            ["--uri", locked_uri, "inspect", "--vector-index-key", index_key], 0, failures
        )
        check(
            released.get("rows") == 0,
            f"the released data directory is not usable again: {released.get('rows')}",
            failures,
        )
    finally:
        if not keep_workspace:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--embeddings",
        type=pathlib.Path,
        required=True,
        help="`tools/verify_embed.py --keep-workspace` 产出的 ai-worker.json（真实 BGE 观测）",
    )
    parser.add_argument("--database-url", default="")
    parser.add_argument("--max-inputs", type=int, default=4)
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()
    if not arguments.embeddings.is_file():
        raise SystemExit(f"embeddings not found: {arguments.embeddings}")

    database_url, origin = derive_database_url(arguments.database_url)
    entries = load_entries(arguments.embeddings, arguments.max_inputs)
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-index-"))
    print(f"workspace: {workspace}")
    print(f"database: {describe_target(database_url)} (from {origin})")
    print(f"embeddings: {len(entries)} from {arguments.embeddings.name}")
    started = time.monotonic()
    failures = verify(entries, database_url, workspace, arguments.keep_workspace)
    if failures:
        print("\nindex acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        f"\nindex acceptance: real BGE vectors -> Milvus ({len(entries)} rows) -> "
        f"PostgreSQL provenance passed in {round(time.monotonic() - started, 1)}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

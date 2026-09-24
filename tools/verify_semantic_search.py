"""网关语义检索端到端验收（ADR-023）：真实权重 → 真实 Milvus Lite → 常驻检索面 → API 水合。

这条链路接在 `tools/verify_embed.py` 后面，但**不要求**先有 `ai-worker.json`：本脚本自己把
一段真实文本送进真实的 BGE 插件进程（`python -m edge_material_plugin_embed_bge_onnx`），
由插件按自己的观测契约产出向量与观测，再用 `python -m sensoryplex_index_worker.cli` 落库。
向量与观测都不是本脚本造的：它只负责"给文本、起进程、对账"。

角色（全部是真实进程/真实服务，没有替身）：

1. 本脚本（编排 + 对账，在**主机**执行：Milvus Lite 与 HF 权重都只在 macOS 主机上）；
2. BGE 插件进程 + `tools/ai_worker.py`（把文本编码成真实向量与真实观测）；
3. `python -m sensoryplex_index_worker.cli index`（短命进程，落库到 Milvus + PostgreSQL）；
4. `python -m sensoryplex_index_worker.cli serve`（**常驻检索面**：持有向量库，只收查询文本）；
5. 真实 PostgreSQL（本次新建隔离 schema，跑真实迁移）；
6. 真实 API（`create_app` + `TestClient`：真实 ASGI 与真实错误处理器）。

判定标准（全部来自真实执行）：

- 检索面是**唯一**打开向量库的地方：API 自己不碰 Milvus，只转发查询并水合事实；
- 同源守卫：collection 里的模型身份必须唯一且等于查询编码器，混装/不一致都必须显式失败
  （`vector_index_model_release_mixed` / `query_model_release_mismatch`），
  不许给出"看起来能用"的距离；
- 检索必须回查事实：非 owner 命中被丢弃并计入 `unindexed_hits`，不能表现成"没有命中"；
- 状态码表示失败落在哪一环：没走到检索面（未配置 / 连不上 / 令牌不符）→ 503，
  检索面明确拒绝（契约/同源不符）→ 502；`retryable` 是与之独立的标记；
- 鉴权：令牌不符是配置问题（不可重试），未配置不等于"重试就好"；
- 单写进程：Milvus Lite 的目录锁是进程级的，第二个 `serve` 必须报 `vector_store_locked`；
- 不外泄：检索面响应里没有路径、没有 DSN、没有向量、没有被编码的原文。
"""

import argparse
import json
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
import psycopg  # noqa: E402
from edge_material_sdk.generated.common.v1 import common_pb2 as common  # noqa: E402
from edge_material_sdk.generated.index.v1 import index_pb2, index_pb2_grpc  # noqa: E402
from edge_material_sdk.generated.material.v1 import material_pb2 as material  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from google.protobuf.json_format import MessageToDict, MessageToJson, ParseDict  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sensoryplex_api.app import create_app  # noqa: E402
from sensoryplex_api.infrastructure import materials as api_materials  # noqa: E402
from sensoryplex_api.settings import Settings  # noqa: E402
from sensoryplex_index_worker import records  # noqa: E402

from tools.migrate import migrate  # noqa: E402
from tools.verify_embed import (  # noqa: E402
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_FILE,
    MAX_LENGTH,
    READY_TIMEOUT_S,
    RUN_TIMEOUT_S,
    SERVER_TTL_MS,
)
from tools.verify_index import (  # noqa: E402
    derive_database_url,
    describe_target,
    stage_drifted_collection,
)
from tools.verify_model import Process, check, free_port  # noqa: E402

PLUGIN_MODULE = "edge_material_plugin_embed_bge_onnx"
PLUGIN_DIR = ROOT / "plugins/python/processors/embed-bge-onnx"
CLI_MODULE = "sensoryplex_index_worker.cli"
WORKER = ROOT / "tools" / "ai_worker.py"
PRINCIPAL = "semantic-acceptance-owner"
INTRUDER = "semantic-acceptance-intruder"
API_TOKEN = "semantic-acceptance-api-token-not-a-deployment-secret"
SURFACE_TOKEN = "semantic-acceptance-surface-token-0123456789abcdef"
STREAM = "stream_semantic_acceptance"
SOURCE = "source_semantic_acceptance"
MATERIAL = "material_semantic_acceptance"
ASSET = "asset_semantic_acceptance"
DIGEST = "sha256:" + "e" * 64
# 索引侧的 collection 名由 API 配置给出（也是检索面必须逐字匹配的那一个）：检索面启动时
# 会用编码器实测出来的 key 与它比对，不等即拒绝启动——这里不再自己推一遍同一套命名规则。
KEY = Settings(database_url="postgresql://placeholder/none").index_vector_index_key
DIMENSION = int(KEY.rsplit("_d", 1)[1].split("_", 1)[0])
# 三段真实文本：都是"素材"这一域里会出现的句子，且彼此语义可分。
DOCUMENTS = (
    "季度销售数据复盘：华东区回款率提升 12%，库存周转天数下降三天",
    "设备巡检记录：三号机组轴承温度偏高，需要更换润滑油并复测振动",
    "安全培训通知：本周五下午两点在二楼会议室进行消防演练，全员参加",
)
# 语义查询与它应当命中的那段文本：断言"排第一的是这一条"，不是"能返回点什么"。
QUERY = "机组轴承温度异常"
EXPECTED_TOP = 1
CLI_TIMEOUT_S = 300.0
# 上游文本输入不是模型输出：置信度必须显式缺失并说明原因（缺失即未知，不许填 0/1）。
CONFIDENCE_UNAVAILABLE_REASON = "acceptance_text_input_has_no_model_confidence"
# 线上响应允许出现的字段（`index/v1/index.proto`）：多一个字段就意味着契约被悄悄改宽。
RESPONSE_FIELDS = frozenset(
    {
        "hits",
        "vectorIndexKey",
        "collection",
        "indexVersion",
        "unindexedHits",
        "queryModelReleaseId",
        "queryDimension",
        "error",
    }
)
HIT_FIELDS = frozenset(
    {
        "embeddingId",
        "materialUnitId",
        "materialRevision",
        "distance",
        "vectorRef",
        "observationId",
        "modelReleaseId",
        "dimension",
    }
)


# ── 真实编码：文本 → BGE 插件进程 → 真实观测 ────────────────────────────────


def upstream_observations() -> list:
    """上游 `ocr_blocks` 观测：本脚本只提供**文本**，文字怎么拼、怎么编码由插件说了算。

    provenance 如实写成"验收构造的文本输入"：它不是 OCR 结果、也不是模型输出，因此不带
    `confidence`，而是显式给出 `confidence_unavailable_reason`。索引侧的真实身份由 BGE
    插件自己产出的观测携带（`encode_documents` 的返回值），这里不冒充它。
    """
    created_at = int(time.time() * 1000)
    observations = []
    for index, text in enumerate(DOCUMENTS):
        observation = material.Observation(
            observation_id=f"obs_semantic_upstream_{index}",
            modality="ocr_blocks",
            stream_id=STREAM,
            source_id=SOURCE,
            source_item_id=f"item_semantic_{index}",
            time_range=common.TimeRange(start_ms=index * 1000, end_ms=index * 1000 + 1000),
            confidence_unavailable_reason=CONFIDENCE_UNAVAILABLE_REASON,
            content_hash=DIGEST,
            quality_state="final",
            timing_source="media_pts",
            created_at_unix_ms=created_at,
            provenance=material.Provenance(
                plugin="org.sensoryplex.verify-semantic-search",
                plugin_version="0.1.0",
                artifact_digest=DIGEST,
                model_release_id="acceptance:upstream-text-input@0",
                model_id="acceptance-upstream-text",
                model_version="0.1.0",
                config_hash=DIGEST,
                execution_backend="no_inference",
                model_artifact_digest=DIGEST,
            ),
        )
        observation.payload.update({"blocks": [{"text": text}]})
        observations.append(observation)
    return observations


def plugin_digest() -> str:
    import importlib

    module = importlib.import_module(f"{PLUGIN_MODULE}.artifact")
    return str(module.package_digest(PLUGIN_DIR))


def encode_documents(
    workspace: pathlib.Path,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
    failures: list[str],
) -> pathlib.Path:
    """真实 BGE 插件进程把文本编码成观测；这一步没有任何替身或预置向量。"""
    upstream = upstream_observations()
    upstream_path = workspace / "upstream-observations.json"
    upstream_path.write_text(
        json.dumps(
            {"observations": [json.loads(MessageToJson(item)) for item in upstream]},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    digest = plugin_digest()
    config_path = workspace / "plugin-config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": provider,
                "model_dir": str(model_dir),
                "model_file": model_file,
                "model_id": "bge-small-zh-v1.5",
                "max_length": MAX_LENGTH,
                "ttl_ms": SERVER_TTL_MS,
                "timeout_s": RUN_TIMEOUT_S,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    port = free_port()
    plugin = Process(
        "bge-plugin",
        [sys.executable, "-m", PLUGIN_MODULE, "--port", str(port), "--expect-digest", digest],
    )
    plugin.start()
    report_path = workspace / "ai-worker.json"
    try:
        started = plugin.wait_for("plugin ready", timeout=READY_TIMEOUT_S)
        check(
            started.get("artifact_digest") == digest,
            f"the plugin started with a different artifact digest: {started}",
            failures,
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(WORKER),
                "--plugin",
                f"127.0.0.1:{port}",
                "--input-observations",
                str(upstream_path),
                "--source-id",
                SOURCE,
                "--plugin-config",
                str(config_path),
                "--max-inputs",
                str(len(upstream)),
                "--report",
                str(report_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=CLI_TIMEOUT_S,
        )
        if completed.returncode != 0:
            failures.append(
                f"the embedding worker failed ({completed.returncode}): "
                f"{completed.stderr.strip()[-400:]}"
            )
            return report_path
    finally:
        plugin.kill()
        plugin.finish(timeout=30)
    observations = json.loads(report_path.read_text(encoding="utf-8")).get("observations", [])
    check(
        len(observations) == len(upstream),
        f"the worker produced {len(observations)} observations for {len(upstream)} inputs",
        failures,
    )
    for document in observations:
        payload = document.get("payload", {})
        vector = payload.get("vector")
        check(
            isinstance(vector, list) and len(vector) == DIMENSION,
            f"an embedding observation carries no {DIMENSION}-dimension vector",
            failures,
        )
        check(
            payload.get("vector_index_key") == KEY,
            f"the plugin derived a different collection: {payload.get('vector_index_key')}",
            failures,
        )
        check(
            payload.get("embedding_id") == document.get("observationId"),
            "the payload embedding id is not the observation id",
            failures,
        )
    return report_path


# ── 事实落库与检索面装配 ────────────────────────────────────────────────────


def observation_from_json(document: dict):
    return ParseDict(document, material.Observation())


def seed_facts(conn, observations: list, *, owner: str) -> None:
    """登记引用事实，再用真实观测追加一条素材：这里没有模拟的 ingestion 结果。"""
    conn.execute(
        "INSERT INTO media_source(source_id,type,uri_redacted,owner) "
        "VALUES (%s,'file','private://not-exposed',%s)",
        (SOURCE, owner),
    )
    conn.execute(
        "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
        "VALUES (%s,%s,now(),'stopped')",
        (STREAM, SOURCE),
    )
    conn.execute(
        "INSERT INTO media_asset(asset_id,stream_id,object_uri,sha256,codec,duration_ms) "
        "VALUES (%s,%s,'private://not-exposed',%s,'acceptance',%s)",
        (ASSET, STREAM, DIGEST, len(observations) * 1000),
    )
    for index, observation in enumerate(observations):
        conn.execute(
            "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) "
            "VALUES (%s,%s,'video_frame',%s,%s) ON CONFLICT (item_id) DO NOTHING",
            (observation.source_item_id, STREAM, index * 1000, index * 1000 + 1000),
        )
    provenance = observations[0].provenance
    conn.execute(
        "INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,"
        "config_hash) VALUES (%s,%s,%s,%s,%s,%s)",
        (
            provenance.model_release_id,
            provenance.model_id,
            provenance.model_version,
            provenance.model_artifact_digest,
            provenance.execution_backend,
            provenance.config_hash,
        ),
    )
    end_ms = len(observations) * 1000
    unit = material.MaterialUnit(
        material_unit_id=MATERIAL,
        stream_id=STREAM,
        time_range=common.TimeRange(start_ms=0, end_ms=end_ms),
        status="fast_ready",
        revision=1,
        observations=observations,
        tags=["acceptance"],
        pipeline_version="semantic-acceptance-v1",
        source_refs=[
            material.SourceReference(
                asset_id=ASSET,
                time_range=common.TimeRange(start_ms=0, end_ms=end_ms),
                content_hash=DIGEST,
            )
        ],
        created_at_unix_ms=observations[0].created_at_unix_ms,
    )
    if not api_materials.append_material(conn, unit, trace_id="semantic-acceptance"):
        raise RuntimeError("the acceptance material facts were not appended")


def run_cli(arguments: list[str], expected_exit: int, failures: list[str]) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *arguments],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=CLI_TIMEOUT_S,
    )
    if completed.returncode != expected_exit:
        failures.append(
            f"{CLI_MODULE} exited {completed.returncode} (expected {expected_exit}): "
            f"stdout={completed.stdout.strip()[-300:]!r} stderr={completed.stderr.strip()[-300:]!r}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        failures.append(
            f"{CLI_MODULE} produced no JSON: stdout={completed.stdout[-200:]!r} "
            f"stderr={completed.stderr.strip()[-400:]!r}"
        )
        return {}


def index_arguments(
    uri: str, database_url: str, source: pathlib.Path, material_id: str
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
        KEY,
        "--material-unit-id",
        material_id,
        "--material-revision",
        "1",
        "--model-release-id",
        _model_release_id(source),
        "--stream-id",
        STREAM,
        "--start-ms",
        "0",
        "--end-ms",
        str(len(DOCUMENTS) * 1000),
    ]


def _model_release_id(source: pathlib.Path) -> str:
    document = json.loads(source.read_text(encoding="utf-8"))
    observations = document.get("observations", [])
    return str(observations[0]["provenance"]["modelReleaseId"])


def start_surface(
    *,
    label: str,
    uri: str,
    database_url: str,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
    port: int,
    token: str,
    workspace: pathlib.Path,
) -> tuple[Process, pathlib.Path]:
    """起一个常驻检索面；就绪信号用 `--out` 写出的 JSON（比抓 stdout 更确定）。"""
    out = workspace / f"serve-{label}.json"
    process = Process(
        f"surface-{label}",
        [
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
            token,
            "--out",
            str(out),
        ],
    )
    process.start()
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if out.is_file():
            document = json.loads(out.read_text(encoding="utf-8"))
            if document.get("address"):
                return process, out
        if process.process is not None and process.process.poll() is not None:
            raise AssertionError(
                f"{label} surface exited before readiness: stdout={process.lines[-5:]} "
                f"stderr={process.stderr[-5:]}"
            )
        time.sleep(0.1)
    raise AssertionError(f"{label} surface never reported readiness")


def surface_call(
    endpoint: str,
    *,
    query: str = QUERY,
    token: str = SURFACE_TOKEN,
    principal: str = PRINCIPAL,
    limit: int = 3,
):
    """裸 gRPC 调用：用来检查线上真正传了什么，而不是只看 API 加工后的结果。"""
    channel = grpc.insecure_channel(endpoint)
    try:
        stub = index_pb2_grpc.IndexSearchServiceStub(channel)
        return stub.SearchSemantic(
            index_pb2.SemanticSearchRequest(
                query=query, principal=principal, vector_index_key=KEY, limit=limit
            ),
            timeout=30,
            metadata=(("authorization", "Bearer " + token),),
        )
    finally:
        channel.close()


def api_settings(
    database_url: str, *, endpoint: str = "", token: str = SURFACE_TOKEN, principal: str = PRINCIPAL
) -> Settings:
    fields: dict = {
        "database_url": database_url,
        "api_token": SecretStr(API_TOKEN),
        "principal": principal,
    }
    if endpoint:
        fields["index_search_endpoint"] = endpoint
        fields["index_search_token"] = SecretStr(token)
    return Settings(**fields)


def search(client: TestClient, body: dict) -> tuple[int, dict]:
    response = client.post(
        "/v1/materials:search", headers={"Authorization": f"Bearer {API_TOKEN}"}, json=body
    )
    return response.status_code, response.json()


def stage_foreign_release(conn, release_id: str) -> None:
    """登记一个"另一个模型身份"的 release（同源守卫要能把它读出来）。"""
    conn.execute(
        "INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,"
        "config_hash) VALUES (%s,'bge','0.0',%s,'cpu',%s) ON CONFLICT (model_release_id) "
        "DO NOTHING",
        (release_id, DIGEST, DIGEST),
    )


def restage_all_ready(conn, release_id: str) -> int:
    """把该 key 下**所有** ready 行改成指定身份（造"整库只有另一个模型"的状态）。"""
    return conn.execute(
        "UPDATE embedding_record SET model_release_id=%s WHERE vector_index_key=%s "
        "AND state='ready'",
        (release_id, KEY),
    ).rowcount


def stage_extra_ready_row(conn, release_id: str, marker: str) -> str:
    """再塞一条真实格式的 ready 行（另一个身份），造"同一个 collection 混装"的状态。"""
    embedding_id = records.embedding_id_for(f"obs_semantic_foreign_{marker}", MATERIAL, 1)
    conn.execute(
        "INSERT INTO embedding_record(embedding_id,material_unit_id,material_revision,"
        "model_release_id,vector_ref,dimension,content_hash,state,observation_id,"
        "vector_index_key,indexed_at) VALUES (%s,%s,1,%s,%s,%s,%s,'ready',%s,%s,now())",
        (
            embedding_id,
            MATERIAL,
            release_id,
            f"milvus://{KEY}/{embedding_id}",
            DIMENSION,
            DIGEST,
            f"obs_semantic_foreign_{marker}",
            KEY,
        ),
    )
    return embedding_id


def serve_arguments(
    uri: str,
    database_url: str,
    port: int,
    token: str,
    out: pathlib.Path,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
) -> list[str]:
    return [
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
        token,
        "--out",
        str(out),
    ]


def verify(  # noqa: PLR0915 - 验收脚本按场景直排，拆函数会让"哪一步在验什么"更难读
    report_path: pathlib.Path,
    database_url: str,
    workspace: pathlib.Path,
    model_dir: pathlib.Path,
    model_file: str,
    provider: str,
    keep_workspace: bool,
) -> list[str]:
    failures: list[str] = []
    document = json.loads(report_path.read_text(encoding="utf-8"))
    observations = [observation_from_json(item) for item in document.get("observations", [])]
    release_id = observations[0].provenance.model_release_id
    uri = str(workspace / "vector-semantic.db")
    print(f"workspace: {workspace}")
    print(f"vector uri: {uri}  collection: {KEY}  dimension: {DIMENSION}")
    print(f"release: {release_id}")

    schema = "semantic_" + uuid.uuid4().hex
    admin = psycopg.connect(database_url, autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(database_url, options=f"-c search_path={schema},public")
    surfaces: list[Process] = []
    try:
        migrate(isolated)
        with psycopg.connect(isolated) as conn:
            seed_facts(conn, observations, owner=PRINCIPAL)

        # ── 场景 1：真实观测落库（Milvus + PostgreSQL 两侧都要对账） ────────
        first = run_cli(index_arguments(uri, isolated, report_path, MATERIAL), 0, failures)
        indexed = first.get("indexed", [])
        check(
            len(indexed) == len(observations),
            f"expected {len(observations)} indexed embeddings, got {len(indexed)}",
            failures,
        )
        for row in indexed:
            check(row.get("state") == "ready", f"embedding not ready: {row}", failures)
            check(row.get("confirmed") is True, f"embedding not confirmed: {row}", failures)
            check(row.get("dimension") == DIMENSION, f"wrong dimension: {row}", failures)
        with psycopg.connect(isolated) as conn:
            ready = conn.execute(
                "SELECT count(*), count(DISTINCT model_release_id) FROM embedding_record "
                "WHERE vector_index_key=%s AND state='ready'",
                (KEY,),
            ).fetchone()
            check(
                ready == (len(observations), 1),
                f"the fact store holds {ready} ready rows, expected "
                f"{len(observations)} in one release",
                failures,
            )
        inspect = run_cli(["--uri", uri, "inspect", "--vector-index-key", KEY], 0, failures)
        check(
            inspect.get("rows") == len(observations),
            f"another process sees {inspect.get('rows')} rows, expected {len(observations)}",
            failures,
        )
        check(
            inspect.get("dimension") == DIMENSION,
            f"the collection dimension is {inspect.get('dimension')}, expected {DIMENSION}",
            failures,
        )

        # ── 场景 2：检索面必须显式带令牌才允许启动 ────────────────────────
        no_token = subprocess.run(
            [
                sys.executable,
                "-m",
                CLI_MODULE,
                "--uri",
                uri,
                "--database-url",
                isolated,
                "serve",
                "--vector-index-key",
                KEY,
                "--port",
                str(free_port()),
                "--model-dir",
                str(model_dir),
                "--model-file",
                model_file,
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=CLI_TIMEOUT_S,
        )
        check(
            no_token.returncode != 0 and "index_auth_token_required" in no_token.stderr,
            f"the surface started without a token: rc={no_token.returncode} "
            f"{no_token.stderr[-200:]}",
            failures,
        )

        # ── 场景 3：启动常驻检索面（持有向量库的唯一进程） ────────────────
        port = free_port()
        endpoint = f"127.0.0.1:{port}"
        surface, readiness_path = start_surface(
            label="main",
            uri=uri,
            database_url=isolated,
            model_dir=model_dir,
            model_file=model_file,
            provider=provider,
            port=port,
            token=SURFACE_TOKEN,
            workspace=workspace,
        )
        surfaces.append(surface)
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        check(
            readiness.get("index_version") == "milvus-flat-cosine-v1",
            f"the surface reports an unexpected index version: {readiness.get('index_version')}",
            failures,
        )
        check(
            readiness.get("collection") == KEY,
            f"the surface serves another collection: {readiness.get('collection')}",
            failures,
        )
        check(
            readiness.get("encoder", {}).get("release_id") == release_id,
            "the query encoder is not the same release as the indexed vectors: "
            f"{readiness.get('encoder')} vs {release_id}",
            failures,
        )

        # ── 场景 4：端到端语义检索（真实编码 + 真实近邻 + 真实水合） ──────
        settings = api_settings(isolated, endpoint=endpoint)
        with TestClient(create_app(settings)) as client:
            status, body = search(
                client, {"mode": "semantic", "query": DOCUMENTS[0], "limit": len(DOCUMENTS)}
            )
            check(status == 200, f"a self query failed: {status} {body}", failures)
            check(body.get("mode") == "semantic", f"wrong mode: {body.get('mode')}", failures)
            check(
                body.get("index_version") == "milvus-flat-cosine-v1",
                f"wrong index version: {body.get('index_version')}",
                failures,
            )
            check(
                body.get("vector_index_key") == KEY,
                f"wrong collection in the response: {body.get('vector_index_key')}",
                failures,
            )
            hits = body.get("hits", [])
            check(bool(hits), "a 3-document collection returned no hit at all", failures)
            if hits:
                top = hits[0]
                check(
                    top.get("observation_id") == observations[0].observation_id,
                    f"self query did not return its own observation first: {top}",
                    failures,
                )
                check(
                    abs(float(top.get("distance", 0.0)) - 1.0) < 1e-3,
                    f"cosine self distance is {top.get('distance')}, expected 1.0",
                    failures,
                )
            check(
                (body.get("unindexed_hits"), body.get("unresolved_hits")) == (0, 0),
                f"a hydrated query reported drops: {body.get('unindexed_hits')}/"
                f"{body.get('unresolved_hits')}",
                failures,
            )
            materials = body.get("materials", [])
            check(bool(materials), "no material fact was hydrated for the hit", failures)
            if materials:
                hydrating = materials[0]
                check(
                    hydrating.get("material_unit_id") == MATERIAL
                    and hydrating.get("stream_id") == STREAM
                    and hydrating.get("revision") == 1,
                    "the hydrated material is not the indexed one: "
                    f"{hydrating.get('material_unit_id')}",
                    failures,
                )
                payload = hydrating.get("observations", [{}])[0].get("payload", {})
                check(
                    payload.get("text") == DOCUMENTS[0],
                    "the hydrated material does not carry the indexed text",
                    failures,
                )

            # 语义查询：排第一的必须是语义上对应的那一段，而不是随便一条。
            status, body = search(client, {"mode": "semantic", "query": QUERY, "limit": 3})
            check(status == 200, f"a semantic query failed: {status} {body}", failures)
            ranked = body.get("hits", [])
            check(
                bool(ranked)
                and ranked[0].get("observation_id") == observations[EXPECTED_TOP].observation_id,
                f"{QUERY!r} did not rank the expected document first: {ranked}",
                failures,
            )

            # ── 场景 5：非 owner 命中必须被丢弃并计数，不能表现成"没有命中" ──
            with TestClient(
                create_app(api_settings(isolated, endpoint=endpoint, principal=INTRUDER))
            ) as alien:
                alien_status, alien_body = search(alien, {"mode": "semantic", "query": QUERY})
            check(alien_status == 200, f"a non-owner query failed: {alien_status}", failures)
            check(
                alien_body.get("materials") == [] and alien_body.get("hits") == [],
                f"a non-owner principal received hits: {alien_body.get('hits')}",
                failures,
            )
            check(
                alien_body.get("unindexed_hits") == len(observations),
                f"non-owner hits were not accounted as dropped: {alien_body.get('unindexed_hits')}",
                failures,
            )

            # ── 场景 6：keyword 不排名，也不带语义字段 ──────────────────────
            status, body = search(client, {"query": "销售"})
            check(status == 200, f"a keyword query failed: {status} {body}", failures)
            check(
                body.get("mode") == "keyword"
                and body.get("hits") == []
                and body.get("vector_index_key") == ""
                and body.get("index_version") == "postgres-literal-v1",
                f"keyword mode reported semantic fields: {body}",
                failures,
            )
            check(
                len(body.get("materials", [])) == 1,
                f"keyword search lost the material: {len(body.get('materials', []))}",
                failures,
            )

        # ── 场景 7：线上真正传的东西（裸 gRPC，看不清就不算验收） ────────
        wire = surface_call(endpoint, query=DOCUMENTS[0], limit=len(DOCUMENTS))
        wire_text = MessageToJson(wire, ensure_ascii=False)
        wire_fields = set(MessageToDict(wire))
        check(
            wire_fields <= RESPONSE_FIELDS,
            "the wire response carries fields outside the contract: "
            f"{wire_fields - RESPONSE_FIELDS}",
            failures,
        )
        for hit in wire.hits:
            hit_fields = set(MessageToDict(hit))
            check(
                hit_fields <= HIT_FIELDS,
                f"a hit carries fields outside the contract: {hit_fields - HIT_FIELDS}",
                failures,
            )
        check(
            wire.index_version == "milvus-flat-cosine-v1"
            and wire.vector_index_key == KEY
            and wire.collection == KEY,
            f"the wire response carries another contract: {wire_text[:200]}",
            failures,
        )
        check(
            wire.query_model_release_id == release_id and wire.query_dimension == DIMENSION,
            "the wire response does not report the query encoder identity",
            failures,
        )
        check(
            (wire.unindexed_hits, len(wire.hits)) == (0, len(DOCUMENTS)),
            f"the wire response lost hits: {wire.unindexed_hits}/{len(wire.hits)}",
            failures,
        )
        if wire.hits:
            check(
                abs(float(wire.hits[0].distance) - 1.0) < 1e-3,
                f"the wire distance for a self query is {wire.hits[0].distance}",
                failures,
            )
        for label, needle in (
            ("a host path", str(workspace)),
            ("the database url", isolated),
            ("the weights directory", str(model_dir)),
            ("the encoded text", DOCUMENTS[0]),
        ):
            check(needle not in wire_text, f"the retrieval surface leaks {label}", failures)

        # ── 场景 8：令牌不符是配置问题（503，但不可重试） ─────────────────
        with TestClient(
            create_app(api_settings(isolated, endpoint=endpoint, token="x" * 48))
        ) as client:
            status, body = search(client, {"mode": "semantic", "query": QUERY})
        check(
            (status, body.get("reason_code"), body.get("retryable"))
            == (503, "semantic_index_unauthenticated", False),
            f"a wrong surface token was not reported as a configuration error: {status} {body}",
            failures,
        )

        # ── 场景 9：检索面不可达是可重试的，且绝不伪装成"没有命中" ────────
        dead = f"127.0.0.1:{free_port()}"
        with TestClient(create_app(api_settings(isolated, endpoint=dead))) as client:
            status, body = search(client, {"mode": "semantic", "query": QUERY})
        check(
            (status, body.get("reason_code"), body.get("retryable"))
            == (503, "semantic_index_unreachable", True),
            f"an unreachable surface was not reported as retryable: {status} {body}",
            failures,
        )
        check("materials" not in body, "an unreachable surface still returned materials", failures)

        # ── 场景 10：未配置 ≠ 曾经 501（capability 只报配置事实） ─────────
        with TestClient(create_app(api_settings(isolated))) as client:
            status, body = search(client, {"mode": "semantic", "query": QUERY})
            check(
                (status, body.get("reason_code"), body.get("retryable"))
                == (503, "semantic_search_unavailable", False),
                f"an unconfigured semantic mode was not reported as unavailable: {status} {body}",
                failures,
            )
            capabilities = client.get(
                "/v1/capabilities", headers={"Authorization": f"Bearer {API_TOKEN}"}
            ).json()["capabilities"]
            semantic = next(item for item in capabilities if item["name"] == "semantic_search")
            check(
                semantic["available"] is False
                and semantic["reason"] == "semantic_search_unavailable",
                f"capabilities lie about an unconfigured surface: {semantic}",
                failures,
            )
        with TestClient(create_app(api_settings(isolated, endpoint=endpoint))) as client:
            capabilities = client.get(
                "/v1/capabilities", headers={"Authorization": f"Bearer {API_TOKEN}"}
            ).json()["capabilities"]
            semantic = next(item for item in capabilities if item["name"] == "semantic_search")
            check(
                semantic["available"] is True and semantic["reason"] == "",
                f"capabilities under-report a configured surface: {semantic}",
                failures,
            )

        # ── 场景 11：同源守卫的两种形态（整库另一个模型 / 同库混装） ──────
        prefix = release_id.rsplit("@", 1)[0] + "@"
        foreign_one, foreign_two = prefix + "0" * 12, prefix + "1" * 12
        with psycopg.connect(isolated) as conn:
            stage_foreign_release(conn, foreign_one)
            moved = restage_all_ready(conn, foreign_one)
        check(
            moved == len(observations),
            f"could not restage the collection onto another release: {moved} rows",
            failures,
        )
        with TestClient(create_app(api_settings(isolated, endpoint=endpoint))) as client:
            status, body = search(client, {"mode": "semantic", "query": QUERY})
        check(
            (status, body.get("reason_code"), body.get("retryable"))
            == (502, "query_model_release_mismatch", False),
            f"a collection built by another model was not rejected: {status} {body}",
            failures,
        )
        with psycopg.connect(isolated) as conn:
            stage_foreign_release(conn, foreign_two)
            extra = stage_extra_ready_row(conn, foreign_two, "mixed")
        with TestClient(create_app(api_settings(isolated, endpoint=endpoint))) as client:
            status, body = search(client, {"mode": "semantic", "query": QUERY})
        check(
            (status, body.get("reason_code"), body.get("retryable"))
            == (502, "vector_index_model_release_mixed", False),
            f"two model releases in one collection were not reported: {status} {body}",
            failures,
        )
        with psycopg.connect(isolated) as conn:
            restage_all_ready(conn, release_id)
            conn.execute("DELETE FROM embedding_record WHERE embedding_id=%s", (extra,))
        with TestClient(create_app(api_settings(isolated, endpoint=endpoint))) as client:
            status, _ = search(client, {"mode": "semantic", "query": QUERY})
        check(status == 200, f"the guard did not clear with the foreign rows: {status}", failures)

        # ── 场景 12：collection 契约漂移必须在启动时就拒绝 ────────────────
        contract_uri = str(workspace / "vector-contract.db")
        stage_drifted_collection(contract_uri, KEY, DIMENSION)
        contract_out = workspace / "serve-contract.json"
        refused = subprocess.run(
            serve_arguments(
                contract_uri,
                isolated,
                free_port(),
                SURFACE_TOKEN,
                contract_out,
                model_dir,
                model_file,
                provider,
            ),
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=CLI_TIMEOUT_S,
        )
        check(
            refused.returncode != 0,
            "the surface served a collection whose contract does not match",
            failures,
        )
        emitted = (
            json.loads(contract_out.read_text(encoding="utf-8")) if contract_out.is_file() else {}
        )
        check(
            emitted.get("error_code") == "vector_collection_contract_mismatch",
            f"a drifted collection was not reported as a contract mismatch: {emitted}",
            failures,
        )

        # ── 场景 13：目录锁（单写进程）+ 停止后立刻可用 ───────────────────
        locked_out = workspace / "serve-locked.json"
        locked = subprocess.run(
            serve_arguments(
                uri,
                isolated,
                free_port(),
                SURFACE_TOKEN,
                locked_out,
                model_dir,
                model_file,
                provider,
            ),
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=CLI_TIMEOUT_S,
        )
        check(
            locked.returncode != 0,
            "a second surface took over the data directory that is already held",
            failures,
        )
        locked_emitted = (
            json.loads(locked_out.read_text(encoding="utf-8")) if locked_out.is_file() else {}
        )
        check(
            locked_emitted.get("error_code") == "vector_store_locked",
            f"a locked data directory was not reported as locked: {locked_emitted}",
            failures,
        )
        surface.kill()
        check(surface.finish(timeout=30) == 0, "the surface did not stop gracefully", failures)
        surfaces.remove(surface)
        released = run_cli(["--uri", uri, "inspect", "--vector-index-key", KEY], 0, failures)
        check(
            released.get("rows") == len(observations),
            f"the released data directory is not usable again: {released.get('rows')}",
            failures,
        )
    finally:
        for process in surfaces:
            process.kill()
            try:
                process.finish(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - 收尾尽力而为
                pass
        if not keep_workspace:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument("--model-file", default=DEFAULT_MODEL_FILE)
    parser.add_argument("--provider", default="cpu")
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()

    model_dir = pathlib.Path(arguments.model_dir).expanduser()
    if not model_dir.is_dir():
        raise SystemExit(f"model dir not found: {model_dir}")
    database_url, origin = derive_database_url(arguments.database_url)
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-semantic-"))
    print(f"database: {describe_target(database_url)} (from {origin})")
    print(f"weights: {model_dir} ({arguments.provider})")
    started = time.monotonic()
    failures: list[str] = []
    report_path = encode_documents(
        workspace, model_dir, arguments.model_file, arguments.provider, failures
    )
    if failures or not report_path.is_file():
        print("\nsemantic search acceptance FAILED before the first query:")
        for failure in failures or ["no embedding report was produced"]:
            print(f"  - {failure}")
        return 1
    failures = verify(
        report_path,
        database_url,
        workspace,
        model_dir,
        arguments.model_file,
        arguments.provider,
        arguments.keep_workspace,
    )
    if failures:
        print("\nsemantic search acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        f"\nsemantic search acceptance: real BGE -> Milvus Lite -> resident surface -> "
        f"API hydration passed in {round(time.monotonic() - started, 1)}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

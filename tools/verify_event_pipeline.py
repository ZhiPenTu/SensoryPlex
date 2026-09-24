"""事件链路常驻的容器内闭环验收（ADR-027）。

链路：真实写侧 → 常驻 relay → JetStream → 常驻 index → 向量 → api 语义检索。

在 api 容器内执行（`make event-pipeline-check`）。两个常驻服务（relay / index）由
`./deploy/up-events.sh` 起在 compose 里；本脚本**不自己起** relay、index 或检索面——它验的正是
"这两个服务真的在 compose 里常驻着，并且把事件搬成了可被检索的向量"。

角色（除了一条刻意越界的配置探针，其余都是真实进程与真实服务）：

1. 本脚本（编排 + 对账，在 api 容器内执行：真 PostgreSQL / 真 JetStream / 真向量库都在 compose
   里）；
2. 真实写侧 `sensoryplex_api.infrastructure.materials.append_material`（素材 + 观测 + outbox
   同一个事务，事件由它产生，不是本脚本手写的）；
3. compose 里**常驻**的 `relay` 服务（outbox → JetStream）；
4. 真实 NATS JetStream；
5. compose 里**常驻**的 `index` 服务（消费 → 真 BGE 编码 → 真 Milvus Lite → 检索面同进程）；
6. 运行中的 api 进程本身：`POST /v1/materials:search` 的 `mode=semantic` 走真 gRPC 转到检索面。

判定标准（全部来自真实执行）：

- 分级背压准入（ADR-019 / ADR-027）：relay 与 index 的状态行都必须是 `admitted`，两者报同一档位
  与同一个上限，且声明值 <= 上限；另把一条**越界**配置喂给真实 relay CLI，必须在连数据库之前
  按 `event_inflight_exceeds_tier_cap` 拒绝启动（不夹取、不静默降级）；
- 事件真的被常驻进程搬运：写侧落下素材后，relay 的 `published_total` 与 index 的
  `consumed_total` 都必须各自**增长**（计数器单调，所以比较的是本次增量，不是"文件还在"）；
- 事件真的变成向量：`embedding_record` 出现 ready 行（带 `vector_ref` / `indexed_at` /
  `model_release_id`），且 `vector_ref` 是受控引用而不是字节；
- 检索面真的能回答：api 的 `mode=semantic` 返回该素材，且它是本次查询的第一名（不是"返回点什么"）；
- 事实回查真的在挡 stale：删掉事实行后同一个查询不再返回该素材，而陈旧向量被计入
  `unindexed_hits`——这正是"向量在、事实不在"应有的语义，也是本验收刻意留下的残余
  （向量本体不随事实删除而消失，所以按 embedding_id 计数并如实报告）；
- 不外泄：状态行与 HTTP 响应里没有 DSN、主机路径、令牌；状态行只有计数、档位与原因码；
- 清理：本次写入的素材 / 观测 / 资产 / 流 / 来源 / outbox / 记账行全部删除，并复查残留为 0。

刻意不做的事（未验证边界，见 ADR-027）：不验检索面停机时的降级路径（`semantic_index_unreachable`
由 tests/integration/test_semantic_search.py 覆盖）、不验服务端 Milvus 形态、不验多副本消费、
不验跨主机 NATS 集群、不验 `ack_wait` 到期后的自动重投。
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import psycopg  # noqa: E402
from edge_material_sdk.generated.common.v1 import common_pb2 as common  # noqa: E402
from edge_material_sdk.generated.material.v1 import material_pb2 as material  # noqa: E402
from sensoryplex_api.infrastructure import materials as api_materials  # noqa: E402
from sensoryplex_relay import residency  # noqa: E402

EVENTS_DIR = ROOT / ".data/events"
RELAY_STATUS = EVENTS_DIR / "relay-status.json"
INDEX_STATUS = EVENTS_DIR / "index-status.json"
RELAY_MODULE = "sensoryplex_relay.cli"
# 本脚本写进共享开发库的每一行都带这个标记：既是清理范围，也是"这条路是谁留下的"。
MARKER = "compose_acceptance"
# 一次运行一个后缀：上一次崩溃留下的行也能被同一个 LIKE 模式扫掉。
RUN_ID = uuid.uuid4().hex[:8]
STREAM = f"stream_{MARKER}"
SOURCE = f"source_{MARKER}"
ASSET = f"asset_{MARKER}"
UPSTREAM_RELEASE = f"acceptance:{MARKER}-text@0"
UPSTREAM_MODEL = f"acceptance-{MARKER}-text"
DIGEST = "sha256:" + "c" * 64
# 两条素材：一条与查询语义相关，一条无关。查询必须把相关的那条排第一。
#
# 文本与查询都带**本次运行**的批次标记：向量本体不随事实行删除而消失，所以上一次运行留下的
# 陈旧向量会一直留在向量库里。带同一个批次标记的查询只会命中"留给它的"那些陈旧向量，不会
# 挤掉真实用户的查询窗口——这也是为什么下面的判定是"相对基线"而不是"绝对为 0"。
DOCUMENTS = (
    ("alpha", "合成氨装置三号压缩机联轴器振动超标，需要停机复测动平衡并更换密封件"),
    ("beta", "员工食堂本周菜单：周一红烧排骨，周二清蒸鲈鱼，周三麻婆豆腐"),
)
QUERY = "压缩机联轴器振动超标怎么处理"
TAG = f"（验收批次 {RUN_ID}）"
# 状态行每轮都会重写（relay 1s 一轮），所以"新鲜"是一个可用的事实：旧文件不能读成"在跑"。
STATUS_FRESHNESS_S = 90.0
PATH_TIMEOUT_S = 180.0
CLEANUP_TIMEOUT_S = 60.0
LEAK_NEEDLES = ("postgresql://", "/Users/", "postgres:5432", "Bearer ", "milvus.db")
# 清理顺序 = 依赖顺序（外键没有 ON DELETE CASCADE）。
CLEANUP_ORDER = (
    ("embedding_record", "material_unit_id"),
    ("material_observation", "material_unit_id"),
    ("material_source_reference", "material_unit_id"),
    ("material_unit", "material_unit_id"),
    ("observation", "observation_id"),
    ("timeline_item", "item_id"),
    ("media_asset", "asset_id"),
    ("stream_session", "stream_id"),
    ("media_source", "source_id"),
    ("consumed_event", "event_id"),
    ("event_outbox", "event_id"),
)


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def read_json(path: pathlib.Path) -> dict:
    """读一份原子替换写出的 JSON 文档；不存在或读坏都返回空文档。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def fresh_status(path: pathlib.Path, label: str, failures: list[str]) -> dict:
    """读状态行，并判定它确实是**刚才**写的：停在那里的进程不能被读成"在跑"。"""
    document = read_json(path)
    if not document:
        failures.append(
            f"{label}: 状态行不存在或不可解析（{path.name}）——先 ./deploy/up-events.sh 起常驻服务"
        )
        return {}
    age = time.time() - path.stat().st_mtime
    check(
        age <= STATUS_FRESHNESS_S,
        f"{label}: 状态行已经 {age:.0f}s 没有更新，常驻进程可能停了（{path.name}）",
        failures,
    )
    return document


def admission_of(document: dict, label: str, failures: list[str]) -> tuple[str, int, int]:
    """分级背压（ADR-027）：状态行必须带一份**自洽**的准入结论。"""
    declared = document.get("inflight_declared")
    capacity = document.get("inflight_capacity")
    check(
        document.get("inflight_state") == residency.ADMITTED,
        f"{label}: 准入结论不是 admitted：{document.get('inflight_state')!r}"
        f"（tier={document.get('resident_tier')!r}）",
        failures,
    )
    consistent = (
        isinstance(declared, int) and isinstance(capacity, int) and 0 < declared <= capacity
    )
    check(
        consistent,
        f"{label}: 在飞声明值与上限不自洽：declared={declared!r} capacity={capacity!r}",
        failures,
    )
    return str(document.get("resident_tier")), int(declared or 0), int(capacity or 0)


def counters(document: dict, key: str) -> int:
    value = document.get(key, 0)
    return int(value) if isinstance(value, int) else 0


def wait_for_growth(
    path: pathlib.Path,
    key: str,
    baseline: int,
    minimum: int,
    *,
    label: str,
    failures: list[str],
    timeout: float = PATH_TIMEOUT_S,
) -> dict:
    """等某个累计计数器增长到 baseline+minimum；超时就把最后一份状态行原样报出来。"""
    deadline = time.monotonic() + timeout
    document: dict = {}
    while time.monotonic() < deadline:
        document = read_json(path)
        if counters(document, key) - baseline >= minimum:
            return document
        time.sleep(0.5)
    failures.append(
        f"{label}: 等了 {timeout:.0f}s，{key} 没有增长 {minimum} 条"
        f"（baseline={baseline}，最后一份状态行={document}）"
    )
    return document


def search(base_url: str, token: str, query: str, limit: int = 5) -> tuple[int, dict]:
    """真实 HTTP 语义检索：api 会经 gRPC 转发给常驻检索面，再回查 PostgreSQL 事实。"""
    body = json.dumps({"mode": "semantic", "query": query, "limit": limit}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/materials:search",
        data=body,
        method="POST",
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(payload)
        except json.JSONDecodeError:
            return error.code, {"detail": payload[:400]}


def seed_references(conn, principal: str) -> None:
    """写侧前置事实：来源 / 流 / 资产 / 时间轴项 / 上游模型身份（都带本次运行的标记）。"""
    conn.execute(
        "INSERT INTO media_source(source_id,type,uri_redacted,owner) "
        "VALUES (%s,'file','private://not-exposed',%s) ON CONFLICT (source_id) DO NOTHING",
        (SOURCE, principal),
    )
    conn.execute(
        "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
        "VALUES (%s,%s,now(),'stopped') ON CONFLICT (stream_id) DO NOTHING",
        (STREAM, SOURCE),
    )
    conn.execute(
        "INSERT INTO media_asset(asset_id,stream_id,object_uri,sha256,codec,duration_ms) "
        "VALUES (%s,%s,'private://not-exposed',%s,'acceptance',%s) "
        "ON CONFLICT (asset_id) DO NOTHING",
        (ASSET, STREAM, DIGEST, len(DOCUMENTS) * 1000),
    )
    for name, _ in DOCUMENTS:
        conn.execute(
            "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) "
            "VALUES (%s,%s,'video_frame',0,1000) ON CONFLICT (item_id) DO NOTHING",
            (item_id(name), STREAM),
        )
    conn.execute(
        "INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,"
        "config_hash) VALUES (%s,%s,'0.1.0',%s,'no_inference',%s) ON CONFLICT DO NOTHING",
        (UPSTREAM_RELEASE, UPSTREAM_MODEL, DIGEST, DIGEST),
    )


def run_id_of(name: str) -> str:
    return f"{MARKER}_{RUN_ID}_{name}"


def item_id(name: str) -> str:
    return f"item_{run_id_of(name)}"


def upstream_observation(name: str, text: str):
    """上游观测的身份：本脚本给的是**文本**输入，不是模型输出，所以登记成"无推理"身份。"""
    observation = material.Observation(
        observation_id=f"obs_{run_id_of(name)}",
        modality="ocr_blocks",
        stream_id=STREAM,
        source_id=SOURCE,
        source_item_id=item_id(name),
        time_range=common.TimeRange(start_ms=0, end_ms=1000),
        confidence_unavailable_reason="acceptance_text_input_has_no_model_confidence",
        content_hash=DIGEST,
        quality_state="final",
        timing_source="media_pts",
        created_at_unix_ms=int(time.time() * 1000),
        provenance=material.Provenance(
            plugin="org.sensoryplex.verify-event-pipeline",
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


def write_materials(conn, principal: str) -> list[str]:
    """用**真实写侧**追加素材：事实与 outbox 事件在同一个事务里落下。"""
    seed_references(conn, principal)
    event_ids: list[str] = []
    for name, text in DOCUMENTS:
        unit = material.MaterialUnit(
            material_unit_id=run_id_of(name),
            stream_id=STREAM,
            time_range=common.TimeRange(start_ms=0, end_ms=1000),
            status="fast_ready",
            revision=1,
            observations=[upstream_observation(name, text + TAG)],
            tags=["acceptance"],
            pipeline_version="event-pipeline-acceptance-v1",
            created_at_unix_ms=int(time.time() * 1000),
            source_refs=[
                material.SourceReference(
                    asset_id=ASSET,
                    time_range=common.TimeRange(start_ms=0, end_ms=1000),
                    content_hash=DIGEST,
                )
            ],
        )
        if not api_materials.append_material(conn, unit, trace_id="event-pipeline-acceptance"):
            raise RuntimeError(f"the acceptance material {name} was not appended")
        event_ids.append(f"material:{run_id_of(name)}:1")
    return event_ids


def cleanup(conn) -> dict[str, int]:
    """按依赖顺序删除本脚本写下的每一行（含上一次崩溃留下的残留）。

    模型身份（`model_release`）是**不可变事实**，刻意不删：它描述的是"这份输入没有推理"，
    删掉它并不会让向量或素材更干净，反而会把一条真实来源抹掉。
    """
    pattern = f"%{MARKER}%"
    deleted: dict[str, int] = {}
    for table, column in CLEANUP_ORDER:
        cursor = conn.execute(f"DELETE FROM {table} WHERE {column} LIKE %s", (pattern,))
        deleted[table] = cursor.rowcount
    return deleted


def admission_refusal(failures: list[str]) -> str:
    """把一条越界配置喂给真实 relay CLI：必须在连数据库之前按原因码拒绝启动。"""
    environ = dict(os.environ)
    environ[residency.TIER_VAR] = "small"
    environ[residency.CAPACITY_VAR] = "16"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            RELAY_MODULE,
            "--database-url",
            "postgresql://placeholder/never-connected",
            "--batch",
            "64",
        ],
        capture_output=True,
        text=True,
        env=environ,
        timeout=120,
    )
    message = (completed.stderr or "").strip().splitlines()
    text = message[-1] if message else ""
    check(
        completed.returncode != 0 and "event_inflight_exceeds_tier_cap" in text,
        f"越界配置没有被拒绝启动：exit={completed.returncode} stderr={text[:200]!r}",
        failures,
    )
    check(
        completed.stdout.strip() == "",
        f"越界配置仍然打印了状态行（说明它开始跑了）：{completed.stdout[:200]!r}",
        failures,
    )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8091", help="api 基址")
    parser.add_argument("--database-url", default="", help="默认取 SENSORYPLEX_DATABASE_URL")
    parser.add_argument("--principal", default="", help="默认取 SENSORYPLEX_PRINCIPAL")
    arguments = parser.parse_args()

    failures: list[str] = []
    database_url = arguments.database_url or os.getenv("SENSORYPLEX_DATABASE_URL", "")
    if not database_url:
        print("event pipeline acceptance: 需要 SENSORYPLEX_DATABASE_URL（在 api 容器内执行）")
        return 1
    principal = arguments.principal or os.getenv("SENSORYPLEX_PRINCIPAL", "local-developer")
    token = os.getenv("SENSORYPLEX_API_TOKEN", "")
    if not token:
        print("event pipeline acceptance: 需要 SENSORYPLEX_API_TOKEN（在 api 容器内执行）")
        return 1

    # ── 1. 常驻服务在跑 + 分级背压准入 ──────────────────────────────────────
    relay_document = fresh_status(RELAY_STATUS, "relay", failures)
    index_document = fresh_status(INDEX_STATUS, "index", failures)
    relay_tier, relay_declared, relay_capacity = admission_of(relay_document, "relay", failures)
    index_tier, index_declared, index_capacity = admission_of(index_document, "index", failures)
    check(
        relay_tier == index_tier and relay_capacity == index_capacity,
        f"两个常驻服务报的不是同一档位：relay=({relay_tier},{relay_capacity})"
        f" index=({index_tier},{index_capacity})",
        failures,
    )
    print(
        f"[1] 常驻服务：relay 档位={relay_tier} 上限={relay_capacity} 每轮认领={relay_declared}；"
        f"index 消费在飞={index_declared}"
    )
    refusal = admission_refusal(failures)
    print(f"[1] 越界配置被拒：{refusal[:120]}")

    # ── 2. 基线：本次批次的查询在写入**之前**是什么样 ────────────────────────
    # 上一次运行留下的陈旧向量也在这个窗口里，所以后面的判定都是相对基线，而不是"绝对为 0"。
    query = QUERY + TAG
    baseline_status, baseline = search(arguments.base_url, token, query)
    check(
        baseline_status == 200,
        f"写入之前语义检索就不可用：HTTP {baseline_status} {baseline}"
        "（api 容器的检索面配置：./deploy/up.sh api 重建后重试）",
        failures,
    )
    baseline_unindexed = int(baseline.get("unindexed_hits") or 0)
    print(
        f"[2] 基线：materials={len(baseline.get('materials') or [])}"
        f" unindexed_hits={baseline_unindexed}（上一次运行的陈旧向量）"
    )

    # ── 3. 真实写侧落事件，等常驻进程把它搬走 ────────────────────────────────
    relay_before = counters(relay_document, "published_total")
    index_before = counters(index_document, "consumed_total")
    with psycopg.connect(database_url) as conn:
        swept = cleanup(conn)
        event_ids = write_materials(conn, principal)
    print(f"[3] 写侧落下 {len(event_ids)} 条事件（顺带清理上次残留 {sum(swept.values())} 行）")
    wait_for_growth(
        RELAY_STATUS,
        "published_total",
        relay_before,
        len(event_ids),
        label="relay",
        failures=failures,
    )
    wait_for_growth(
        INDEX_STATUS,
        "consumed_total",
        index_before,
        len(event_ids),
        label="index",
        failures=failures,
    )

    # ── 4. 事件真的变成了向量（ready 行 + 受控引用） ─────────────────────────
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT embedding_id,state,vector_ref,indexed_at,model_release_id "
            "FROM embedding_record WHERE material_unit_id LIKE %s ORDER BY embedding_id",
            (f"%{MARKER}%",),
        ).fetchall()
    check(
        len(rows) == len(event_ids),
        f"事件数 {len(event_ids)} 与向量行数 {len(rows)} 不一致",
        failures,
    )
    for embedding_id, state, vector_ref, indexed_at, record_release in rows:
        check(
            state == "ready" and indexed_at is not None,
            f"{embedding_id}: 消费出来的向量不是 ready：{(state, indexed_at)}",
            failures,
        )
        check(
            str(vector_ref or "").startswith("milvus://") and len(str(vector_ref)) < 200,
            f"{embedding_id}: vector_ref 不是受控引用：{vector_ref!r}",
            failures,
        )
        check(
            bool(record_release),
            f"{embedding_id}: 没有登记模型身份（model_release_id 为空）",
            failures,
        )
    print(f"[4] 向量落库：{len(rows)} 行 ready（引用形如 {rows[0][2] if rows else 'milvus://…'}）")

    # ── 5. api 的语义检索真的答了，并且把相关的那条排第一 ────────────────────
    status, body = search(arguments.base_url, token, query)
    check(status == 200, f"语义检索没有成功：HTTP {status} {body}", failures)
    hits = [hit.get("material_unit_id") for hit in (body.get("hits") or [])]
    check(body.get("mode") == "semantic", f"检索模式不是 semantic：{body.get('mode')!r}", failures)
    check(
        run_id_of("alpha") in hits,
        f"相关素材没有出现在命中里：hits={hits} unindexed={body.get('unindexed_hits')}",
        failures,
    )
    check(
        bool(hits) and hits[0] == run_id_of("alpha"),
        f"相关素材不是第一名：hits={hits}（语义检索必须排序，不是返回点什么）",
        failures,
    )
    check(
        int(body.get("unresolved_hits") or 0) == 0,
        f"检索面给出了 unresolved_hits（调用方无法解释的命中）：{body.get('unresolved_hits')}",
        failures,
    )
    # 本次新写下的向量都是 ready 的，所以"查不到的命中"不该比基线更多（可能更少：
    # 它们把窗口里的陈旧向量挤了出去）。
    check(
        int(body.get("unindexed_hits") or 0) <= baseline_unindexed,
        f"本次写入后出现了新的查不到命中：{body.get('unindexed_hits')} > 基线 {baseline_unindexed}",
        failures,
    )
    check(
        bool(body.get("index_version")) and bool(body.get("vector_index_key")),
        f"检索面没有给出 index_version / vector_index_key：{body}",
        failures,
    )
    print(f"[5] HTTP 语义检索：hits={hits} index_version={body.get('index_version')}")

    # ── 6. 事实回查挡 stale：删掉事实后不再返回，陈旧向量计入 unindexed_hits ──
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            cleanup(conn)
    _, after = search(arguments.base_url, token, query)
    after_hits = [hit.get("material_unit_id") for hit in (after.get("hits") or [])]
    check(
        run_id_of("alpha") not in after_hits,
        f"事实已删但素材仍被返回（回查没有挡住）：{after_hits}",
        failures,
    )
    check(
        int(after.get("unindexed_hits") or 0) >= 1,
        f"事实已删的陈旧向量没有被计入 unindexed_hits：{after}",
        failures,
    )
    print(f"[6] 事实回查：materials={len(after_hits)} unindexed_hits={after.get('unindexed_hits')}")

    # ── 7. 清理复查 + 不外泄 ────────────────────────────────────────────────
    with psycopg.connect(database_url) as conn:
        residual = {
            table: conn.execute(
                f"SELECT count(*) FROM {table} WHERE {column} LIKE %s", (f"%{MARKER}%",)
            ).fetchone()[0]
            for table, column in CLEANUP_ORDER
        }
    leftovers = {table: count for table, count in residual.items() if count}
    check(not leftovers, f"验收没有把自己写下的行清干净：{leftovers}", failures)

    surface = "\n".join(
        (
            RELAY_STATUS.read_text(encoding="utf-8") if RELAY_STATUS.is_file() else "",
            INDEX_STATUS.read_text(encoding="utf-8") if INDEX_STATUS.is_file() else "",
            json.dumps(body, ensure_ascii=False),
        )
    )
    for needle in LEAK_NEEDLES:
        check(needle not in surface, f"状态行或响应里出现了不该出现的东西：{needle!r}", failures)

    # 结果行：把"这次到底验了什么"写成可被外部消费的一份记录。
    print(
        json.dumps(
            {
                "event": "event.pipeline.acceptance",
                "mode": "semantic",
                "events": len(event_ids),
                "vectors": len(rows),
                "tier": relay_tier,
                "capacity": relay_capacity,
                "relay_inflight": relay_declared,
                "index_inflight": index_declared,
                "query_hits": len(hits),
                "unindexed_hits_baseline": baseline_unindexed,
                "unindexed_hits_after_cleanup": int(after.get("unindexed_hits") or 0),
                "residual_rows": leftovers,
                "failures": failures,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if failures:
        print("\n事件链路验收失败：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        "事件链路验收通过：常驻 relay → JetStream → 常驻 index → 向量 → api 语义检索，"
        "且事实回查与清理都对得上。"
    )
    # 如实交代这次运行留下的东西：向量本体不随事实行删除而消失（向量 GC 不在 ADR-027 范围）。
    print(
        f"提示：本次留下 {len(rows)} 条陈旧向量（会一直计入 unindexed_hits，"
        "但比不过带当前批次标记的查询）；要清空向量库用 ./deploy/down-events.sh --volumes。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

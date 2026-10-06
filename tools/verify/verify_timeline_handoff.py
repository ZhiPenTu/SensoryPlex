"""真实媒体端到端验收（ADR-028 §8）：Runtime → Timeline → 授权追加 → outbox → 真 JetStream。

这个是 TODO「真实媒体端到端」里**Runtime → Timeline → metadata writer/outbox** 这一段的
闭环证据，全程用真实授权样本，没有任何替身数据。

角色（除了刻意构造的那几个坏输入，其余都是真实进程 / 真实服务）：

1. 本脚本（编排 + 对账）。固定在**主机**执行：runtime 二进制是主机 Mach-O（容器里
   `Exec format error`），而 PostgreSQL / NATS 在宿主回环端口上都可达；
2. `sensoryplex-runtime replay --handoff-listen`（**真解码**，持有字节与保留表）；
3. `python -m edge_material_plugin_vlm_moondream`（真 VLM 插件进程，自己按 lease 读字节）；
4. `tools/ai_worker.py`（真 worker：buffer 模式 ⇒ Runtime 签发的描述符账本 + 真观测）；
5. `sensoryplex-runtime timeline`（**真融合**：按 pipeline 声明的栅格选窗 → 逐条准入 →
   写出素材 protobuf 与报告）；
6. `tools/timeline_handoff.py`（**授权追加**：引用事实 + 素材 + outbox 行同一个事务）；
7. `python -m sensoryplex_relay.cli --once`（真 relay：outbox → JetStream，确认发布）；
8. 真 PostgreSQL（本次新建隔离 schema + 跑真实迁移）、真 NATS JetStream（独立 stream/前缀，
   用完删掉，不碰开发用的 `sensoryplex-events`）。

判定标准（全部来自真实执行，不做"健康检查即通过"）：

- 链路真的产出了素材：`timeline` 报告的 `materials` 数等于"有观测的窗口数"，每条素材的
  `material_unit_id` 是 `material-<摘要前 12 位>-<窗口起点>`、起点落在栅格上、最后一段被
  时长夹住；`observations_rejected == 0`，且每条观测都能回指一条 Runtime 签发的账目；
- 原片引用不是帧摘要冒充的：素材 `source_refs[].content_hash` 等于**整文件**摘要，也等于
  库里 `media_asset.sha256`；`asset_id` 从同一段摘要派生；
- 追加是真写侧干的事：`material_unit` / `material_observation` /
  `material_source_reference` / `observation` / `event_outbox` 的行数与报告逐一对上，
  且 `contract_bytes` 与磁盘上的素材 protobuf **逐字节相同**；
- 幂等来自写侧：第二遍追加必须 `appended=0 / replayed=<全部>`，库里的 revision 不增，
  outbox 不增；第二遍 relay 也必须 `published=0`；
- 发布这一跳是真的：relay 状态行的 `published` 等于 outbox 行数，且
  `event_outbox.published_at` 全部落定，`Nats-Msg-Id` 去重由 JetStream 吸收；
- 发布侧背压准入被**显式注入**一档（`medium`），状态行必须是 `admitted` 并带上真实上限——
  "注入后通过"与"没注入所以没判"是两种结论；
- 失败必须显式（每次都用真实写侧/真实入口，不 mock）：owner 漂移 ⇒
  `media_source_conflict`；同一 revision 换内容 ⇒ `immutable_revision_conflict`；
  报告视图被改过 ⇒ `material_report_mismatch`；
- 不外泄：三份产物里没有 DSN、主机路径、素材文本、令牌或向量库引用；
- `golden_path_verified` 在三份产物里都必须是 `false`：向量与检索发生在别处。

刻意不做的事（未验证边界，见 ADR-028 §9）：不验 relay → 常驻消费 → 向量 → 语义检索
这一段（素材被事件驱动写成向量、再被检索到，由 `consume-check` / `event-pipeline-check`
覆盖，而它们跑的不是 timeline 融合出来的素材）；不验 SRT 实时源；不验同一窗口内多模态
共存（本验收只有 VLM 一种模态）；不验 revision 前进（那需要一个先读事实的调用方）；
不验内容切窗（栅格是固定的）。
"""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import psycopg  # noqa: E402
from edge_material_sdk.generated.material.v1 import material_pb2 as material  # noqa: E402
from edge_material_sdk.generated.media.v1 import media_pb2 as media  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from sensoryplex_relay import residency  # noqa: E402

from tools.macos_resident import TIERS  # noqa: E402
from tools.migrate import migrate  # noqa: E402
from tools.timeline_handoff import BLOCKERS as HANDOFF_BLOCKERS  # noqa: E402
from tools.verify_index import derive_database_url, describe_target  # noqa: E402
from tools.verify_index_consume import isolated_scope, jetstream  # noqa: E402
from tools.verify_model import Process, check, free_port  # noqa: E402

PIPELINE = ROOT / "config/pipelines/file-material.yaml"
WORKER = ROOT / "tools/ai_worker.py"
HANDOFF = ROOT / "tools/timeline_handoff.py"
PLUGIN_MODULE = "edge_material_plugin_vlm_moondream"
RELAY_MODULE = "sensoryplex_relay.cli"

# 观测帧数刻意有界：本验收验的是"链路怎么把已观测的事实变成素材"，不是"跑满整段素材"。
# 6 帧落在 4 s 间隔上，因此会跨多个窗口（窗口 5 s），窗口归属这件事才真的被走到。
MAX_FRAMES = 6
READY_TIMEOUT_S = 300.0
RUN_TIMEOUT_S = 900.0
SERVER_TTL_MS = 30_000
# 数据面按"最后一次调用"起算空闲；模型推理期间是没有任何 gRPC 调用的，
# 所以这个窗口必须**大于单帧最长推理时间**（实测 VLM 首帧 ~3 s），否则生产者会在观
# 测途中关掉数据面，失败会伪装成 `data_plane_stats_failed`。它同时决定收尾等待时长。
IDLE_TIMEOUT_MS = 20_000
WAIT_TIMEOUT_MS = 600_000
RETAINED_LIMIT = 32
ARENA_BYTES = 256 * 1024 * 1024

OWNER = "timeline-handoff-acceptance"
TRACE_ID = "timeline-acceptance"
RELAY_TIER = next(tier for tier in TIERS if tier.name == "medium")
RELAY_BATCH = 16

# 与 `tools/verify_index_consume.py` 同一套"不外泄"针脚。
LEAK_NEEDLES = ("postgresql://", "/Users/", "password", "Bearer", "milvus://", "secret")


def runtime_binary() -> pathlib.Path:
    for profile in ("release", "debug"):
        candidate = ROOT / f"target/{profile}/sensoryplex-runtime"
        if candidate.is_file():
            return candidate
    raise SystemExit("build the runtime first: make timeline-check builds it for you")


def relay_environ() -> dict[str, str]:
    """给 relay 子进程显式注入一档，让"准入通过"是一条真结论而不是空过。

    摘掉再注入（不是"随宿主环境"）：开发者终端里若加载过 `resident.env`，同一份验收会
    因为外面那个 shell 而给出不同结论。
    """
    environ = dict(os.environ)
    environ.pop(residency.TIER_VAR, None)
    environ.pop(residency.CAPACITY_VAR, None)
    environ[residency.TIER_VAR] = RELAY_TIER.name
    environ[residency.CAPACITY_VAR] = str(RELAY_TIER.event_queue_capacity)
    return environ


def probe_identity(report_path: pathlib.Path) -> dict:
    """原片身份只从 Runtime 的 replay 报告里读：本脚本不自己算摘要、也不猜 source_id。"""
    document = media.ReplayReport()
    document.ParseFromString(report_path.read_bytes())
    source = document.source
    return {
        "stream_id": source.source.stream_id,
        "source_id": source.source.source_id,
        "content_hash": source.source.content_hash,
        "duration_ms": source.duration_ms,
        "codecs": {track.track_kind: track.codec for track in source.tracks},
    }


def replay_pass(
    media_path: pathlib.Path,
    workspace: pathlib.Path,
    failures: list,
    *,
    plugin_module: str,
    report_name: str,
    worker_report_name: str,
    plugin_config: dict | None = None,
    label: str = "vlm",
    max_frames: int = MAX_FRAMES,
    allow_frame_failures: bool = False,
    exact_frame_count: bool = True,
) -> dict:
    """一遍真实回放：Runtime 生产者（真解码 + 描述符账本）→ 插件消费者 → worker。

    一次回放**只服务一个插件**：数据面的保留表按 lease 发缓冲区，同一条 buffer 同一时刻只能
    被一个消费者持有（`buffer_already_leased`），所以多模态是"同一批描述符窗口跑多遍"，不是
    "多个消费者抢同一条 buffer"。两遍得到的窗口由解码本身决定，逐点相同。

    `allow_frame_failures`：调用失败（`error` 行，没有描述符窗口）是**真实媒体路径**上的常态
    （模型端点偶发 5xx 就会走到），默认仍按失败处理；调用方显式打开时会把这些行单独计数并
    放进 `_failed_frames`，让"几帧没跑成"成为可读的事实，而不是被混进窗口校验里。
    """
    data_plane = f"127.0.0.1:{free_port()}"
    plugin_address = f"127.0.0.1:{free_port()}"
    replay_report = workspace / report_name
    worker_report = workspace / worker_report_name
    config_path = workspace / f"{worker_report_name}.plugin-config.json"

    plugin_command = [sys.executable, "-m", plugin_module, "--port", plugin_address.split(":")[1]]
    if plugin_config is not None:
        # 配置是**输入的一部分**：它进插件身份（`config_hash`），所以必须显式、稳定，
        # 不能让"这次恰好没传"变成另一份模型身份。
        config_path.write_text(
            json.dumps(plugin_config, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )

    producer = Process(
        "runtime",
        [
            str(runtime_binary()),
            "replay",
            str(PIPELINE),
            str(media_path),
            "--report",
            str(replay_report),
            "--handoff-listen",
            data_plane,
            "--handoff-retained-limit",
            str(RETAINED_LIMIT),
            "--handoff-arena-bytes",
            str(ARENA_BYTES),
            "--handoff-ttl-ms",
            str(SERVER_TTL_MS),
            "--handoff-wait-timeout-ms",
            str(WAIT_TIMEOUT_MS),
            "--handoff-idle-timeout-ms",
            str(IDLE_TIMEOUT_MS),
        ],
    )
    plugin = Process(label, plugin_command)
    producer.start()
    try:
        ready = producer.wait_for("handoff_ready", timeout=READY_TIMEOUT_S)
        check(
            ready.get("listen") == data_plane,
            f"[{label}] unexpected data plane: {ready}",
            failures,
        )
        check(int(ready.get("retained", "0")) > 0, f"[{label}] no frame was retained", failures)

        identity = probe_identity(replay_report)
        print(
            f"[{label}] probe: stream={identity['stream_id']} source={identity['source_id']} "
            f"duration_ms={identity['duration_ms']} codecs={identity['codecs']}"
        )

        plugin.start()
        plugin.wait_for("plugin ready", timeout=120.0)

        worker_command = [
            sys.executable,
            str(WORKER),
            "--data-plane",
            data_plane,
            "--plugin",
            plugin_address,
            "--source-id",
            identity["source_id"],
            "--max-frames",
            str(max_frames),
            "--report",
            str(worker_report),
        ]
        if plugin_config is not None:
            worker_command += ["--plugin-config", str(config_path)]
        completed = subprocess.run(
            worker_command,
            capture_output=True,
            text=True,
            check=False,
            timeout=RUN_TIMEOUT_S,
        )
        check(
            completed.returncode == 0,
            f"[{label}] ai worker failed ({completed.returncode}): {completed.stderr[-2000:]}",
            failures,
        )
    finally:
        plugin.kill()
        try:
            producer.finish(timeout=RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            producer.kill()
            failures.append(f"[{label}] runtime replay never returned after the consumer left")

    document = json.loads(worker_report.read_text()) if worker_report.is_file() else {}
    check(
        document.get("input_mode") == "buffer",
        f"[{label}] worker ran in {document.get('input_mode')!r}",
        failures,
    )
    check(
        document.get("failures") == [],
        f"[{label}] worker failures: {document.get('failures')}",
        failures,
    )
    frames = document.get("frames", [])
    if exact_frame_count:
        check(
            len(frames) == max_frames,
            f"[{label}] worker observed {len(frames)} of {max_frames} frames; "
            f"producer tail={producer.lines[-3:]}",
            failures,
        )
    else:
        check(
            len(frames) > 0,
            f"[{label}] worker observed no frames; producer tail={producer.lines[-3:]}",
            failures,
        )
    failed = [frame for frame in frames if "error" in frame]
    processed = [frame for frame in frames if "error" not in frame]
    if failed and not allow_frame_failures:
        failures.append(
            f"[{label}] {len(failed)} frame(s) never reached the model: "
            f"{[frame.get('error', {}).get('reason') for frame in failed]}"
        )
    check(
        all(frame.get("source_time_range_ms") for frame in processed),
        f"[{label}] a frame carried no Runtime-issued descriptor window",
        failures,
    )
    check(
        all(frame.get("source_time_range_ms") == frame.get("time_range_ms") for frame in processed),
        f"[{label}] an observation was re-anchored away from its descriptor window",
        failures,
    )
    print(
        f"[{label}] observations={len(document.get('observations', []))} "
        f"frames={len(frames)} failed_frames={len(failed)}"
    )
    document["_replay_report"] = str(replay_report)
    document["_worker_report"] = str(worker_report)
    document["_failed_frames"] = [
        (frame.get("error") or {}).get("reason", "unknown") for frame in failed
    ]
    return document


def fuse_timeline(
    media_path: pathlib.Path,
    workspace: pathlib.Path,
    replay_report: pathlib.Path,
    worker_report: pathlib.Path,
    failures: list,
    *,
    material_dir_name: str = "materials",
    report_name: str = "timeline.json",
) -> dict:
    """真融合：按 pipeline 栅格选窗 → 逐条准入 → 写素材 protobuf 与报告。"""
    material_dir = workspace / material_dir_name
    timeline_report = workspace / report_name
    timeline = subprocess.run(
        [
            str(runtime_binary()),
            "timeline",
            str(PIPELINE),
            str(media_path),
            "--report",
            str(replay_report),
            "--worker-report",
            str(worker_report),
            "--material-dir",
            str(material_dir),
            "--out",
            str(timeline_report),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=RUN_TIMEOUT_S,
    )
    check(
        timeline.returncode == 0,
        f"timeline fusion failed ({timeline.returncode}): "
        f"{timeline.stdout[-500:]} {timeline.stderr[-500:]}",
        failures,
    )
    if not timeline_report.is_file():
        return {}
    document = json.loads(timeline_report.read_text())
    document["_workspace"] = {
        "materials": str(material_dir),
        "replay_report": str(replay_report),
        "report": str(timeline_report),
    }
    print(f"timeline: {timeline.stdout.strip().splitlines()[0] if timeline.stdout else ''}")
    return document


def produce_timeline(media_path: pathlib.Path, workspace: pathlib.Path, failures: list) -> dict:
    """步骤 1–5：真解码 → 真 VLM 观测 → 真融合，返回 timeline 报告。"""
    worker = replay_pass(
        media_path,
        workspace,
        failures,
        plugin_module=PLUGIN_MODULE,
        report_name="replay.pb",
        worker_report_name="ai-worker.json",
    )
    return fuse_timeline(
        media_path,
        workspace,
        pathlib.Path(worker["_replay_report"]),
        pathlib.Path(worker["_worker_report"]),
        failures,
    )


def run_handoff(
    report_path: pathlib.Path,
    material_dir: pathlib.Path,
    database_url: str,
    *,
    owner: str = OWNER,
    out: pathlib.Path | None = None,
    trace_id: str = TRACE_ID,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(HANDOFF),
        "--report",
        str(report_path),
        "--material-dir",
        str(material_dir),
        "--owner",
        owner,
        "--trace-id",
        trace_id,
        "--database-url",
        database_url,
    ]
    if out is not None:
        command += ["--out", str(out)]
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=RUN_TIMEOUT_S
    )


def run_relay(
    database_url: str, nats_url: str, stream: str, prefix: str
) -> tuple[int, list[dict], str]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            RELAY_MODULE,
            "--database-url",
            database_url,
            "--nats-url",
            nats_url,
            "--stream",
            stream,
            "--subject-prefix",
            prefix,
            "--batch",
            str(RELAY_BATCH),
            "--once",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
        timeout=RUN_TIMEOUT_S,
        env=relay_environ(),
    )
    documents: list[dict] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            documents.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return completed.returncode, documents, completed.stderr[-500:]


def relay_status(documents: list[dict]) -> dict:
    for document in documents:
        if document.get("event") == "relay.status":
            return document
    return {}


def verify_media_chain(
    document: dict,
    failures: list,
    *,
    items_expected: int = MAX_FRAMES,
    frames_without_window_expected: int = 0,
) -> dict:
    """素材与报告之间的一致性（不碰数据库）：身份派生、栅格、原片引用。"""
    if not document:
        failures.append("timeline produced no report")
        return {}
    source = document["source"]
    counters = document["counters"]
    policy = document["pipeline"]
    window_ms = policy["window_ms"]
    short = source["content_hash"][7:19]

    check(
        document["golden_path_verified"] is False,
        "the timeline report claims a verified golden path",
        failures,
    )
    check(
        set(document["blockers"])
        == {
            "metadata_append_not_exercised",
            "vector_index_not_exercised",
            "semantic_search_not_exercised",
            "golden_path_not_verified",
        },
        f"the timeline report hides what it did not do: {document['blockers']}",
        failures,
    )
    check(counters["materials"] > 0, "no material was materialized", failures)
    check(
        counters["observations_rejected"] == 0,
        f"{counters['observations_rejected']} observations were rejected: {document['rejected']}",
        failures,
    )
    check(
        counters["materials"] == counters["windows_with_observations"],
        "the material count does not match the number of populated windows",
        failures,
    )
    check(
        document["run"]["worker_frames_without_descriptor_window"]
        == frames_without_window_expected,
        f"{document['run']['worker_frames_without_descriptor_window']} frame(s) reached the "
        f"ledger without a descriptor window (expected {frames_without_window_expected})",
        failures,
    )
    check(
        document["run"]["replay_golden_path_verified"] is False,
        "the replay report claims a verified golden path",
        failures,
    )

    asset_id = f"asset-{short}"
    check(
        source["asset_id"] == asset_id,
        f"asset id is not digest-derived: {source['asset_id']}",
        failures,
    )
    check(
        len(document["items"]) == items_expected,
        f"{len(document['items'])} ledger items (expected {items_expected})",
        failures,
    )

    seen = set()
    for entry in document["materials"]:
        identifier = entry["material_unit_id"]
        seen.add(identifier)
        start_ms, end_ms = entry["start_ms"], entry["end_ms"]
        check(
            identifier == f"material-{short}-{start_ms}",
            f"material id is not derived from the digest and window start: {identifier}",
            failures,
        )
        check(start_ms % window_ms == 0, f"{identifier} does not start on the grid", failures)
        check(
            end_ms == min(start_ms + window_ms, source["duration_ms"]),
            f"{identifier} is not clamped to the media duration",
            failures,
        )
        check(entry["revision"] == 1, f"{identifier} is not at revision 1", failures)
        check(entry["status"] != "failed", f"{identifier} is failed", failures)
        missing = entry["fusion"]["missing_required_modalities"]
        check(
            set(missing).issubset(set(entry["pending_enrichments"])),
            f"{identifier} disagrees about what is still missing",
            failures,
        )
        # readiness 必须如实：缺必需模态就只能读成 partial，不是"融合成功所以 ready"。
        if missing:
            check(
                entry["status"] == "partial",
                f"{identifier} claims {entry['status']} while missing {missing}",
                failures,
            )
        else:
            check(
                entry["status"] in ("fast_ready", "enriched"),
                f"{identifier} is not ready though nothing is missing: {entry['status']}",
                failures,
            )
        on_disk = pathlib.Path(document["_workspace"]["materials"]) / entry["material_file"]
        check(on_disk.is_file(), f"{identifier} has no material file on disk", failures)
        if on_disk.is_file():
            digest = "sha256:" + hashlib.sha256(on_disk.read_bytes()).hexdigest()
            check(
                entry["material_digest"] == digest,
                f"{identifier}: the reported digest is not the bytes on disk",
                failures,
            )
        for reference in entry["source_refs"]:
            check(
                reference["asset_id"] == asset_id,
                f"{identifier} points at another asset: {reference['asset_id']}",
                failures,
            )
            check(
                reference["content_hash"] == source["content_hash"],
                f"{identifier} references the frame digest instead of the whole file",
                failures,
            )
            check(
                start_ms <= reference["start_ms"] and reference["end_ms"] <= end_ms,
                f"{identifier} references bytes outside its own window",
                failures,
            )
    check(
        len(seen) == counters["materials"],
        "the material list has duplicates",
        failures,
    )
    return document["_workspace"]


def verify_database(isolated: str, document: dict, failures: list) -> dict:
    """库里的行必须与报告逐项对上；素材字节必须与磁盘上的 protobuf 逐字节相同。"""
    counters = document["counters"]
    materials = {entry["material_unit_id"]: entry for entry in document["materials"]}
    material_dir = pathlib.Path(document["_workspace"]["materials"])
    facts = {}
    with psycopg.connect(isolated) as conn:
        facts["material_unit"] = conn.execute("SELECT count(*) FROM material_unit").fetchone()[0]
        facts["observation"] = conn.execute("SELECT count(*) FROM observation").fetchone()[0]
        facts["links"] = conn.execute("SELECT count(*) FROM material_observation").fetchone()[0]
        facts["references"] = conn.execute(
            "SELECT count(*) FROM material_source_reference"
        ).fetchone()[0]
        facts["timeline_item"] = conn.execute("SELECT count(*) FROM timeline_item").fetchone()[0]
        facts["outbox"] = conn.execute("SELECT count(*) FROM event_outbox").fetchone()[0]
        facts["published"] = conn.execute(
            "SELECT count(*) FROM event_outbox WHERE published_at IS NOT NULL"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT material_unit_id, revision, status, start_ms, end_ms, search_text, "
            "content_hash, contract_bytes FROM material_unit ORDER BY material_unit_id"
        ).fetchall()
        asset = conn.execute("SELECT sha256, duration_ms, codec FROM media_asset").fetchone()

    check(
        facts["material_unit"] == counters["materials"],
        f"material_unit has {facts['material_unit']} rows for {counters['materials']} materials",
        failures,
    )
    check(
        facts["observation"] == counters["observations_accepted"],
        f"observation has {facts['observation']} rows for "
        f"{counters['observations_accepted']} accepted observations",
        failures,
    )
    check(facts["links"] == facts["observation"], "an observation lost its material link", failures)
    check(
        facts["references"] == counters["observations_accepted"],
        f"material_source_reference has {facts['references']} rows",
        failures,
    )
    check(
        facts["timeline_item"] == len(document["items"]),
        f"timeline_item has {facts['timeline_item']} rows for {len(document['items'])} items",
        failures,
    )
    check(facts["outbox"] == counters["materials"], "an append wrote no outbox row", failures)
    check(
        asset is not None and asset[0] == document["source"]["content_hash"],
        f"media_asset does not carry the whole-file digest: {asset}",
        failures,
    )

    for (
        identifier,
        revision,
        status,
        start_ms,
        end_ms,
        search_text,
        content_hash,
        contract_bytes,
    ) in rows:
        entry = materials.get(identifier)
        if entry is None:
            failures.append(f"{identifier} is in the database but not in the report")
            continue
        check(
            (status, start_ms, end_ms) == (entry["status"], entry["start_ms"], entry["end_ms"]),
            f"{identifier} differs between the database and the report",
            failures,
        )
        check(bool(search_text.strip()), f"{identifier} has an empty search_text", failures)
        check(bool(content_hash), f"{identifier} has no content hash", failures)
        on_disk = (material_dir / entry["material_file"]).read_bytes()
        check(
            contract_bytes == on_disk,
            f"{identifier}: stored bytes differ from the fused material on disk",
            failures,
        )
        # 回查必须给出**同一份**事实：读侧水合的是版本化 protobuf。
        decoded = material.MaterialUnit()
        decoded.ParseFromString(contract_bytes)
        check(decoded.revision == revision, f"{identifier} stored revision drift", failures)
        check(
            "sha256:" + hashlib.sha256(contract_bytes).hexdigest() == content_hash,
            f"{identifier} content hash does not cover the stored bytes",
            failures,
        )
    return facts


def verify_no_leak(documents: dict[str, dict], failures: list) -> None:
    for label, document in documents.items():
        payload = json.dumps(document, ensure_ascii=False)
        for needle in LEAK_NEEDLES:
            check(needle not in payload, f"{label} leaks {needle!r}", failures)


def verify(
    media: pathlib.Path, database_url: str, nats_url: str, workspace: pathlib.Path
) -> list[str]:
    failures: list[str] = []
    schema = "timeline_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(database_url, autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(database_url, options=f"-c search_path={schema},public")
    migrate(isolated)
    stream, prefix = isolated_scope("timeline")
    jetstream(nats_url, "delete", stream)
    try:
        document = produce_timeline(media, workspace, failures)
        verify_media_chain(document, failures)
        if not document:
            return failures

        material_dir = pathlib.Path(document["_workspace"]["materials"])
        report_path = pathlib.Path(document["_workspace"]["report"])

        first = run_handoff(report_path, material_dir, isolated, out=workspace / "handoff-1.json")
        check(
            first.returncode == 0,
            f"handoff failed ({first.returncode}): {first.stdout[-500:]}",
            failures,
        )
        if first.returncode != 0:
            return failures
        handoff = json.loads((workspace / "handoff-1.json").read_text())
        check(
            handoff["counters"]["appended"] == document["counters"]["materials"],
            f"the first append added {handoff['counters']['appended']} materials",
            failures,
        )
        check(handoff["counters"]["replayed"] == 0, "the first append reported a replay", failures)
        check(
            handoff["owner"] == OWNER, f"the append was recorded under {handoff['owner']}", failures
        )
        check(
            set(handoff["blockers"]) == set(HANDOFF_BLOCKERS),
            f"the handoff hides what it did not do: {handoff['blockers']}",
            failures,
        )
        check(
            handoff["golden_path_verified"] is False,
            "the handoff report claims a verified golden path",
            failures,
        )

        facts = verify_database(isolated, document, failures)

        code, documents, stderr = run_relay(isolated, nats_url, stream, prefix)
        check(code == 0, f"relay exited {code}: {stderr}", failures)
        status = relay_status(documents)
        check(bool(status), f"relay printed no status line: {documents}", failures)
        check(
            status.get("published") == facts.get("outbox"),
            f"relay published {status.get('published')} of {facts.get('outbox')} events",
            failures,
        )
        check(
            status.get("inflight_state") == "admitted",
            f"the publish hop was not admitted: {status.get('inflight_state')}",
            failures,
        )
        check(
            int(status.get("inflight_capacity") or 0) == RELAY_TIER.event_queue_capacity,
            f"the publish hop reported capacity {status.get('inflight_capacity')}",
            failures,
        )
        with psycopg.connect(isolated) as conn:
            published = conn.execute(
                "SELECT count(*) FROM event_outbox WHERE published_at IS NOT NULL"
            ).fetchone()[0]
        check(
            published == facts.get("outbox"),
            f"only {published} outbox rows were marked published",
            failures,
        )

        # 重放：同一批事实第二遍必须什么都不新增（写侧判官 + outbox 不动 + relay 无事件可发）。
        second = run_handoff(report_path, material_dir, isolated, out=workspace / "handoff-2.json")
        check(second.returncode == 0, f"the replay append failed ({second.returncode})", failures)
        replay = (
            json.loads((workspace / "handoff-2.json").read_text()) if second.returncode == 0 else {}
        )
        check(
            replay.get("counters", {}).get("appended") == 0,
            "the replay appended materials",
            failures,
        )
        check(
            replay.get("counters", {}).get("replayed") == document["counters"]["materials"],
            "the replay did not report every material as replayed",
            failures,
        )
        with psycopg.connect(isolated) as conn:
            rows = conn.execute(
                "SELECT (SELECT count(*) FROM material_unit), "
                "(SELECT count(*) FROM event_outbox), "
                "(SELECT count(*) FROM observation)"
            ).fetchone()
        check(
            rows == (facts["material_unit"], facts["outbox"], facts["observation"]),
            f"the replay changed the fact set: {rows}",
            failures,
        )
        code, documents, stderr = run_relay(isolated, nats_url, stream, prefix)
        check(code == 0, f"the second relay exited {code}: {stderr}", failures)
        status = relay_status(documents)
        check(
            status.get("published") == 0 and status.get("claimed") == 0,
            f"the second relay republished events: {status}",
            failures,
        )

        verify_failures(isolated, report_path, material_dir, workspace, failures)
        documents = {
            "timeline": {key: value for key, value in document.items() if key != "_workspace"},
            "handoff": handoff,
            "relay": status,
        }
        verify_no_leak(documents, failures)
    finally:
        jetstream(nats_url, "delete", stream)
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
    return failures


def verify_failures(
    isolated: str,
    report_path: pathlib.Path,
    material_dir: pathlib.Path,
    workspace: pathlib.Path,
    failures: list,
) -> None:
    """三类显式失败：每一次都打真实入口，不做 mock。"""

    def outcome(label: str, result: subprocess.CompletedProcess, needle: str) -> None:
        combined = result.stdout + result.stderr
        check(result.returncode != 0, f"{label}: the entry point reported success", failures)
        check(
            needle in combined, f"{label}: expected {needle!r}, got {combined[-300:]!r}", failures
        )
        print(f"  {label}: {combined.strip().splitlines()[-1][:150]}")

    # 1) owner 漂移：同一个 source 换一个授权主体，不能静默沿用旧行。
    outcome(
        "owner drift",
        run_handoff(report_path, material_dir, isolated, owner=OWNER + "-intruder"),
        "media_source_conflict",
    )

    # 2) 同一 revision 换内容：不可变事实必须拒绝，而不是覆盖。
    tampered = workspace / "tampered"
    tampered.mkdir(exist_ok=True)
    targets = sorted(material_dir.glob("*.material.pb"))
    check(bool(targets), "no material file to tamper with", failures)
    for path in targets:
        (tampered / path.name).write_bytes(path.read_bytes())
    target = targets[0]
    unit = material.MaterialUnit()
    unit.ParseFromString(target.read_bytes())
    # 只改 payload，不动 identity：这样撞上的正是"同一 revision 内容变了"。
    unit.observations[0].payload["text"] = "tampered-payload"
    (tampered / target.name).write_bytes(unit.SerializeToString(deterministic=True))
    outcome(
        "immutable revision",
        run_handoff(report_path, tampered, isolated),
        "immutable_revision_conflict",
    )

    # 3) 报告视图被改过：写侧的意见不能来自 JSON，只能来自素材本体。
    edited = workspace / "timeline-edited.json"
    document = json.loads(report_path.read_text())
    document["materials"][0]["revision"] = 7
    edited.write_text(json.dumps(document, ensure_ascii=False))
    outcome(
        "tampered report",
        run_handoff(edited, material_dir, isolated),
        "material_report_mismatch",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", type=pathlib.Path, required=True)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--nats-url", default="nats://127.0.0.1:24222")
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()

    media = arguments.media.expanduser()
    if not media.is_file():
        raise SystemExit(f"authorized sample not found: {media}")
    database_url, origin = derive_database_url(arguments.database_url)
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-timeline-"))
    print(f"media: {media.name}")
    print(f"database: {describe_target(database_url)} (from {origin})")
    print(f"nats: {arguments.nats_url}")
    print(f"workspace: {workspace}")
    started = time.monotonic()
    try:
        failures = verify(media, database_url, arguments.nats_url, workspace)
    except Exception as error:  # noqa: BLE001 - 起不来就是验收失败，不跳过
        failures = [f"{type(error).__name__}: {str(error)[:300]}"]
    if failures:
        print("\ntimeline handoff acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        "timeline handoff acceptance: real media -> replay -> VLM -> fusion -> authorized append "
        f"-> outbox -> JetStream passed in {round(time.monotonic() - started, 1)}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

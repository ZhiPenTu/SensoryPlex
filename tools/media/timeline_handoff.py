"""Runtime → Timeline 的**授权追加**入口（ADR-028 §5）。

本脚本是整条链路里唯一碰数据库的一环，并且只做两件事：

1. 按 Runtime 报告登记**引用事实**（`media_source` / `stream_session` / `media_asset` /
   `timeline_item` / `model_release`）——它们的值全部取自 Runtime 的真实探测与真实报告，
   本脚本不生成、也不猜测任何身份；
2. 用**真实写侧** `sensoryplex_api.infrastructure.materials.append_material()` 逐条追加
   素材（事实 + lineage + outbox 行在同一个事务里落下）。

为什么不在 Rust 里写库：`crates/storage` 的 `MetadataStore` 端口仍然没有 adapter，
Runtime 进程没有数据库依赖。追加必须由一个**持有授权**的调用方显式发起，所以它是
一个参数显式（`--owner` / `--trace-id`）的入口，而不是运行时的隐式副作用。

三条不得含糊的语义：

- 幂等来自写侧，不来自本脚本：同一份素材重放时 `append_material` 返回 `False`
  （内容摘要一致 ⇒ 未新增），本脚本如实记账为 `replayed`，绝不谎报为 `appended`；
- `revision` 冲突（`immutable_revision_conflict` / `non_sequential_revision`）原样上抛，
  不做"自动 +1"或"覆盖"这类会改写历史的兜底；
- 引用事实若与库里已有的行冲突（owner / 摘要 / 时长 / 类型不一致），显式失败，
  不静默沿用旧行——"看起来跑通了但指向另一个 owner 的素材"是最坏的失败形态。
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import psycopg  # noqa: E402
from edge_material_sdk.generated.material.v1 import material_pb2 as material  # noqa: E402
from edge_material_sdk.generated.media.v1 import media_pb2 as media  # noqa: E402
from sensoryplex_api.infrastructure import materials as api_materials  # noqa: E402

from tools.verify_index import derive_database_url, describe_target  # noqa: E402
from tools.verify_replay import describe_track  # noqa: E402

# `media_source.uri_redacted` / `media_asset.object_uri`：只登记"不可外泄"这个事实本身，
# 原片路径与访问凭据都不进数据库（AGENTS.md：不把密钥与原始路径放进控制面）。
REDACTED_URI = "private://not-exposed"

# 本命令**没有**做的事，逐条写进产物：追加完成不等于"能被语义检索到"。
BLOCKERS = [
    "revision_advance_not_exercised",
    # outbox 行已在这个事务里落下，但本入口不发事件；"发到总线"是 relay 那一跳的事。
    "relay_publish_not_exercised",
    "vector_index_not_exercised",
    "semantic_search_not_exercised",
    "golden_path_not_verified",
]

# `MediaSourceKind` 枚举 → `media_source.type` 的取值域。
SOURCE_TYPES = {
    media.MEDIA_SOURCE_KIND_FILE: "file",
    media.MEDIA_SOURCE_KIND_SRT: "srt",
}


class HandoffError(RuntimeError):
    """带着稳定原因串的失败：调用方据此判断"哪一类不一致"，而不是解析自由文本。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason


def load_report(path: pathlib.Path) -> dict:
    if not path.is_file():
        raise HandoffError("timeline_report_unreadable", str(path))
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise HandoffError("timeline_report_unparsable", str(error)) from None
    for key in ("source", "items", "materials", "run"):
        if key not in document:
            raise HandoffError("timeline_report_incomplete", f"missing {key}")
    return document


def load_source_description(report: dict) -> media.MediaSourceDescription:
    """从那一轮的 replay 报告里取**真实探测**到的轨道，用来登记 `media_asset.codec`。

    这一段不读取任何用户输入：codec 描述如果由调用方手填，就等于让"素材指向哪个编码"
    变成一句话，而不是一条探测事实。
    """
    path = report["run"].get("replay_report", "")
    if not path or not pathlib.Path(path).is_file():
        raise HandoffError("replay_report_unreadable", str(path))
    document = media.ReplayReport()
    document.ParseFromString(pathlib.Path(path).read_bytes())
    if not document.HasField("source"):
        raise HandoffError("replay_report_without_source")
    return document.source


def load_materials(directory: pathlib.Path, report: dict) -> list:
    """素材以**磁盘上的 protobuf** 为准，报告里的 `materials[]` 只用来对账。

    反过来（信任 JSON 视图去写库）会让"报告被改过"变成"事实被改过"。
    """
    declared = {entry["material_unit_id"]: entry for entry in report["materials"]}
    found = {}
    for path in sorted(directory.glob("*.material.pb")):
        unit = material.MaterialUnit()
        unit.ParseFromString(path.read_bytes())
        if not unit.material_unit_id:
            raise HandoffError("material_file_without_identity", path.name)
        if unit.material_unit_id in found:
            raise HandoffError("material_file_duplicated", unit.material_unit_id)
        found[unit.material_unit_id] = unit
    if set(found) != set(declared):
        raise HandoffError(
            "material_report_mismatch",
            f"on_disk={sorted(found)} report={sorted(declared)}",
        )
    for identifier, unit in found.items():
        entry = declared[identifier]
        if unit.revision != entry["revision"] or unit.status != entry["status"]:
            raise HandoffError("material_report_mismatch", identifier)
        if unit.stream_id != report["source"]["stream_id"]:
            raise HandoffError("material_stream_mismatch", identifier)
    return [found[identifier] for identifier in sorted(found)]


def _require_identical(existing, expected, reason, detail, failures) -> None:
    """库里已有行必须与本次推导**逐字段一致**；不一致是失败，不是"沿用旧行"。"""
    if existing is not None and existing != expected:
        failures.append(f"{reason}: {detail} existing={existing} expected={expected}")


def register_references(conn, report, description, owner, units, upload_id: str = "") -> dict:
    """登记素材引用的**全部**外部身份，并在冲突时显式失败。"""
    source = report["source"]
    source_type = SOURCE_TYPES.get(description.source.kind)
    if source_type is None:
        raise HandoffError("unsupported_source_kind", str(description.source.kind))
    tracks = [describe_track(track) for track in description.tracks]
    if not tracks:
        raise HandoffError("probe_reported_no_tracks")
    codec = ",".join(tracks)
    # 素材时间轴的起点：取观测里最早的事实时间，**不是**当前时间。重放必须逐字节相同。
    started_at = datetime.datetime.fromtimestamp(
        min(unit.created_at_unix_ms for unit in units) / 1000, tz=datetime.UTC
    )

    failures = []
    existing = conn.execute(
        "SELECT type, uri_redacted, owner FROM media_source WHERE source_id=%s",
        (source["source_id"],),
    ).fetchone()
    _require_identical(
        existing,
        (source_type, REDACTED_URI, owner),
        "media_source_conflict",
        source["source_id"],
        failures,
    )
    conn.execute(
        "INSERT INTO media_source(source_id,type,uri_redacted,owner) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (source_id) DO NOTHING",
        (source["source_id"], source_type, REDACTED_URI, owner),
    )

    existing = conn.execute(
        "SELECT source_id FROM stream_session WHERE stream_id=%s", (source["stream_id"],)
    ).fetchone()
    _require_identical(
        existing,
        (source["source_id"],),
        "stream_session_conflict",
        source["stream_id"],
        failures,
    )
    conn.execute(
        "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
        "VALUES (%s,%s,%s,'stopped') ON CONFLICT (stream_id) DO NOTHING",
        (source["stream_id"], source["source_id"], started_at),
    )

    existing = conn.execute(
        "SELECT stream_id, sha256, codec, duration_ms FROM media_asset WHERE asset_id=%s",
        (source["asset_id"],),
    ).fetchone()
    _require_identical(
        existing,
        (source["stream_id"], source["content_hash"], codec, source["duration_ms"]),
        "media_asset_conflict",
        source["asset_id"],
        failures,
    )
    conn.execute(
        (
            "INSERT INTO media_asset(asset_id,stream_id,object_uri,sha256,codec,duration_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (asset_id) DO UPDATE SET object_uri=EXCLUDED.object_uri"
        ),
        (
            source["asset_id"],
            source["stream_id"],
            f"upload://{upload_id}" if upload_id else REDACTED_URI,
            source["content_hash"],
            codec,
            source["duration_ms"],
        ),
    )

    for item in report["items"]:
        existing = conn.execute(
            "SELECT stream_id, kind, start_ms, end_ms FROM timeline_item WHERE item_id=%s",
            (item["item_id"],),
        ).fetchone()
        _require_identical(
            existing,
            (source["stream_id"], item["kind"], item["start_ms"], item["end_ms"]),
            "timeline_item_conflict",
            item["item_id"],
            failures,
        )
        conn.execute(
            "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (item_id) DO NOTHING",
            (
                item["item_id"],
                source["stream_id"],
                item["kind"],
                item["start_ms"],
                item["end_ms"],
            ),
        )

    releases = {}
    for unit in units:
        for observation in unit.observations:
            provenance = observation.provenance
            releases[provenance.model_release_id] = (
                provenance.model_id,
                provenance.model_version,
                provenance.model_artifact_digest,
                provenance.execution_backend,
                provenance.config_hash,
            )
    for release_id, expected in sorted(releases.items()):
        existing = conn.execute(
            "SELECT name, version, artifact_hash, backend, config_hash FROM model_release "
            "WHERE model_release_id=%s",
            (release_id,),
        ).fetchone()
        _require_identical(existing, expected, "model_release_conflict", release_id, failures)
        conn.execute(
            "INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,"
            "config_hash) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (model_release_id) DO NOTHING",
            (release_id, *expected),
        )

    if failures:
        raise HandoffError("reference_fact_conflict", "; ".join(failures))
    return {
        "media_sources": 1,
        "stream_sessions": 1,
        "media_assets": 1,
        "timeline_items": len(report["items"]),
        "model_releases": len(releases),
    }


def handoff(
    *,
    report_path,
    material_dir,
    owner,
    trace_id,
    database_url,
    upload_id: str = "",
    execution_id: str = "",
) -> dict:
    """登记引用事实 + 追加素材；返回可直接序列化的产物。"""
    if not owner:
        raise HandoffError("missing_owner")
    if not trace_id:
        raise HandoffError("missing_trace_id")
    if not material_dir.is_dir():
        raise HandoffError("material_dir_unreadable", str(material_dir))

    report = load_report(report_path)
    description = load_source_description(report)
    units = load_materials(material_dir, report)

    recorded = []
    counters = {
        "materials": len(units),
        "appended": 0,
        "replayed": 0,
        "observations": 0,
        "source_references": 0,
        "outbox_events": 0,
    }
    with psycopg.connect(database_url) as conn:
        references = register_references(
            conn, report, description, owner, units, upload_id=upload_id
        )
        for unit in units:
            prev = conn.execute(
                (
                    "SELECT revision, content_hash FROM material_unit "
                    "WHERE material_unit_id=%s ORDER BY revision DESC LIMIT 1"
                ),
                (unit.material_unit_id,),
            ).fetchone()
            if prev is not None:
                unit_data = unit.SerializeToString(deterministic=True)
                unit_digest = "sha256:" + hashlib.sha256(unit_data).hexdigest()
                if unit_digest == prev[1]:
                    unit.revision = prev[0]
                else:
                    unit.revision = prev[0] + 1
                    unit.prev_revision = prev[0]

            # 写侧是唯一判官：True=新增，False=完全一致的 replay。这里不做任何"补一次"。
            appended = api_materials.append_material(
                conn, unit, trace_id=trace_id, execution_id=execution_id, auto_forward=True
            )
            counters["appended"] += int(appended)
            counters["replayed"] += int(not appended)
            counters["observations"] += len(unit.observations)
            counters["source_references"] += len(unit.source_refs)
            recorded.append(
                {
                    "material_unit_id": unit.material_unit_id,
                    "revision": unit.revision,
                    "prev_revision": unit.prev_revision if unit.HasField("prev_revision") else None,
                    "status": unit.status,
                    "start_ms": unit.time_range.start_ms,
                    "end_ms": unit.time_range.end_ms,
                    "observations": len(unit.observations),
                    "source_references": len(unit.source_refs),
                    "appended": appended,
                    "replayed": not appended,
                }
            )
        event_ids = [f"material:{unit.material_unit_id}:{unit.revision}" for unit in units]
        counters["outbox_events"] = conn.execute(
            "SELECT count(*) FROM event_outbox WHERE event_id = ANY(%s)", (event_ids,)
        ).fetchone()[0]

    return {
        "contract": "edge.material/v1/timeline-handoff",
        "database": describe_target(database_url),
        "owner": owner,
        "trace_id": trace_id,
        "execution_id": execution_id,
        "source": report["source"],
        "pipeline_version": report["pipeline"]["pipeline_version"],
        "references": references,
        "materials": recorded,
        "counters": counters,
        "blockers": list(BLOCKERS),
        "golden_path_verified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--material-dir", type=pathlib.Path, required=True)
    parser.add_argument("--owner", required=True, help="授权主体：素材读取权限由它决定")
    parser.add_argument("--trace-id", default="", help="写入 lineage 的追踪号")
    parser.add_argument("--upload-id", default="", help="console_upload 的 id，格式 asset_xxx")
    parser.add_argument("--execution-id", default="", help="绑定本次素材的不可变执行批次")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--out", type=pathlib.Path, default=None)
    arguments = parser.parse_args()

    database_url, origin = derive_database_url(arguments.database_url)
    print(f"database: {describe_target(database_url)} (from {origin})")
    try:
        document = handoff(
            report_path=arguments.report,
            material_dir=arguments.material_dir,
            owner=arguments.owner,
            trace_id=arguments.trace_id or f"timeline-handoff:{arguments.owner}",
            database_url=database_url,
            upload_id=arguments.upload_id,
            execution_id=arguments.execution_id,
        )
    except (HandoffError, api_materials.RevisionConflict) as error:
        # 带着稳定原因串失败：调用方据此分支，而不是解析数据库异常文本。
        print(f"timeline handoff failed: {error}")
        return 2
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.out is not None:
        arguments.out.write_text(payload + "\n", encoding="utf-8")
        print(f"handoff report written: {arguments.out}")
    else:
        print(payload)
    print(
        "timeline handoff: materials={materials} appended={appended} replayed={replayed} "
        "observations={observations} outbox={outbox_events} blockers={blockers}".format(
            **document["counters"], blockers=",".join(document["blockers"])
        )
    )
    print("timeline handoff golden_path_verified=false: vector / search are not exercised here")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

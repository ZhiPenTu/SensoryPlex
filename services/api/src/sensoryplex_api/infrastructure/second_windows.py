"""完整文件的逐秒素材：来源引用先落库，模型事实随后追加，始终不保留像素。"""

import hashlib
import time

from edge_material_sdk.generated.material.v1 import material_pb2

from . import materials

MAX_WINDOWS = 7200


def ranges(duration_ms: int):
    """尾部不足一秒仍是一个真实半开窗口；拒绝超出支持时长的文件。"""
    if not 0 < duration_ms <= MAX_WINDOWS * 1000:
        raise ValueError("timeline_window_count_exceeded")
    return [(start, min(start + 1000, duration_ms)) for start in range(0, duration_ms, 1000)]


def ensure_materials(
    conn, *, execution_id: str, asset_id: str, pending: list[str], empty_status="failed"
):
    """保留已经落库的观测，只补缺失的秒；同一执行重试不会覆盖历史版本。"""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("seconds:" + execution_id,)
    )
    asset = conn.execute(
        "SELECT stream_id,sha256,duration_ms FROM media_asset WHERE asset_id=%s", (asset_id,)
    ).fetchone()
    if not asset:
        raise ValueError("timeline_source_missing")
    stream_id, digest, duration = asset
    existing = conn.execute(
        """
        SELECT DISTINCT ON (m.material_unit_id) m.contract_bytes
        FROM material_unit m JOIN material_execution e
          ON (m.material_unit_id,m.revision)=(e.material_unit_id,e.material_revision)
        WHERE e.execution_id=%s AND m.stream_id=%s
        ORDER BY m.material_unit_id,m.revision DESC
        """,
        (execution_id, stream_id),
    ).fetchall()
    units = [material_pb2.MaterialUnit.FromString(row[0]) for row in existing]
    by_range = {(unit.time_range.start_ms, unit.time_range.end_ms): unit for unit in units}
    grid = ranges(duration)
    grid_set = set(grid)
    if len(by_range) != len(units) or any(key not in grid_set for key in by_range):
        raise ValueError("timeline_existing_window_grid_mismatch")
    created = int(time.time() * 1000)
    for start, end in grid:
        if (start, end) in by_range:
            continue
        key = hashlib.sha256(f"{execution_id}|{stream_id}|{start}|{end}".encode()).hexdigest()[:32]
        unit = material_pb2.MaterialUnit(
            material_unit_id="mat_second_" + key,
            stream_id=stream_id,
            time_range={"start_ms": start, "end_ms": end},
            status="partial" if pending else empty_status,
            revision=1,
            source_refs=[
                {
                    "asset_id": asset_id,
                    "content_hash": digest,
                    "time_range": {"start_ms": start, "end_ms": end},
                }
            ],
            pipeline_version="source-seconds-v1",
            pending_enrichments=pending,
            created_at_unix_ms=created,
        )
        materials.append_material(
            conn, unit, trace_id="seconds:" + execution_id, execution_id=execution_id
        )
        by_range[(start, end)] = unit
    return [by_range[key] for key in grid]


def anchors(conn, *, stream_id: str, content_hash: str, duration_ms: int, interval_ms: int):
    """登记可重新解码的时间锚点；它不是声称已解码成功的 video_frame。"""
    windows = ranges(duration_ms)
    result = []
    next_at = 0
    for start, end in windows:
        if start < next_at:
            continue
        next_at = start + interval_ms
        key = hashlib.sha256(f"{content_hash}|{start}|{end}".encode()).hexdigest()[:32]
        item_id = "anchor_" + key
        conn.execute(
            """INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms)
               VALUES (%s,%s,'media_time_anchor',%s,%s) ON CONFLICT DO NOTHING""",
            (item_id, stream_id, start, end),
        )
        result.append((item_id, start, end))
    return result

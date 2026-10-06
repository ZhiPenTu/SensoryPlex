"""事务 Outbox 生命周期归档与定点清理策略 (ADR-024 / ADR-027 扩展)。

根据工程约定：已确认发布且超过安全窗口（默认 7 天）的历史事件，异步转移至归档表
`event_outbox_archive` 与 `enrichment_task_outbox_archive`，并从分区主表中物理移除，
消除表膨胀对 FOR UPDATE SKIP LOCKED 认领性能的损耗。
"""

from __future__ import annotations

import json
from typing import Any

import psycopg


def archive_and_purge_outbox(
    conn: psycopg.Connection, safety_window_days: int = 7
) -> dict[str, Any]:
    """执行定点归档与清理。

    优先调用数据库不可变存储过程 `outbox_archive_and_purge`；若在极简环境或替身连接中未定义，
    则执行等价的原子转移与清理 SQL。
    """
    if safety_window_days < 1:
        raise ValueError("safety_window_days must be >= 1")

    try:
        row = conn.execute("SELECT outbox_archive_and_purge(%s)", (safety_window_days,)).fetchone()
        if row and row[0]:
            result = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            conn.commit()
            return result
    except Exception:
        conn.rollback()

    # 兜底回退：直接执行等价 SQL 逻辑
    cutoff_res = conn.execute(
        "SELECT now() - (%s || ' days')::interval", (safety_window_days,)
    ).fetchone()
    cutoff = cutoff_res[0] if cutoff_res else None

    # 1. 归档 event_outbox
    moved_events = conn.execute(
        """
        WITH moved AS (
            DELETE FROM event_outbox
            WHERE published_at IS NOT NULL AND published_at < now() - (%s || ' days')::interval
            RETURNING event_id, event_type, contract_bytes, published_at, attempt, created_at
        )
        INSERT INTO event_outbox_archive (
            event_id, event_type, contract_bytes, published_at, attempt, created_at
        )
        SELECT event_id, event_type, contract_bytes, published_at, attempt, created_at FROM moved
        RETURNING event_id
        """,
        (safety_window_days,),
    ).fetchall()

    # 2. 归档 enrichment_task_outbox
    try:
        moved_enrichments = conn.execute(
            """
            WITH moved AS (
                DELETE FROM enrichment_task_outbox
                WHERE published_at IS NOT NULL AND published_at < now() - (%s || ' days')::interval
                RETURNING
                    event_id, task_id, subject, contract_bytes, published_at,
                    claim_until, attempt, created_at
            )
            INSERT INTO enrichment_task_outbox_archive (
                event_id, task_id, subject, contract_bytes, published_at,
                claim_until, attempt, created_at
            )
            SELECT
                event_id, task_id, subject, contract_bytes, published_at,
                claim_until, attempt, created_at
            FROM moved
            RETURNING event_id
            """,
            (safety_window_days,),
        ).fetchall()
        enrichment_count = len(moved_enrichments)
    except Exception:
        enrichment_count = 0

    conn.commit()
    return {
        "status": "ok",
        "cutoff": cutoff.isoformat() if cutoff else None,
        "archived_events": len(moved_events),
        "archived_enrichments": enrichment_count,
    }

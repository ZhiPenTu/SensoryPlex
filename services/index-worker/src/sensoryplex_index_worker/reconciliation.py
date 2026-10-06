"""向量垃圾回收与事实对账巡检 (Vector GC & Tombstone Reconciliation Loop)。

解决痛点：
当素材废弃、更高版本覆盖 (superseded) 或物理清理时，底层向量库 (Milvus / pgvector)
中的历史向量通常不会被同步清除，导致检索面虽然能通过回查 PostgreSQL 过滤丢弃，
但会产生无效检索开销与 `unindexed_hits` 残留。

本模块提供后台周期性对账补偿 Worker (Reconciliation Loop)，基于事实状态巡检，
批量执行物理向量清理与数据库墓碑 (Tombstone) 标记，彻底消除索引污染。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import psycopg
from edge_material_sdk import get_logger

from . import records
from .milvus_store import VectorIndex

LOGGER = get_logger("sensoryplex.index.reconcile")


def reconcile_cycle(
    conn: psycopg.Connection,
    index: VectorIndex,
    *,
    batch: int = 100,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行一轮向量对账巡检与清理。

    1. 回查 PostgreSQL 中状态为 ready 但已被废弃/孤立/失败的向量记录；
    2. 若非 dry_run，先调用向量库物理删除对应的 embedding_id；
    3. 在 PostgreSQL 事务内将记录标记为 tombstoned 并清空 vector_ref；
    4. 返回结构化对账度量。
    """
    candidates = records.find_reconciliation_candidates(conn, index.vector_index_key, limit=batch)
    if not candidates:
        return {
            "event": "reconcile.cycle",
            "vector_index_key": index.vector_index_key,
            "candidates_found": 0,
            "vectors_deleted": 0,
            "tombstoned_count": 0,
            "dry_run": dry_run,
        }

    embedding_ids = [item["embedding_id"] for item in candidates]
    deleted_count = 0
    tombstoned_count = 0

    if not dry_run:
        index.ensure_collection()
        deleted_count = index.delete(embedding_ids)
        tombstoned_count = records.mark_tombstone(conn, embedding_ids)
        conn.commit()

    return {
        "event": "reconcile.cycle",
        "vector_index_key": index.vector_index_key,
        "candidates_found": len(candidates),
        "vectors_deleted": deleted_count,
        "tombstoned_count": tombstoned_count,
        "dry_run": dry_run,
        "sample_reasons": [item["reason"] for item in candidates[:5]],
    }


class ReconciliationRunner:
    """常驻后台对账补偿巡检工作器。"""

    def __init__(
        self,
        *,
        database_url: str,
        index: VectorIndex,
        interval_s: float = 60.0,
        batch: int = 100,
        dry_run: bool = False,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.database_url = database_url
        self.index = index
        self.interval_s = max(1.0, interval_s)
        self.batch = min(max(1, batch), 1000)
        self.dry_run = dry_run
        self._emit = emit or (lambda doc: None)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="sensoryplex-vector-reconcile")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10.0)

    def _run_loop(self) -> None:
        LOGGER.info(
            "Vector reconciliation worker started",
            vector_index_key=self.index.vector_index_key,
            interval_s=self.interval_s,
            batch=self.batch,
        )
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.database_url) as conn:
                    outcome = reconcile_cycle(
                        conn, self.index, batch=self.batch, dry_run=self.dry_run
                    )
                    self._emit(outcome)
            except Exception as error:  # noqa: BLE001
                LOGGER.error("Reconciliation cycle error", error=str(error))
                self._emit(
                    {
                        "event": "reconcile.error",
                        "vector_index_key": self.index.vector_index_key,
                        "error": type(error).__name__,
                    }
                )
            if self._stop.wait(timeout=self.interval_s):
                break
        LOGGER.info("Vector reconciliation worker stopped")

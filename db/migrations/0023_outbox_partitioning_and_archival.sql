-- 事务 Outbox 分区与生命周期归档，及向量对账墓碑 (ADR-024 / ADR-027 / ADR-031 扩展)

-- 1. 迁移 event_outbox 为按 created_at 范围分区表 (Table Partitioning)
ALTER TABLE event_outbox RENAME TO event_outbox_legacy;

CREATE TABLE event_outbox (
    event_id text NOT NULL,
    event_type text NOT NULL,
    contract_bytes bytea NOT NULL,
    published_at timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, created_at)
) PARTITION BY RANGE (created_at);

CREATE INDEX event_outbox_pending_idx
    ON event_outbox(created_at, event_id) WHERE published_at IS NULL;
CREATE INDEX event_outbox_published_idx
    ON event_outbox(published_at) WHERE published_at IS NOT NULL;

-- 预建 2026/2027 月度分区 + 兜底 DEFAULT 分区
CREATE TABLE event_outbox_y2026m08 PARTITION OF event_outbox
    FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00');
CREATE TABLE event_outbox_y2026m09 PARTITION OF event_outbox
    FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');
CREATE TABLE event_outbox_y2026m10 PARTITION OF event_outbox
    FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');
CREATE TABLE event_outbox_y2026m11 PARTITION OF event_outbox
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE event_outbox_y2026m12 PARTITION OF event_outbox
    FOR VALUES FROM ('2026-12-01 00:00:00+00') TO ('2027-01-01 00:00:00+00');
CREATE TABLE event_outbox_y2027m01 PARTITION OF event_outbox
    FOR VALUES FROM ('2027-01-01 00:00:00+00') TO ('2027-02-01 00:00:00+00');
CREATE TABLE event_outbox_default PARTITION OF event_outbox DEFAULT;

-- 从旧表回填数据并清理
INSERT INTO event_outbox (event_id, event_type, contract_bytes, published_at, attempt, created_at)
SELECT event_id, event_type, contract_bytes, published_at, attempt, created_at FROM event_outbox_legacy;

DROP TABLE event_outbox_legacy;


-- 2. 补齐与分区 enrichment_task_outbox
CREATE TABLE enrichment_task_outbox (
    event_id text NOT NULL,
    task_id text NOT NULL,
    subject text NOT NULL DEFAULT 'enrichment.task',
    contract_bytes bytea NOT NULL,
    published_at timestamptz,
    claim_until timestamptz,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, created_at)
) PARTITION BY RANGE (created_at);

CREATE INDEX enrichment_task_outbox_pending_idx
    ON enrichment_task_outbox(created_at, event_id) WHERE published_at IS NULL;
CREATE INDEX enrichment_task_outbox_published_idx
    ON enrichment_task_outbox(published_at) WHERE published_at IS NOT NULL;

CREATE TABLE enrichment_task_outbox_y2026m08 PARTITION OF enrichment_task_outbox
    FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00');
CREATE TABLE enrichment_task_outbox_y2026m09 PARTITION OF enrichment_task_outbox
    FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');
CREATE TABLE enrichment_task_outbox_y2026m10 PARTITION OF enrichment_task_outbox
    FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');
CREATE TABLE enrichment_task_outbox_y2026m11 PARTITION OF enrichment_task_outbox
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE enrichment_task_outbox_y2026m12 PARTITION OF enrichment_task_outbox
    FOR VALUES FROM ('2026-12-01 00:00:00+00') TO ('2027-01-01 00:00:00+00');
CREATE TABLE enrichment_task_outbox_y2027m01 PARTITION OF enrichment_task_outbox
    FOR VALUES FROM ('2027-01-01 00:00:00+00') TO ('2027-02-01 00:00:00+00');
CREATE TABLE enrichment_task_outbox_default PARTITION OF enrichment_task_outbox DEFAULT;


-- 3. 历史事件与任务异步归档表 (Archival Tables)
CREATE TABLE event_outbox_archive (
    event_id text NOT NULL,
    event_type text NOT NULL,
    contract_bytes bytea NOT NULL,
    published_at timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, created_at)
);
CREATE INDEX event_outbox_archive_created_idx ON event_outbox_archive(created_at);

CREATE TABLE enrichment_task_outbox_archive (
    event_id text NOT NULL,
    task_id text NOT NULL,
    subject text NOT NULL,
    contract_bytes bytea NOT NULL,
    published_at timestamptz,
    claim_until timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, created_at)
);
CREATE INDEX enrichment_task_outbox_archive_created_idx ON enrichment_task_outbox_archive(created_at);


-- 4. 存储过程：定点归档与清理 7 天前已发布历史事件 (TTL Archival Policy)
CREATE OR REPLACE FUNCTION outbox_archive_and_purge(safety_window_days integer DEFAULT 7)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    cutoff timestamptz;
    event_count integer := 0;
    enrichment_count integer := 0;
BEGIN
    cutoff := now() - (safety_window_days || ' days')::interval;

    -- 归档并清理 event_outbox 中已发布超过安全窗口的记录
    WITH moved_events AS (
        DELETE FROM event_outbox
        WHERE published_at IS NOT NULL AND published_at < cutoff
        RETURNING event_id, event_type, contract_bytes, published_at, attempt, created_at
    )
    INSERT INTO event_outbox_archive (event_id, event_type, contract_bytes, published_at, attempt, created_at)
    SELECT event_id, event_type, contract_bytes, published_at, attempt, created_at FROM moved_events;
    GET DIAGNOSTICS event_count = ROW_COUNT;

    -- 归档并清理 enrichment_task_outbox 中已发布超过安全窗口的记录
    WITH moved_enrichments AS (
        DELETE FROM enrichment_task_outbox
        WHERE published_at IS NOT NULL AND published_at < cutoff
        RETURNING event_id, task_id, subject, contract_bytes, published_at, claim_until, attempt, created_at
    )
    INSERT INTO enrichment_task_outbox_archive (event_id, task_id, subject, contract_bytes, published_at, claim_until, attempt, created_at)
    SELECT event_id, task_id, subject, contract_bytes, published_at, claim_until, attempt, created_at FROM moved_enrichments;
    GET DIAGNOSTICS enrichment_count = ROW_COUNT;

    RETURN jsonb_build_object(
        'status', 'ok',
        'cutoff', cutoff,
        'archived_events', event_count,
        'archived_enrichments', enrichment_count
    );
END;
$$;


-- 5. 支持向量墓碑标记 (Vector GC & Tombstone)
ALTER TABLE embedding_record DROP CONSTRAINT IF EXISTS embedding_record_state_check;
ALTER TABLE embedding_record ADD CONSTRAINT embedding_record_state_check
    CHECK (state IN ('pending', 'ready', 'failed', 'tombstoned'));
ALTER TABLE embedding_record ADD COLUMN IF NOT EXISTS tombstoned_at timestamptz;
CREATE INDEX IF NOT EXISTS embedding_record_reconcile_idx
    ON embedding_record(material_unit_id, material_revision) WHERE state = 'ready';


-- 6. PostgreSQL 原生向量余弦距离计算函数（为 pgvector / PostgreSQL 原生引擎备选）
CREATE OR REPLACE FUNCTION vector_cosine_distance(a float8[], b float8[])
RETURNS float8 LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    s float8 := 0;
    i int;
    len int := array_length(a, 1);
BEGIN
    IF a IS NULL OR b IS NULL OR len IS NULL OR len != array_length(b, 1) THEN
        RETURN 1.0;
    END IF;
    FOR i IN 1..len LOOP
        s := s + a[i] * b[i];
    END LOOP;
    RETURN 1.0 - s;
END;
$$;

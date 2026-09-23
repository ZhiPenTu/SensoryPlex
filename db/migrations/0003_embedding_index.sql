-- Vector index bookkeeping for the index-worker sink (ADR-020).
-- Append-only: 既有行、既有 ready/vector_ref 不变式都不改；这里只补"落库确认"需要的列与约束。
-- 向量本体不在这里：它写在 Milvus 里，本表只保存引用与重建依据。
ALTER TABLE embedding_record ADD COLUMN observation_id text;
ALTER TABLE embedding_record ADD COLUMN vector_index_key text;
ALTER TABLE embedding_record ADD COLUMN error_code text;
ALTER TABLE embedding_record ADD COLUMN indexed_at timestamptz;
ALTER TABLE embedding_record ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE embedding_record ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

-- "先写向量、再标 ready"必须能从行本身看出来：ready 的行一定带写入时刻，
-- failed 的行一定带原因码，不允许"失败了但没有原因"这种没法排查的状态。
ALTER TABLE embedding_record ADD CONSTRAINT embedding_record_ready_is_confirmed
    CHECK (state <> 'ready' OR (vector_ref IS NOT NULL AND indexed_at IS NOT NULL));
ALTER TABLE embedding_record ADD CONSTRAINT embedding_record_failed_has_reason
    CHECK (state <> 'failed' OR error_code IS NOT NULL);
ALTER TABLE embedding_record ADD CONSTRAINT embedding_record_ready_has_no_error
    CHECK (state <> 'ready' OR error_code IS NULL);

CREATE INDEX embedding_record_material ON embedding_record(material_unit_id, material_revision);
CREATE INDEX embedding_record_index_key ON embedding_record(vector_index_key) WHERE state = 'ready';

-- 素材版本链与历史血缘前进 (Revision Forwarding)
-- 支持受控的 revision 自动递增与版本链（revision -> prev_revision）追溯

ALTER TABLE material_unit ADD COLUMN IF NOT EXISTS prev_revision integer;

-- 允许在迁移中回填已有升版记录（如果有对应的前序 revision）
ALTER TABLE material_unit DISABLE TRIGGER material_revision_immutable;

UPDATE material_unit m
SET prev_revision = (
    SELECT max(prev.revision)
    FROM material_unit prev
    WHERE prev.material_unit_id = m.material_unit_id
      AND prev.revision < m.revision
)
WHERE m.prev_revision IS NULL
  AND m.revision > 1;

ALTER TABLE material_unit ENABLE TRIGGER material_revision_immutable;

-- 约束与外键
ALTER TABLE material_unit DROP CONSTRAINT IF EXISTS material_unit_prev_revision_check;
ALTER TABLE material_unit ADD CONSTRAINT material_unit_prev_revision_check
    CHECK (prev_revision IS NULL OR (prev_revision > 0 AND prev_revision < revision));

ALTER TABLE material_unit DROP CONSTRAINT IF EXISTS material_unit_prev_revision_fkey;
ALTER TABLE material_unit ADD CONSTRAINT material_unit_prev_revision_fkey
    FOREIGN KEY (material_unit_id, prev_revision)
    REFERENCES material_unit(material_unit_id, revision)
    DEFERRABLE INITIALLY IMMEDIATE;

CREATE INDEX IF NOT EXISTS material_unit_prev_revision_idx
    ON material_unit(material_unit_id, prev_revision);

-- 同一时间区间可有多个独立上游 Observation。完整输入身份已锁在 task_id/manifest_bytes。
DO $$
DECLARE constraint_name text;
BEGIN
    FOR constraint_name IN SELECT conname FROM pg_constraint
        WHERE conrelid='enrichment_task'::regclass AND contype='u'
    LOOP
        EXECUTE format('ALTER TABLE enrichment_task DROP CONSTRAINT %I',constraint_name);
    END LOOP;
END $$;

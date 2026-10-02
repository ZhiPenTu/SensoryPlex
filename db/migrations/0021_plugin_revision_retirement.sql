-- 方案事实不可变；停用追加独立记录，停止接受新运行，在飞执行继续固定原版本。
CREATE TABLE pipeline_revision_retirement (
    pipeline_id text NOT NULL,
    revision integer NOT NULL,
    retired_by text NOT NULL,
    retired_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pipeline_id,revision),
    FOREIGN KEY (pipeline_id,revision) REFERENCES pipeline_revision
);
CREATE TRIGGER revision_retirement_immutable BEFORE UPDATE OR DELETE ON pipeline_revision_retirement
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();
ALTER TABLE plugin_deployment_operation DROP CONSTRAINT plugin_deployment_operation_kind_check;
ALTER TABLE plugin_deployment_operation ADD CONSTRAINT plugin_deployment_operation_kind_check
    CHECK (kind IN ('provision','upgrade','rollback','retire'));

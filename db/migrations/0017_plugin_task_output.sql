-- ADR-032：进程重启后可恢复的上游输出，不改变历史 Observation 和回执。
CREATE TABLE plugin_task_output (
    task_id text PRIMARY KEY REFERENCES pipeline_task,
    assignment_id text NOT NULL REFERENCES scheduler_assignment,
    contract_bytes bytea NOT NULL,
    content_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER plugin_task_output_immutable BEFORE UPDATE OR DELETE ON plugin_task_output
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();

ALTER TABLE material_unit DROP CONSTRAINT material_unit_status_check;
ALTER TABLE material_unit ADD CONSTRAINT material_unit_status_check
    CHECK (status IN ('partial','fast_ready','enriched','failed','conflict','low_confidence','no_observations'));

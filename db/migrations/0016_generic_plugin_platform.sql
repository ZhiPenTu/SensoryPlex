-- ADR-032：外部制品、不可变 schema 与通用异步补全。保留历史事实和旧队列。
CREATE TABLE plugin_signer (
    signer_id text PRIMARY KEY,
    display_name text NOT NULL,
    public_key text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

ALTER TABLE plugin_release DROP CONSTRAINT plugin_release_trust_check;
ALTER TABLE plugin_release ADD CONSTRAINT plugin_release_trust_check
    CHECK (trust IN ('first_party','trusted_publisher'));

CREATE TABLE plugin_registration (
    release_id text PRIMARY KEY REFERENCES plugin_release,
    signer_id text REFERENCES plugin_signer,
    manifest jsonb NOT NULL,
    config_schema jsonb NOT NULL,
    schemas jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER plugin_registration_immutable BEFORE UPDATE OR DELETE ON plugin_registration
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();

-- 非模型算法使用 contract_bytes 中的 processor_release_id，不造 model_release 行。
ALTER TABLE observation ALTER COLUMN model_release_id DROP NOT NULL;

CREATE TABLE enrichment_task (
    task_id text PRIMARY KEY,
    execution_id text NOT NULL REFERENCES console_job_execution,
    run_id text NOT NULL REFERENCES pipeline_run,
    node_id text NOT NULL,
    target_node_id text NOT NULL REFERENCES console_node,
    route_id text NOT NULL,
    release_id text NOT NULL REFERENCES plugin_release,
    config_id text NOT NULL REFERENCES console_plugin_config,
    material_unit_id text NOT NULL,
    start_ms bigint NOT NULL CHECK (start_ms >= 0),
    end_ms bigint NOT NULL CHECK (end_ms > start_ms),
    manifest_bytes bytea NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','succeeded','failed','cancelled')),
    reason_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (execution_id,node_id,material_unit_id,start_ms,end_ms)
);
CREATE INDEX enrichment_task_pending ON enrichment_task(execution_id,state);
CREATE TABLE enrichment_outbox (
    task_id text PRIMARY KEY REFERENCES enrichment_task,
    subject text NOT NULL,
    contract_bytes bytea NOT NULL,
    published_at timestamptz,
    claim_until timestamptz,
    attempts integer NOT NULL DEFAULT 0
);
CREATE TABLE enrichment_result (
    task_id text PRIMARY KEY REFERENCES enrichment_task,
    result_digest text NOT NULL,
    contract_bytes bytea NOT NULL,
    staged_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER enrichment_result_immutable BEFORE UPDATE OR DELETE ON enrichment_result
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();
CREATE TABLE enrichment_result_outbox (
    task_id text PRIMARY KEY REFERENCES enrichment_result,
    contract_bytes bytea NOT NULL,
    published_at timestamptz,
    claim_until timestamptz,
    attempts integer NOT NULL DEFAULT 0
);

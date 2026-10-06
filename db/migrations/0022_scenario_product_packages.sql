-- ADR-029 P3: 场景化产品包实体、版本锁定、配置契约与调度策略。

CREATE TABLE scenario_package (
    package_id text PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    version text NOT NULL DEFAULT '1.0.0',
    pipeline_id text NOT NULL,
    pipeline_revision integer NOT NULL,
    graph_digest text NOT NULL CHECK (graph_digest ~ '^sha256:[0-9a-f]{64}$'),
    config_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    rbac_scopes jsonb NOT NULL DEFAULT '[]'::jsonb,
    scheduling_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'published', 'active', 'archived')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (pipeline_id, pipeline_revision)
        REFERENCES pipeline_revision(pipeline_id, revision) ON DELETE RESTRICT
);

CREATE INDEX scenario_package_state_idx ON scenario_package (state, created_at DESC);
CREATE INDEX scenario_package_pipeline_idx ON scenario_package (pipeline_id, pipeline_revision);

ALTER TABLE console_job_draft
    ADD COLUMN scenario_package_id text REFERENCES scenario_package(package_id) ON DELETE SET NULL;

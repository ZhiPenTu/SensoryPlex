-- 多模态 Console 任务与 ADR-029 编排运行之间的不可变桥接。
-- 历史单插件任务保留 legacy_ocr_v1 语义；本迁移绝不猜测它们本应执行的模态。

ALTER TABLE console_pipeline
    ADD COLUMN execution_mode text NOT NULL DEFAULT 'legacy_ocr_v1',
    ADD COLUMN orchestration_pipeline_id text,
    ADD COLUMN orchestration_revision integer,
    ADD COLUMN graph_digest text;

ALTER TABLE console_pipeline
    ADD CONSTRAINT console_pipeline_execution_mode_check
    CHECK (execution_mode IN ('legacy_ocr_v1', 'orchestrated_v2'));

ALTER TABLE console_pipeline
    ADD CONSTRAINT console_pipeline_execution_binding_check
    CHECK (
        (execution_mode = 'legacy_ocr_v1'
            AND orchestration_pipeline_id IS NULL
            AND orchestration_revision IS NULL
            AND graph_digest IS NULL)
        OR
        (execution_mode = 'orchestrated_v2'
            AND orchestration_pipeline_id IS NOT NULL
            AND orchestration_revision IS NOT NULL
            AND orchestration_revision > 0
            AND graph_digest ~ '^sha256:[0-9a-f]{64}$')
    );

ALTER TABLE console_pipeline
    ADD CONSTRAINT console_pipeline_orchestration_revision_fk
    FOREIGN KEY (orchestration_pipeline_id, orchestration_revision)
    REFERENCES pipeline_revision(pipeline_id, revision)
    ON DELETE RESTRICT;

-- 节点必需性属于不可变 Revision；展开后的 Task 复制这一事实，确保可选补全失败可见但不阻断快路径。
ALTER TABLE pipeline_task
    ADD COLUMN required boolean NOT NULL DEFAULT true;

ALTER TABLE console_job_draft DROP CONSTRAINT console_job_draft_state_check;
ALTER TABLE console_job_draft ADD CONSTRAINT console_job_draft_state_check
    CHECK (state IN ('draft', 'pending', 'processing', 'completed', 'failed', 'cancelled', 'archived'));

CREATE TABLE console_job_execution (
    execution_id text PRIMARY KEY,
    job_id text NOT NULL REFERENCES console_job_draft(id) ON DELETE RESTRICT,
    run_id text NOT NULL UNIQUE REFERENCES pipeline_run(run_id) ON DELETE RESTRICT,
    pipeline_id text NOT NULL REFERENCES pipeline_definition(pipeline_id) ON DELETE RESTRICT,
    pipeline_revision integer NOT NULL,
    graph_digest text NOT NULL CHECK (graph_digest ~ '^sha256:[0-9a-f]{64}$'),
    target_node_id text NOT NULL REFERENCES console_node(node_id) ON DELETE RESTRICT,
    input_ref text NOT NULL,
    state text NOT NULL CHECK (state IN (
        'accepted', 'queued', 'running', 'succeeded',
        'succeeded_with_partial_enrichment', 'failed', 'cancelled'
    )),
    modality_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    FOREIGN KEY (pipeline_id, pipeline_revision)
        REFERENCES pipeline_revision(pipeline_id, revision) ON DELETE RESTRICT,
    CONSTRAINT console_job_execution_completed_at_check CHECK (
        (state IN ('accepted', 'queued', 'running') AND completed_at IS NULL)
        OR (state NOT IN ('accepted', 'queued', 'running'))
    )
);

CREATE INDEX console_job_execution_job_idx
    ON console_job_execution(job_id, created_at DESC);
CREATE INDEX console_job_execution_node_state_idx
    ON console_job_execution(target_node_id, state, created_at DESC);

-- 回执是 assignment 确实执行的权威事实。其身份不可变；重复投递只能复现同一摘要。
CREATE TABLE task_execution_receipt (
    task_id text NOT NULL REFERENCES pipeline_task(task_id) ON DELETE CASCADE,
    attempt integer NOT NULL CHECK (attempt > 0),
    assignment_id text NOT NULL REFERENCES scheduler_assignment(assignment_id) ON DELETE RESTRICT,
    run_id text NOT NULL REFERENCES pipeline_run(run_id) ON DELETE CASCADE,
    plugin_id text NOT NULL,
    artifact_digest text NOT NULL CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    config_hash text NOT NULL CHECK (config_hash ~ '^sha256:[0-9a-f]{64}$'),
    input_count integer NOT NULL CHECK (input_count >= 0),
    output_count integer NOT NULL CHECK (output_count >= 0),
    result_manifest_ref text NOT NULL DEFAULT '',
    reason_code text NOT NULL DEFAULT '',
    receipt_digest text NOT NULL CHECK (receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, attempt, assignment_id),
    CONSTRAINT task_execution_receipt_interval_check CHECK (completed_at >= started_at)
);

CREATE UNIQUE INDEX task_execution_receipt_digest_idx
    ON task_execution_receipt(task_id, attempt, assignment_id, receipt_digest);

-- 同一素材可由多个隔离执行产出。默认查询范围由此关联决定，不能只凭素材标识猜测。
CREATE TABLE material_execution (
    material_unit_id text NOT NULL,
    material_revision integer NOT NULL,
    execution_id text NOT NULL REFERENCES console_job_execution(execution_id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (material_unit_id, material_revision, execution_id),
    FOREIGN KEY (material_unit_id, material_revision)
        REFERENCES material_unit(material_unit_id, revision) ON DELETE RESTRICT
);

CREATE INDEX material_execution_execution_idx
    ON material_execution(execution_id, created_at DESC);

-- VLM 慢路径采用「底座发布 - 插件竞争拉取」：PostgreSQL 是任务和结果的唯一事实源，
-- JetStream WorkQueue 只是有界的至少一次投递层。历史编排任务与事件 outbox 不改写。

ALTER TABLE console_job_execution
    DROP CONSTRAINT console_job_execution_state_check;

ALTER TABLE console_job_execution
    ADD CONSTRAINT console_job_execution_state_check CHECK (state IN (
        'accepted', 'queued', 'running', 'ready_for_review', 'succeeded',
        'succeeded_with_partial_enrichment', 'failed', 'cancelled'
    ));

ALTER TABLE console_job_execution
    DROP CONSTRAINT console_job_execution_completed_at_check;

ALTER TABLE console_job_execution
    ADD CONSTRAINT console_job_execution_completed_at_check CHECK (
        (state IN ('accepted', 'queued', 'running', 'ready_for_review') AND completed_at IS NULL)
        OR (state NOT IN ('accepted', 'queued', 'running', 'ready_for_review'))
    );

ALTER TABLE console_job_draft
    DROP CONSTRAINT console_job_draft_state_check;

ALTER TABLE console_job_draft
    ADD CONSTRAINT console_job_draft_state_check
    CHECK (state IN (
        'draft', 'pending', 'processing', 'ready_for_review', 'completed',
        'failed', 'cancelled', 'archived'
    ));

CREATE TABLE vlm_enrichment_task (
    task_id text PRIMARY KEY,
    execution_id text NOT NULL REFERENCES console_job_execution(execution_id) ON DELETE RESTRICT,
    asset_id text NOT NULL REFERENCES media_asset(asset_id) ON DELETE RESTRICT,
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    media_locator text NOT NULL CHECK (media_locator ~ '^media_asset:[A-Za-z0-9_-]+$'),
    stream_id text NOT NULL REFERENCES stream_session(stream_id) ON DELETE RESTRICT,
    source_id text NOT NULL REFERENCES media_source(source_id) ON DELETE RESTRICT,
    source_item_id text NOT NULL REFERENCES timeline_item(item_id) ON DELETE RESTRICT,
    material_unit_id text NOT NULL,
    start_ms bigint NOT NULL CHECK (start_ms >= 0),
    end_ms bigint NOT NULL CHECK (end_ms > start_ms),
    prompt text NOT NULL CHECK (length(prompt) BETWEEN 1 AND 2000),
    plugin_id text NOT NULL CHECK (plugin_id = 'org.sensoryplex.vlm-moondream'),
    artifact_digest text NOT NULL CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
    config_hash text NOT NULL CHECK (config_hash ~ '^sha256:[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('queued', 'published', 'running', 'succeeded', 'failed')),
    delivery_attempt integer NOT NULL DEFAULT 0 CHECK (delivery_attempt >= 0),
    result_digest text,
    reason_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    UNIQUE (execution_id, source_item_id),
    CONSTRAINT vlm_enrichment_task_interval_check CHECK (end_ms > start_ms),
    CONSTRAINT vlm_enrichment_task_terminal_check CHECK (
        (state IN ('queued', 'published', 'running') AND completed_at IS NULL)
        OR (state IN ('succeeded', 'failed') AND completed_at IS NOT NULL)
    )
);

CREATE INDEX vlm_enrichment_task_execution_state_idx
    ON vlm_enrichment_task(execution_id, state, start_ms);

-- 任务建成与要发布的不可变 protobuf 在同一事务里写下。发布确认之前绝不标记 published。
CREATE TABLE vlm_task_outbox (
    event_id text PRIMARY KEY,
    task_id text NOT NULL UNIQUE REFERENCES vlm_enrichment_task(task_id) ON DELETE CASCADE,
    contract_bytes bytea NOT NULL,
    published_at timestamptz,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX vlm_task_outbox_pending_idx
    ON vlm_task_outbox(created_at, event_id) WHERE published_at IS NULL;

-- 重投的相同结果只能重放；同 task_id 的不同摘要一律拒绝，避免 VLM 非确定性覆盖历史事实。
CREATE TABLE vlm_enrichment_result (
    task_id text PRIMARY KEY REFERENCES vlm_enrichment_task(task_id) ON DELETE RESTRICT,
    result_digest text NOT NULL CHECK (result_digest ~ '^sha256:[0-9a-f]{64}$'),
    observation_id text NOT NULL REFERENCES observation(observation_id) ON DELETE RESTRICT,
    contract_bytes bytea NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

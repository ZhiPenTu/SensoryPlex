-- ADR-029 P1: 可编排插件执行核心——持久 Run/Task、不可变 revision、边与调度租约。

CREATE TABLE pipeline_definition (
    pipeline_id text PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    owner text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pipeline_revision (
    pipeline_id text NOT NULL REFERENCES pipeline_definition(pipeline_id) ON DELETE RESTRICT,
    revision integer NOT NULL CHECK (revision > 0),
    graph_digest text NOT NULL,
    definition_json jsonb NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pipeline_id, revision)
);

CREATE TRIGGER deny_pipeline_revision_update
    BEFORE UPDATE OR DELETE ON pipeline_revision
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();

CREATE TABLE pipeline_run (
    run_id text PRIMARY KEY,
    pipeline_id text NOT NULL,
    revision integer NOT NULL,
    input_ref text NOT NULL,
    idempotency_key text NOT NULL,
    deadline_unix_ms bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('accepted', 'validating', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired')),
    owner text NOT NULL,
    error_code text,
    error_detail text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    FOREIGN KEY (pipeline_id, revision) REFERENCES pipeline_revision(pipeline_id, revision) ON DELETE RESTRICT
);

-- 幂等不变式：同一 revision + input_ref + idempotency key 至多创建一个活动 Run
CREATE UNIQUE INDEX pipeline_run_active_idempotency_idx 
    ON pipeline_run (pipeline_id, revision, input_ref, idempotency_key) 
    WHERE state IN ('accepted', 'validating', 'queued', 'running');

CREATE INDEX pipeline_run_owner_idx ON pipeline_run (owner, created_at DESC);

CREATE TABLE pipeline_task (
    task_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES pipeline_run(run_id) ON DELETE CASCADE,
    node_id text NOT NULL,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL DEFAULT 1 CHECK (max_attempts > 0),
    idempotency_key text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'ready', 'assigned', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'blocked')),
    assignment_id text,
    reason_code text,
    error_detail text,
    output_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, node_id)
);

CREATE INDEX pipeline_task_state_idx ON pipeline_task (state, updated_at);
CREATE INDEX pipeline_task_run_idx ON pipeline_task (run_id, node_id);

CREATE TABLE pipeline_task_edge (
    run_id text NOT NULL REFERENCES pipeline_run(run_id) ON DELETE CASCADE,
    from_node_id text NOT NULL,
    to_node_id text NOT NULL,
    modality text NOT NULL,
    join_policy text NOT NULL CHECK (join_policy IN ('same_item', 'same_stream_window', 'window_contains')),
    required boolean NOT NULL DEFAULT true,
    PRIMARY KEY (run_id, from_node_id, to_node_id, modality)
);

CREATE TABLE scheduler_assignment (
    assignment_id text PRIMARY KEY,
    task_id text NOT NULL REFERENCES pipeline_task(task_id) ON DELETE CASCADE,
    run_id text NOT NULL REFERENCES pipeline_run(run_id) ON DELETE CASCADE,
    attempt integer NOT NULL CHECK (attempt > 0),
    requested_node_id text,
    actual_node_id text,
    data_plane_node_id text,
    decision text NOT NULL CHECK (decision IN ('assigned', 'rejected', 'lease_expired', 'cancelled')),
    reason_code text,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX scheduler_assignment_task_idx ON scheduler_assignment (task_id, attempt DESC);
CREATE INDEX scheduler_assignment_lease_idx ON scheduler_assignment (lease_expires_at) 
    WHERE decision = 'assigned';

CREATE TABLE orchestration_audit (
    id text PRIMARY KEY,
    run_id text NOT NULL,
    task_id text,
    action text NOT NULL,
    actor text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX orchestration_audit_run_idx ON orchestration_audit (run_id, created_at DESC);

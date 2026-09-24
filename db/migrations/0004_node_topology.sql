-- LAN plugin worker topology and node registry (ADR-026).
-- Separates node state from plugin instance state; append-only schema evolution.

CREATE TABLE console_node (
    node_id text PRIMARY KEY,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','enrolling','ready','draining','offline','revoked')),
    status_reason text NOT NULL DEFAULT '',
    platform text NOT NULL,
    arch text NOT NULL,
    cpu_cores integer NOT NULL DEFAULT 1 CHECK (cpu_cores > 0),
    memory_bytes bigint NOT NULL DEFAULT 0 CHECK (memory_bytes >= 0),
    unified_memory_bytes bigint NOT NULL DEFAULT 0 CHECK (unified_memory_bytes >= 0),
    accelerators jsonb NOT NULL DEFAULT '[]'::jsonb,
    supported_artifacts text[] NOT NULL DEFAULT '{}',
    labels jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_co_located boolean NOT NULL DEFAULT false,
    session_token_hash text,
    last_heartbeat_at timestamptz,
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX console_node_status ON console_node(status, last_heartbeat_at DESC);

CREATE TABLE console_node_enrollment_token (
    token_hash text PRIMARY KEY,
    node_id text NOT NULL,
    created_by text NOT NULL,
    expires_at timestamptz NOT NULL,
    used boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX console_node_token_expiry ON console_node_enrollment_token(expires_at) WHERE NOT used;

CREATE TABLE console_plugin_instance (
    instance_id text PRIMARY KEY,
    node_id text NOT NULL REFERENCES console_node(node_id),
    plugin_id text NOT NULL,
    plugin_version text NOT NULL,
    artifact_digest text NOT NULL,
    previous_digest text,
    desired_state text NOT NULL CHECK (desired_state IN ('planned','ready','stopped','uninstalled')),
    actual_state text NOT NULL CHECK (actual_state IN ('planned','installing','ready','degraded','draining','stopped','failed','rolled_back','uninstalled')),
    config_hash text NOT NULL,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code text,
    error_detail text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT console_plugin_instance_node_plugin_uniq UNIQUE (node_id, plugin_id)
);

CREATE INDEX console_plugin_instance_node ON console_plugin_instance(node_id, actual_state);

CREATE TABLE console_deployment_intent (
    id text PRIMARY KEY,
    node_id text NOT NULL REFERENCES console_node(node_id),
    instance_id text NOT NULL REFERENCES console_plugin_instance(instance_id),
    action text NOT NULL CHECK (action IN ('install','start','stop','uninstall','rollback','drain')),
    artifact_digest text NOT NULL,
    rollback_digest text,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','dispatched','completed','failed','expired')),
    error_code text,
    error_detail text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    completed_at timestamptz
);

CREATE INDEX console_deployment_intent_queue ON console_deployment_intent(node_id, state, created_at);

CREATE TABLE console_task_assignment (
    id text PRIMARY KEY,
    job_id text NOT NULL,
    target_node_id text NOT NULL REFERENCES console_node(node_id),
    actual_node_id text REFERENCES console_node(node_id),
    state text NOT NULL DEFAULT 'scheduled' CHECK (state IN ('scheduled','running','completed','failed','reassigned','cancelled')),
    reassignment_reason text,
    data_locality_checked boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX console_task_assignment_job ON console_task_assignment(job_id, target_node_id);

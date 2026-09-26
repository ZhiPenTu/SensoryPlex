-- 插件热部署执行器：不可变 release、版本化运行实例与部署操作（ADR-030）。
-- 只追加，不覆盖历史 revision；槽位语义仍由 console_plugin_instance 承担。

CREATE TABLE plugin_release (
    release_id text PRIMARY KEY,
    plugin_id text NOT NULL,
    plugin_version text NOT NULL,
    platform text NOT NULL CHECK (platform IN ('macos','linux')),
    arch text NOT NULL CHECK (arch IN ('aarch64','x86_64')),
    form text NOT NULL CHECK (form IN ('local_native')),
    -- 可执行代码身份（插件包 package_digest）：候选进程启动时必须自证这个值。
    artifact_digest text NOT NULL,
    -- 完整传输内容摘要（bundle 文件字节），bundle 内部 manifest 因此不必自引用。
    bundle_digest text NOT NULL,
    manifest_digest text NOT NULL,
    config_schema_digest text NOT NULL,
    sbom_digest text NOT NULL,
    bundle_bytes bigint NOT NULL CHECK (bundle_bytes > 0),
    entrypoint jsonb NOT NULL,
    runtime_requirements jsonb NOT NULL,
    trust text NOT NULL CHECK (trust IN ('first_party')),
    authenticated boolean NOT NULL,
    authentication_method text NOT NULL,
    signature_status text NOT NULL,
    sbom_components integer NOT NULL DEFAULT 0 CHECK (sbom_components >= 0),
    declared_memory_bytes bigint NOT NULL DEFAULT 0 CHECK (declared_memory_bytes >= 0),
    declared_cpu_millicores integer NOT NULL DEFAULT 0 CHECK (declared_cpu_millicores >= 0),
    default_deadline_ms integer NOT NULL DEFAULT 120000 CHECK (default_deadline_ms > 0),
    -- 制品仓内相对路径；API 导入时重新计算该文件字节的 sha256，客户端无法伪造摘要。
    bundle_path text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT plugin_release_identity_uniq UNIQUE (plugin_id, plugin_version, platform, arch)
);

CREATE INDEX plugin_release_lookup ON plugin_release(plugin_id, platform, arch, published_at DESC);

-- 槽位 active 指针与 fencing 世代：只有控制面能以事务 + generation CAS 修改。
ALTER TABLE console_plugin_instance
    ADD COLUMN active_runtime_instance_id text,
    ADD COLUMN active_release_id text,
    ADD COLUMN previous_runtime_instance_id text,
    ADD COLUMN generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    ADD COLUMN endpoint text NOT NULL DEFAULT '';

CREATE TABLE plugin_runtime_instance (
    runtime_instance_id text PRIMARY KEY,
    instance_id text NOT NULL REFERENCES console_plugin_instance(instance_id),
    node_id text NOT NULL REFERENCES console_node(node_id),
    plugin_id text NOT NULL,
    release_id text NOT NULL REFERENCES plugin_release(release_id),
    artifact_digest text NOT NULL,
    bundle_digest text NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 0),
    role text NOT NULL CHECK (role IN ('candidate','active','previous','failed')),
    state text NOT NULL CHECK (state IN (
        'planned','staged','starting','validating','candidate_ready',
        'active','draining','stopped','failed','uninstalled'
    )),
    endpoint text NOT NULL DEFAULT '',
    supervisor_id text NOT NULL DEFAULT '',
    unit_name text NOT NULL DEFAULT '',
    install_dir text NOT NULL DEFAULT '',
    verified_plugin_id text NOT NULL DEFAULT '',
    verified_artifact_digest text NOT NULL DEFAULT '',
    launch_ms integer CHECK (launch_ms IS NULL OR launch_ms >= 0),
    drain_ms integer CHECK (drain_ms IS NULL OR drain_ms >= 0),
    error_code text,
    error_detail text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- 同一槽位 + 同一 release 的同一个 generation 只能有一个运行实例。
    CONSTRAINT plugin_runtime_instance_generation_uniq UNIQUE (instance_id, release_id, generation)
);

CREATE INDEX plugin_runtime_instance_slot ON plugin_runtime_instance(instance_id, role, state);
CREATE INDEX plugin_runtime_instance_node ON plugin_runtime_instance(node_id, state);

CREATE TABLE plugin_deployment_operation (
    operation_id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('provision','upgrade','rollback')),
    node_id text NOT NULL REFERENCES console_node(node_id),
    instance_id text NOT NULL REFERENCES console_plugin_instance(instance_id),
    plugin_id text NOT NULL,
    release_id text NOT NULL REFERENCES plugin_release(release_id),
    from_runtime_instance_id text REFERENCES plugin_runtime_instance(runtime_instance_id),
    candidate_runtime_instance_id text NOT NULL
        REFERENCES plugin_runtime_instance(runtime_instance_id),
    rollback_of_operation_id text REFERENCES plugin_deployment_operation(operation_id),
    generation bigint NOT NULL CHECK (generation >= 0),
    stage text NOT NULL CHECK (stage IN (
        'accepted','staging','starting','validating','candidate_ready',
        'cutting_over','draining_old','succeeded','failed','cancelled'
    )),
    cancelled boolean NOT NULL DEFAULT false,
    deadline_unix_ms bigint NOT NULL,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    staging_ms integer CHECK (staging_ms IS NULL OR staging_ms >= 0),
    starting_ms integer CHECK (starting_ms IS NULL OR starting_ms >= 0),
    validating_ms integer CHECK (validating_ms IS NULL OR validating_ms >= 0),
    draining_ms integer CHECK (draining_ms IS NULL OR draining_ms >= 0),
    error_code text,
    error_detail text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE INDEX plugin_deployment_operation_slot
    ON plugin_deployment_operation(instance_id, created_at DESC);
CREATE INDEX plugin_deployment_operation_state
    ON plugin_deployment_operation(node_id, stage, created_at DESC);

-- 部署意图扩展：热部署意图必须携带 operation/generation/release/bundle 身份与截止时间。
ALTER TABLE console_deployment_intent
    DROP CONSTRAINT console_deployment_intent_action_check;

ALTER TABLE console_deployment_intent
    ADD CONSTRAINT console_deployment_intent_action_check CHECK (action IN (
        'install','start','stop','uninstall','rollback','drain','task_process',
        'stage_release','reconcile'
    ));

ALTER TABLE console_deployment_intent
    ADD COLUMN operation_id text REFERENCES plugin_deployment_operation(operation_id),
    ADD COLUMN generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    ADD COLUMN release_id text REFERENCES plugin_release(release_id),
    ADD COLUMN bundle_digest text,
    ADD COLUMN runtime_instance_id text REFERENCES plugin_runtime_instance(runtime_instance_id),
    ADD COLUMN grace_period_ms integer CHECK (grace_period_ms IS NULL OR grace_period_ms >= 0),
    ADD COLUMN endpoint_file_path text,
    ADD COLUMN deadline_unix_ms bigint;

CREATE INDEX console_deployment_intent_operation
    ON console_deployment_intent(operation_id, action, state);

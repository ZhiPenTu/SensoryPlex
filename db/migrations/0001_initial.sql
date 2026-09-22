-- Append-only migrations. All media intervals are [start_ms, end_ms).
CREATE TABLE media_source (
    source_id text PRIMARY KEY,
    type text NOT NULL CHECK (type IN ('file', 'srt')),
    uri_redacted text NOT NULL,
    owner text NOT NULL,
    policy jsonb NOT NULL DEFAULT '{"data_egress":"local_only"}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE stream_session (
    stream_id text PRIMARY KEY,
    source_id text NOT NULL REFERENCES media_source,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    clock_offset_ms bigint NOT NULL DEFAULT 0,
    status text NOT NULL CHECK (status IN ('pending', 'running', 'stopping', 'stopped', 'failed')),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE TABLE media_asset (
    asset_id text PRIMARY KEY,
    stream_id text NOT NULL REFERENCES stream_session,
    object_uri text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^sha256:[0-9a-f]{64}$'),
    codec text NOT NULL,
    duration_ms bigint NOT NULL CHECK (duration_ms > 0)
);
CREATE INDEX media_asset_hash_idx ON media_asset(sha256);
CREATE TABLE model_release (
    model_release_id text PRIMARY KEY,
    name text NOT NULL,
    version text NOT NULL,
    artifact_hash text NOT NULL CHECK (artifact_hash ~ '^sha256:[0-9a-f]{64}$'),
    backend text NOT NULL,
    config_hash text NOT NULL CHECK (config_hash ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE timeline_item (
    item_id text PRIMARY KEY,
    stream_id text NOT NULL REFERENCES stream_session,
    kind text NOT NULL,
    start_ms bigint NOT NULL CHECK (start_ms >= 0),
    end_ms bigint NOT NULL,
    parent_item_id text REFERENCES timeline_item,
    CHECK (end_ms > start_ms)
);
CREATE TABLE observation (
    observation_id text PRIMARY KEY,
    item_id text NOT NULL REFERENCES timeline_item,
    modality text NOT NULL,
    payload_jsonb jsonb NOT NULL,
    confidence double precision CHECK (confidence >= 0 AND confidence <= 1),
    model_release_id text NOT NULL REFERENCES model_release,
    contract_bytes bytea NOT NULL
);
CREATE TABLE material_unit (
    material_unit_id text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    stream_id text NOT NULL REFERENCES stream_session,
    start_ms bigint NOT NULL CHECK (start_ms >= 0),
    end_ms bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('partial','fast_ready','enriched','failed','conflict','low_confidence')),
    tags text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL,
    contract_bytes bytea NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (material_unit_id, revision),
    CHECK (end_ms > start_ms)
);
CREATE INDEX material_stream_time_idx ON material_unit(stream_id, start_ms, end_ms);
CREATE INDEX material_tags_idx ON material_unit USING gin(tags);
CREATE TABLE material_observation (
    material_unit_id text NOT NULL,
    revision integer NOT NULL,
    observation_id text NOT NULL REFERENCES observation,
    role text NOT NULL,
    PRIMARY KEY (material_unit_id, revision, observation_id),
    FOREIGN KEY (material_unit_id, revision) REFERENCES material_unit
);
CREATE TABLE material_source_reference (
    material_unit_id text NOT NULL,
    revision integer NOT NULL,
    asset_id text NOT NULL REFERENCES media_asset,
    start_ms bigint NOT NULL CHECK (start_ms >= 0),
    end_ms bigint NOT NULL CHECK (end_ms > start_ms),
    PRIMARY KEY (material_unit_id, revision, asset_id, start_ms, end_ms),
    FOREIGN KEY (material_unit_id, revision) REFERENCES material_unit
);
CREATE TABLE embedding_record (
    embedding_id text PRIMARY KEY,
    material_unit_id text NOT NULL,
    material_revision integer NOT NULL,
    model_release_id text NOT NULL REFERENCES model_release,
    vector_ref text,
    dimension integer NOT NULL CHECK (dimension > 0),
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('pending','ready','failed')),
    CHECK (state != 'ready' OR vector_ref IS NOT NULL),
    FOREIGN KEY (material_unit_id, material_revision) REFERENCES material_unit
);
CREATE TABLE processing_job (
    job_id text PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('pending','running','succeeded','failed','cancelled')),
    attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE consumed_event (
    event_id text NOT NULL,
    consumer_name text NOT NULL,
    consumed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, consumer_name)
);
CREATE TABLE event_outbox (
    event_id text PRIMARY KEY,
    event_type text NOT NULL,
    contract_bytes bytea NOT NULL,
    published_at timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION deny_fact_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable_fact_requires_new_revision';
END;
$$;
CREATE TRIGGER model_release_immutable BEFORE UPDATE ON model_release
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();
CREATE TRIGGER observation_immutable BEFORE UPDATE ON observation
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();
CREATE TRIGGER material_revision_immutable BEFORE UPDATE ON material_unit
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();

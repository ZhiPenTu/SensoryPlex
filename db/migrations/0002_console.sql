-- Console metadata only; no runtime execution or material facts are synthesized.
CREATE TABLE console_user (
    username text PRIMARY KEY,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    roles text[] NOT NULL,
    disabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE console_session (
    token_hash text PRIMARY KEY,
    username text NOT NULL REFERENCES console_user,
    csrf_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX console_session_expiry ON console_session(expires_at);
CREATE TABLE console_login_attempt (
    bucket text PRIMARY KEY,
    failures integer NOT NULL,
    window_start timestamptz NOT NULL
);
CREATE TABLE console_token (
    id text PRIMARY KEY,
    username text NOT NULL REFERENCES console_user,
    name text NOT NULL,
    token_hash text NOT NULL UNIQUE,
    scopes text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE console_upload (
    id text PRIMARY KEY,
    owner text NOT NULL,
    filename text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    content_type text NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','awaiting_admission')),
    sha256 text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX console_upload_owner ON console_upload(owner, created_at DESC, id);
CREATE TABLE console_plugin_config (
    id text PRIMARY KEY,
    plugin_id text NOT NULL,
    name text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    config jsonb NOT NULL,
    config_hash text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plugin_id, name, revision)
);
CREATE TABLE console_pipeline (
    id text PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL,
    plugin_id text NOT NULL,
    plugin_digest text NOT NULL,
    config_id text NOT NULL REFERENCES console_plugin_config,
    revision integer NOT NULL CHECK (revision > 0),
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','archived')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, revision)
);
CREATE TABLE console_job_draft (
    id text PRIMARY KEY,
    owner text NOT NULL,
    asset_id text NOT NULL REFERENCES console_upload,
    pipeline_id text NOT NULL REFERENCES console_pipeline,
    name text NOT NULL,
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','archived')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX console_job_owner ON console_job_draft(owner, created_at DESC, id);
CREATE TABLE console_audit (
    id text PRIMARY KEY,
    actor text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX console_audit_time ON console_audit(created_at DESC, id);
CREATE TRIGGER console_config_immutable BEFORE UPDATE ON console_plugin_config
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();

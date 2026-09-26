-- 多模态执行的 1 秒时间轴覆盖事实。没有 Observation 的格子也必须有状态，但绝不能伪造成素材。

CREATE TABLE timeline_window_state (
    execution_id text NOT NULL REFERENCES console_job_execution(execution_id) ON DELETE RESTRICT,
    stream_id text NOT NULL REFERENCES stream_session(stream_id) ON DELETE RESTRICT,
    start_ms bigint NOT NULL CHECK (start_ms >= 0 AND start_ms % 1000 = 0),
    end_ms bigint NOT NULL CHECK (end_ms > start_ms AND end_ms <= start_ms + 1000),
    state_revision integer NOT NULL CHECK (state_revision > 0),
    sampling_state text NOT NULL CHECK (sampling_state IN (
        'sampled', 'not_sampled_by_policy', 'not_applicable', 'queued', 'running', 'failed'
    )),
    modality_states jsonb NOT NULL CHECK (jsonb_typeof(modality_states) = 'object'),
    reason_codes jsonb NOT NULL CHECK (jsonb_typeof(reason_codes) = 'object'),
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (execution_id, stream_id, start_ms, end_ms, state_revision)
);

CREATE INDEX timeline_window_state_current_idx
    ON timeline_window_state(execution_id, stream_id, start_ms, state_revision DESC);

CREATE TRIGGER timeline_window_state_immutable
    BEFORE UPDATE OR DELETE ON timeline_window_state
    FOR EACH ROW EXECUTE FUNCTION deny_fact_update();

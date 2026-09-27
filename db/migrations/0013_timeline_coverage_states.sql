-- 全帧判别让"没送模型"有了两种不同的诚实语义，采样状态必须能把它们分开：
-- `covered_without_model_refresh` = 这一秒被逐帧判别过，但计划本来就不在这里重新送模型；
-- `semantic_refresh_without_observation` = 计划在这一秒刷新过，但没有留下观测（含被有界
-- 数据面拒绝 `data_plane_retention_rejected` 与模型无结果两种原因）。
-- 迁移只追加取值，不改写历史 revision，旧的 6 个取值继续有效。

ALTER TABLE timeline_window_state
    DROP CONSTRAINT timeline_window_state_sampling_state_check;

ALTER TABLE timeline_window_state
    ADD CONSTRAINT timeline_window_state_sampling_state_check CHECK (sampling_state IN (
        'sampled',
        'not_sampled_by_policy',
        'not_applicable',
        'queued',
        'running',
        'failed',
        'covered_without_model_refresh',
        'semantic_refresh_without_observation'
    ));

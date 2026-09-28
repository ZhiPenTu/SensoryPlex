-- VLM Consumer 的不可重试失败也必须持久化回执。否则事务提交后、JetStream ACK 前的
-- 崩溃会让同一失败结果重投并被误判为冲突，最终耗尽投递预算。失败没有 Observation，
-- 所以仅将该列放宽为可空；成功路径仍由写侧校验 Observation 的完整身份和血缘。

ALTER TABLE vlm_enrichment_result
    ALTER COLUMN observation_id DROP NOT NULL;

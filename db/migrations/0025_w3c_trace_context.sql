-- 全链路可观测性：W3C TraceContext（traceparent）全链路透传
-- 支持端到端分布式链路追踪、耗时瀑布图与瓶颈定位

ALTER TABLE pipeline_run ADD COLUMN IF NOT EXISTS traceparent text;
ALTER TABLE pipeline_task ADD COLUMN IF NOT EXISTS traceparent text;
ALTER TABLE console_job_execution ADD COLUMN IF NOT EXISTS traceparent text;
ALTER TABLE task_execution_receipt ADD COLUMN IF NOT EXISTS traceparent text;
ALTER TABLE scheduler_assignment ADD COLUMN IF NOT EXISTS traceparent text;
ALTER TABLE event_outbox ADD COLUMN IF NOT EXISTS traceparent text;
ALTER TABLE vlm_task_outbox ADD COLUMN IF NOT EXISTS traceparent text;
ALTER TABLE enrichment_task_outbox ADD COLUMN IF NOT EXISTS traceparent text;
ALTER TABLE processing_job ADD COLUMN IF NOT EXISTS traceparent text;

CREATE INDEX IF NOT EXISTS pipeline_run_traceparent_idx ON pipeline_run(traceparent) WHERE traceparent IS NOT NULL;
CREATE INDEX IF NOT EXISTS console_job_execution_traceparent_idx ON console_job_execution(traceparent) WHERE traceparent IS NOT NULL;

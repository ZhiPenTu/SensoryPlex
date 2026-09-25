-- 修复历史 task_process 意图失败后仍停留在 processing 的状态漂移。
-- 只回填已由 Agent 明确回报 unknown_deployment_action 的历史行；不推断或覆盖其他失败语义。
UPDATE console_job_draft AS job
SET
    state = 'failed',
    error_code = intent.error_code,
    error_detail = intent.error_detail,
    completed_at = COALESCE(intent.completed_at, now())
FROM console_deployment_intent AS intent
WHERE intent.job_id = job.id
  AND job.state = 'processing'
  AND intent.action = 'task_process'
  AND intent.state = 'failed'
  AND intent.error_code = 'unknown_deployment_action';

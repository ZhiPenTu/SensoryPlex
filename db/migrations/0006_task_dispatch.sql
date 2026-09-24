-- Unified intent queue extension for task dispatch (ADR-028 / GP-01).
ALTER TABLE console_deployment_intent DROP CONSTRAINT IF EXISTS console_deployment_intent_instance_id_fkey;
ALTER TABLE console_deployment_intent ALTER COLUMN instance_id DROP NOT NULL;
ALTER TABLE console_deployment_intent DROP CONSTRAINT IF EXISTS console_deployment_intent_action_check;
ALTER TABLE console_deployment_intent ADD CONSTRAINT console_deployment_intent_action_check
    CHECK (action IN ('install','start','stop','uninstall','rollback','drain','task_process'));

ALTER TABLE console_deployment_intent ADD COLUMN IF NOT EXISTS job_id text;

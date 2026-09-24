-- Job dispatch and execution states (ADR-028 / GP-01 Golden Path).
-- Expands console_job_draft from draft/archived into a full execution lifecycle.

ALTER TABLE console_job_draft DROP CONSTRAINT console_job_draft_state_check;
ALTER TABLE console_job_draft ADD CONSTRAINT console_job_draft_state_check
    CHECK (state IN ('draft', 'pending', 'processing', 'completed', 'failed', 'archived'));

ALTER TABLE console_job_draft ADD COLUMN IF NOT EXISTS target_node_id text REFERENCES console_node(node_id);
ALTER TABLE console_job_draft ADD COLUMN IF NOT EXISTS error_code text;
ALTER TABLE console_job_draft ADD COLUMN IF NOT EXISTS error_detail text;
ALTER TABLE console_job_draft ADD COLUMN IF NOT EXISTS dispatched_at timestamptz;
ALTER TABLE console_job_draft ADD COLUMN IF NOT EXISTS completed_at timestamptz;

CREATE INDEX IF NOT EXISTS console_job_draft_execution ON console_job_draft(owner, state, dispatched_at DESC);

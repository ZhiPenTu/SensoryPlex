-- Pipeline publish state support (ADR-028 / GP-01).
ALTER TABLE console_pipeline DROP CONSTRAINT IF EXISTS console_pipeline_state_check;
ALTER TABLE console_pipeline ADD CONSTRAINT console_pipeline_state_check
    CHECK (state IN ('draft', 'published', 'archived'));

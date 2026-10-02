-- ADR-032：异步补全的恢复租约仍由 PostgreSQL 判定，队列只是有界投递。
ALTER TABLE enrichment_task ADD COLUMN attempt integer NOT NULL DEFAULT 0 CHECK(attempt>=0);
ALTER TABLE enrichment_task ADD COLUMN lease_id text NOT NULL DEFAULT '';
ALTER TABLE enrichment_task ADD COLUMN lease_expires_at timestamptz;
ALTER TABLE enrichment_task ADD COLUMN deadline_unix_ms bigint NOT NULL DEFAULT 0;
CREATE INDEX enrichment_task_recovery ON enrichment_task(state,deadline_unix_ms,lease_expires_at);
CREATE FUNCTION deny_enrichment_identity_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.manifest_bytes,NEW.release_id,NEW.config_id,NEW.route_id,NEW.target_node_id,
        NEW.execution_id,NEW.run_id,NEW.start_ms,NEW.end_ms,NEW.deadline_unix_ms)
       IS DISTINCT FROM
       (OLD.manifest_bytes,OLD.release_id,OLD.config_id,OLD.route_id,OLD.target_node_id,
        OLD.execution_id,OLD.run_id,OLD.start_ms,OLD.end_ms,OLD.deadline_unix_ms) THEN
        RAISE EXCEPTION 'enrichment_task_identity_is_immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER enrichment_identity_immutable BEFORE UPDATE ON enrichment_task
    FOR EACH ROW EXECUTE FUNCTION deny_enrichment_identity_change();

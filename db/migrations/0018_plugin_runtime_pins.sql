-- ADR-032：每个运行实例固定自己的配置；历史实例保留可确认的配置快照。
ALTER TABLE plugin_runtime_instance ADD COLUMN config_hash text NOT NULL DEFAULT '';
ALTER TABLE plugin_runtime_instance ADD COLUMN config jsonb NOT NULL DEFAULT '{}';
UPDATE plugin_runtime_instance r SET config=o.config
FROM plugin_deployment_operation o
WHERE o.candidate_runtime_instance_id=r.runtime_instance_id;
UPDATE plugin_runtime_instance r SET config_hash=s.config_hash
FROM console_plugin_instance s
WHERE s.active_runtime_instance_id=r.runtime_instance_id AND s.config=r.config;
CREATE INDEX plugin_runtime_pinned_identity ON plugin_runtime_instance
    (node_id,release_id,config_hash,state);
CREATE FUNCTION deny_runtime_config_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.config_hash<>'' AND (NEW.config_hash,NEW.config) IS DISTINCT FROM
       (OLD.config_hash,OLD.config) THEN
        RAISE EXCEPTION 'runtime_configuration_is_immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER runtime_config_immutable BEFORE UPDATE ON plugin_runtime_instance
    FOR EACH ROW EXECUTE FUNCTION deny_runtime_config_change();

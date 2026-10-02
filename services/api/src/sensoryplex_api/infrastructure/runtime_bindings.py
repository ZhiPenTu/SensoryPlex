"""固定 release/config 的运行实例查询；已发布方案不跟随槽位最新指针。"""

from ..contracts import one

MAX_RESIDENT_VERSIONS = 4


def pinned_runtime(conn, target_node_id, node):
    return one(
        conn,
        "SELECT * FROM plugin_runtime_instance WHERE node_id=%s AND plugin_id=%s "
        "AND release_id=%s AND artifact_digest=%s AND config_hash=%s "
        "AND role IN ('active','previous') AND state='active' AND endpoint<>'' "
        "ORDER BY generation DESC LIMIT 1",
        (
            target_node_id,
            node["plugin_id"],
            node["release_id"],
            node["artifact_digest"],
            node["config_hash"],
        ),
    )


def runtime_is_pinned(conn, runtime):
    identity = {"release_id": runtime["release_id"], "config_hash": runtime["config_hash"]}
    from psycopg.types.json import Jsonb

    return bool(
        one(
            conn,
            "SELECT 1 FROM pipeline_revision r WHERE (r.definition_json->'nodes' @> %s::jsonb "
            "OR EXISTS (SELECT 1 FROM jsonb_array_elements(r.definition_json->'nodes') n "
            "WHERE coalesce(n->'delayed_enrichments','[]'::jsonb) @> %s::jsonb)) "
            "AND (NOT EXISTS (SELECT 1 FROM pipeline_revision_retirement x WHERE "
            "(x.pipeline_id,x.revision)=(r.pipeline_id,r.revision)) OR EXISTS "
            "(SELECT 1 FROM pipeline_run u WHERE "
            "(u.pipeline_id,u.revision)=(r.pipeline_id,r.revision) "
            "AND (u.state IN ('accepted','validating','queued','running') OR EXISTS "
            "(SELECT 1 FROM enrichment_task t WHERE t.run_id=u.run_id "
            "AND t.state='queued')))) LIMIT 1",
            (Jsonb([identity]), Jsonb([identity])),
        )
    )

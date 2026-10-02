"""外部发布者准入与不可变注册；信任状态由服务器决定。"""

from psycopg.types.json import Jsonb

from ..contracts import audit, fail, one


def require_release_trust(conn, release):
    if release["trust"] == "first_party" and release["authenticated"]:
        return
    signer = one(
        conn,
        "SELECT s.revoked_at FROM plugin_registration g JOIN plugin_signer s USING(signer_id) "
        "WHERE g.release_id=%s FOR SHARE OF s",
        (release["release_id"],),
    )
    if not signer or not release["authenticated"]:
        fail(422, "plugin_release_not_authenticated")
    if signer["revoked_at"]:
        fail(403, "plugin_signer_revoked")


def import_registration(conn, descriptor, registration, signer_id, owner, relative_bundle):
    """调用者先验证签名及全部字节；再次锁定签名者避免撤销竞态。"""
    signer = one(conn, "SELECT * FROM plugin_signer WHERE signer_id=%s FOR SHARE", (signer_id,))
    if not signer:
        fail(403, "plugin_signer_unknown")
    if signer["revoked_at"]:
        fail(403, "plugin_signer_revoked")
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (descriptor["plugin_id"],))
    existing = one(
        conn,
        "SELECT * FROM plugin_release WHERE plugin_id=%s AND plugin_version=%s "
        "AND platform=%s AND arch=%s",
        tuple(descriptor[key] for key in ("plugin_id", "plugin_version", "platform", "arch")),
    )
    if existing:
        if (
            existing["bundle_digest"] != descriptor["bundle_digest"]
            or existing["release_id"] != descriptor["release_id"]
        ):
            fail(409, "plugin_release_content_conflict")
        return existing
    columns = (
        "release_id",
        "plugin_id",
        "plugin_version",
        "platform",
        "arch",
        "form",
        "artifact_digest",
        "bundle_digest",
        "manifest_digest",
        "config_schema_digest",
        "sbom_digest",
        "bundle_bytes",
        "entrypoint",
        "runtime_requirements",
        "sbom_components",
        "declared_memory_bytes",
        "declared_cpu_millicores",
        "default_deadline_ms",
    )
    values = [
        Jsonb(descriptor[key]) if key in {"entrypoint", "runtime_requirements"} else descriptor[key]
        for key in columns
    ]
    result = one(
        conn,
        "INSERT INTO plugin_release("
        + ",".join(columns)
        + ",trust,authenticated,authentication_method,signature_status,bundle_path,created_by) "
        + "VALUES ("
        + ",".join(["%s"] * len(columns))
        + ",'trusted_publisher',true,'ed25519','verified',%s,%s) RETURNING *",
        (*values, relative_bundle, owner),
    )
    conn.execute(
        "INSERT INTO plugin_registration(release_id,signer_id,manifest,config_schema,schemas) "
        "VALUES (%s,%s,%s,%s,%s)",
        (
            descriptor["release_id"],
            signer_id,
            Jsonb(registration["manifest"]),
            Jsonb(registration["config_schema"]),
            Jsonb(registration["schemas"]),
        ),
    )
    audit(
        conn,
        owner,
        "plugin.release.import",
        descriptor["release_id"] + ":" + descriptor["bundle_digest"],
    )
    return result

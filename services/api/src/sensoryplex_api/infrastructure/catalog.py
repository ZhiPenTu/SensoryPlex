"""只读发现仓库内 Manifest；源码存在不等同于安装或运行。"""

import json

import yaml

from ..contracts import fail


def catalog(settings, conn=None):
    entries = []
    for path in sorted(settings.plugin_root.glob("*/plugin.yaml"))[:100]:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            fail(503, "catalog_manifest_invalid")
        try:
            manifest = yaml.safe_load(path.read_text())
            meta, spec = manifest["metadata"], manifest["spec"]
            schema_path = path.parent / spec["config"]["schema"]
            if not schema_path.resolve().is_relative_to(path.parent.resolve()):
                fail(503, "catalog_schema_invalid")
            if schema_path.stat().st_size > 64 * 1024:
                fail(503, "catalog_schema_invalid")
            schema = json.loads(schema_path.read_text())
            # Runtime 传输字段不向业务配置表单暴露。
            schema["properties"] = {
                k: v for k, v in schema.get("properties", {}).items() if k != "handoff_endpoint"
            }
            schema["required"] = [k for k in schema.get("required", []) if k != "handoff_endpoint"]
            entries.append(
                {
                    "id": meta["name"],
                    "name": meta.get("displayName", meta["name"]),
                    "version": meta["version"],
                    "description": meta.get("description", ""),
                    "digest": spec["artifacts"]["digest"],
                    "trust": "unverified",
                    "state": "source_available",
                    "reason": "runtime_plugin_installer_not_attached",
                    "consumes": spec["capabilities"]["consumes"],
                    "produces": spec["capabilities"]["produces"],
                    "accepts_memory_kinds": spec["capabilities"].get("acceptsMemoryKinds", []),
                    "form": spec["artifacts"].get("form", "local_native"),
                    "resources": spec.get("resources", {}),
                    "config_schema": schema,
                }
            )
        except (OSError, KeyError, ValueError, yaml.YAMLError, TypeError):
            fail(503, "catalog_manifest_invalid")
    if conn is not None:
        from ..contracts import rows

        registered = rows(
            conn,
            "SELECT r.*,g.manifest,g.config_schema,s.revoked_at "
            "FROM plugin_release r JOIN plugin_registration g USING(release_id) "
            "JOIN plugin_signer s USING(signer_id) "
            "ORDER BY r.plugin_id,r.published_at DESC LIMIT 100",
        )
        registered_ids = {item["plugin_id"] for item in registered}
        entries = [item for item in entries if item["id"] not in registered_ids]
        entries.extend(registration_entry(item) for item in registered)
    return entries


def plugin(settings, key, conn=None, release_id=""):
    # 已发布方案按身份直取，不受目录分页或新版本数量影响。
    if conn is not None and release_id:
        from ..contracts import one

        registered = one(
            conn,
            "SELECT r.*,g.manifest,g.config_schema,s.revoked_at "
            "FROM plugin_release r JOIN plugin_registration g USING(release_id) "
            "JOIN plugin_signer s USING(signer_id) "
            "WHERE r.release_id=%s AND r.plugin_id=%s",
            (release_id, key),
        )
        if registered:
            return registration_entry(registered)
    found = next(
        (
            item
            for item in catalog(settings, conn)
            if item["id"] == key and (not release_id or item.get("release_id") == release_id)
        ),
        None,
    )
    if not found:
        fail(404, "plugin_not_found")
    return found


def registration_entry(row):
    from edge_material_sdk.manifest import contracts

    manifest = row["manifest"]
    meta, spec = manifest["metadata"], manifest["spec"]
    resources = dict(spec["resources"])
    resources["memory"] = str(resources["memoryBytes"])
    resources["cpu"] = str(resources["cpuMillicores"] / 1000)
    return {
        "id": row["plugin_id"],
        "name": meta.get("displayName", row["plugin_id"]),
        "version": row["plugin_version"],
        "description": meta.get("description", ""),
        "digest": row["artifact_digest"],
        "trust": row["trust"],
        "state": "revoked" if row["revoked_at"] else "registered",
        "reason": "plugin_signer_revoked" if row["revoked_at"] else "",
        "release_id": row["release_id"],
        "manifest_version": manifest["apiVersion"],
        "form": row["form"],
        "resources": resources,
        "config_schema": row["config_schema"],
        "consumes": [item["modality"] for item in spec["inputs"]],
        "produces": [item["modality"] for item in spec["outputs"]],
        "accepts_memory_kinds": ["cpu_shared_memory"]
        if any(item["modality"].startswith("media.") for item in spec["inputs"])
        else [],
        "input_contracts": contracts(manifest, "inputs"),
        "output_contracts": contracts(manifest, "outputs"),
        "execution_modes": spec["executionModes"],
        "output_text_fields": {d["modality"]: d.get("textFields", []) for d in spec["outputs"]},
    }

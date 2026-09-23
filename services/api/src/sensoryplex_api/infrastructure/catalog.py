"""只读发现仓库内 Manifest；源码存在不等同于安装或运行。"""

import json

import yaml

from ..contracts import fail


def catalog(settings):
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
                    "config_schema": schema,
                }
            )
        except (OSError, KeyError, ValueError, yaml.YAMLError, TypeError):
            fail(503, "catalog_manifest_invalid")
    return entries


def plugin(settings, key):
    found = next((item for item in catalog(settings) if item["id"] == key), None)
    if not found:
        fail(404, "plugin_not_found")
    return found

"""Console 与一键装配共享配置校验、不可变版本保存和幂等复用。"""

import hashlib
import json

from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from ..contracts import audit, fail, identifier, one

# 方案保存完整语义配置，不绑定临时 handoff 端口或宿主模型目录。
MULTIMODAL_CONFIG_DEFAULTS = {
    "org.sensoryplex.ocr-rapidocr": {
        "data_plane_mode": "per_request",
        "provider": "cpu",
        "model_id": "PP-OCRv6_mobile",
        "model_revision": "",
        "text_score": 0.5,
        "timeout_s": 120.0,
        "ttl_ms": 30_000,
    },
    "org.sensoryplex.asr-whisper-mlx": {
        "data_plane_mode": "per_request",
        "model": "mlx-community/whisper-large-v3-turbo",
        "model_revision": "",
        "language": None,
        "task": "transcribe",
        "word_timestamps": False,
        "timeout_s": 300.0,
        "ttl_ms": 30_000,
    },
    "org.sensoryplex.vlm-moondream": {
        "endpoint": "http://127.0.0.1:11434",
        "model": "moondream:v2",
        "prompt": "Describe what is visible in this image in one sentence.",
        "data_plane_mode": "local_decode",
        "min_free_memory_bytes": 268_435_456,
        "timeout_s": 180.0,
        "ttl_ms": 30_000,
    },
    "org.sensoryplex.vlm-vllm": {
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "",
        "max_concurrency": 2,
        "model": "",
        "prompt": "Describe what is visible in this image in one sentence.",
        "data_plane_mode": "static",
        "timeout_s": 180.0,
        "ttl_ms": 30_000,
    },
}


def normalize_configuration(entry: dict, config: dict) -> dict:
    """拒绝不可发布的配置，并恢复 protobuf Struct 丢失的整数类型。"""
    schema = dict(entry["config_schema"])
    schema["properties"] = {
        k: v for k, v in schema["properties"].items() if k != "handoff_endpoint"
    }
    schema["required"] = [k for k in schema.get("required", []) if k != "handoff_endpoint"]
    defaults = MULTIMODAL_CONFIG_DEFAULTS.get(entry["id"])
    if defaults:
        if "model_dir" in config:
            fail(422, "console_plugin_config_host_path_forbidden")
        expected_mode = defaults["data_plane_mode"]
        if (
            entry["id"] != "org.sensoryplex.vlm-vllm"
            and config.get("data_plane_mode", expected_mode) != expected_mode
        ):
            fail(
                422,
                "console_vlm_config_requires_local_decode"
                if expected_mode == "local_decode"
                else "console_plugin_config_requires_per_request_data_plane",
            )
        config = {**defaults, **config}
    if list(Draft202012Validator(schema).iter_errors(config)):
        fail(422, "plugin_config_invalid")
    if entry["id"] == "org.sensoryplex.vlm-moondream":
        if config.get("endpoint", "http://127.0.0.1:11434") != "http://127.0.0.1:11434":
            fail(422, "only_local_model_endpoint_allowed")
    if (
        not 1 <= config.get("timeout_s", 180) <= 300
        or len(config.get("prompt", "")) > 4000
        or len(config.get("model", "")) > 200
    ):
        fail(422, "plugin_config_limits_exceeded")
    if defaults:
        for key, default in defaults.items():
            if type(default) is int:
                config[key] = int(config[key])
            elif type(default) is float:
                config[key] = float(config[key])
    return config


def configuration_hash(config: dict) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def save_configuration(conn, *, plugin_id, name, config, created_by, reuse=False):
    """调用方负责校验；装配按内容复用，手动保存仍追加不可变版本。"""
    digest = configuration_hash(config)
    # 按插件串行化内容复用和命名版本递增，避免不同节点同时装配产生重复记录。
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (plugin_id,))
    if reuse:
        existing = one(
            conn,
            "SELECT * FROM console_plugin_config WHERE plugin_id=%s AND config_hash=%s "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (plugin_id, digest),
        )
        if existing:
            return existing
    revision = conn.execute(
        "SELECT coalesce(max(revision),0)+1 FROM console_plugin_config "
        "WHERE plugin_id=%s AND name=%s",
        (plugin_id, name),
    ).fetchone()[0]
    result = one(
        conn,
        "INSERT INTO console_plugin_config "
        "(id,plugin_id,name,revision,config,config_hash,created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (identifier("config"), plugin_id, name, revision, Jsonb(config), digest, created_by),
    )
    audit(conn, created_by, "plugin.config.save", result["id"])
    return result


def save_deployment_configuration(conn, entry, config, created_by):
    """补齐部署实际使用的多模态配置；不把不兼容的旧配置静默改成另一份。"""
    if entry["id"] not in MULTIMODAL_CONFIG_DEFAULTS:
        return
    normalized = normalize_configuration(entry, config)
    if configuration_hash(normalized) != configuration_hash(config):
        fail(422, "deployed_plugin_configuration_not_publishable")
    return save_configuration(
        conn,
        plugin_id=entry["id"],
        name="一键装配配置",
        config=config,
        created_by=created_by,
        reuse=True,
    )

"""文件多模态方案的受限编译器。

这里不接受命令、URL、宿主路径或密钥。调用方只能选择已发现的首方处理器及其不可变配置，
随后编译为 ADR-029 的 Revision 图。
"""

import hashlib
import json
from typing import Any

from ..contracts import fail, one
from . import orchestration
from .catalog import plugin

WINDOW_MS = 1_000
MAX_POLICY_INTERVAL_MS = 60_000
RUNTIME_TIMELINE_PLUGIN = "org.sensoryplex.runtime.timeline-fusion"
RUNTIME_TIMELINE_VERSION = "1.0.0"
RUNTIME_TIMELINE_DIGEST = (
    "sha256:" + hashlib.sha256(b"sensoryplex:runtime:timeline-fusion:1.0.0").hexdigest()
)

COMPONENTS: dict[str, dict[str, Any]] = {
    "ocr_fast": {
        "plugin_id": "org.sensoryplex.ocr-rapidocr",
        "consumes": ["media.video_frame"],
        "produces": ["observation.ocr_blocks"],
        "required": True,
        "deadline_ms": 120_000,
        "max_attempts": 2,
    },
    "asr_fast": {
        "plugin_id": "org.sensoryplex.asr-whisper-mlx",
        "consumes": ["media.audio_segment"],
        "produces": ["observation.asr_segment"],
        "required": True,
        "deadline_ms": 180_000,
        "max_attempts": 2,
    },
    "vlm_enrich": {
        "plugin_id": "org.sensoryplex.vlm-moondream",
        "consumes": ["media.video_frame"],
        "produces": ["observation.vision.scene_description"],
        "required": False,
        "deadline_ms": 180_000,
        "max_attempts": 2,
    },
}


def _sha256_json(value: dict[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _bounded_int(value: Any, *, default: int, lower: int, upper: int, code: str) -> int:
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError):
        fail(422, code)
    if result < lower or result > upper:
        fail(422, code)
    return result


def _policy(raw: object) -> dict[str, int]:
    """策略进入 Revision 摘要；1 秒 Coverage 与采样阈值不再依赖全局 YAML。"""
    value = raw if isinstance(raw, dict) else {}
    window_ms = _bounded_int(
        value.get("window_ms"),
        default=WINDOW_MS,
        lower=WINDOW_MS,
        upper=WINDOW_MS,
        code="multimodal_window_ms_must_be_1000",
    )
    return {
        "window_ms": window_ms,
        "sample_interval_ms": _bounded_int(
            value.get("sample_interval_ms"),
            default=WINDOW_MS,
            lower=250,
            upper=MAX_POLICY_INTERVAL_MS,
            code="invalid_multimodal_sample_interval",
        ),
        "audio_segment_ms": _bounded_int(
            value.get("audio_segment_ms"),
            default=6_000,
            lower=1_000,
            upper=60_000,
            code="invalid_multimodal_audio_segment",
        ),
        "audio_overlap_ms": _bounded_int(
            value.get("audio_overlap_ms"),
            default=500,
            lower=0,
            upper=59_000,
            code="invalid_multimodal_audio_overlap",
        ),
        "vlm_sample_interval_ms": _bounded_int(
            value.get("vlm_sample_interval_ms"),
            default=5_000,
            lower=1_000,
            upper=MAX_POLICY_INTERVAL_MS,
            code="invalid_multimodal_vlm_sample_interval",
        ),
    }


def build_graph(
    conn, settings, body: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """将受限组件列表编译为固定的文件多模态 DAG，并锁定插件与配置身份。"""
    raw_components = body.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        fail(422, "multimodal_components_required")
    if len(raw_components) > len(COMPONENTS):
        fail(422, "invalid_multimodal_component_count")
    policy = _policy(body.get("policy"))
    selected: dict[str, dict[str, Any]] = {}

    for raw in raw_components:
        if not isinstance(raw, dict):
            fail(422, "invalid_multimodal_component")
        node_id = str(raw.get("node_id", "")).strip()
        blueprint = COMPONENTS.get(node_id)
        if not blueprint or node_id in selected:
            fail(422, "invalid_multimodal_component")
        if raw.get("plugin_id") not in (None, "", blueprint["plugin_id"]):
            fail(422, "multimodal_component_plugin_mismatch")
        config_id = str(raw.get("config_id", "")).strip()
        config = one(
            conn,
            "SELECT id,plugin_id,config_hash FROM console_plugin_config WHERE id=%s",
            (config_id,),
        )
        if not config or config["plugin_id"] != blueprint["plugin_id"]:
            fail(422, "multimodal_component_config_not_found")
        entry = plugin(settings, blueprint["plugin_id"])
        # Revision 不绑定“目录中当前的能力猜测”：plugin manifest 与蓝图两者都须一致。
        if sorted(entry["consumes"]) != sorted(blueprint["consumes"]) or sorted(
            entry["produces"]
        ) != sorted(blueprint["produces"]):
            fail(422, "multimodal_component_capability_mismatch")
        required = bool(raw.get("required", blueprint["required"]))
        if node_id != "vlm_enrich" and not required:
            fail(422, "multimodal_fast_path_must_be_required")
        selected[node_id] = {
            "id": node_id,
            "plugin_id": blueprint["plugin_id"],
            "plugin_version": entry["version"],
            "artifact_digest": entry["digest"],
            "config_id": config["id"],
            "config_hash": config["config_hash"],
            "consumes": list(blueprint["consumes"]),
            "produces": list(blueprint["produces"]),
            "placement": "data_plane_local",
            "deadline_ms": _bounded_int(
                raw.get("deadline_ms"),
                default=blueprint["deadline_ms"],
                lower=1_000,
                upper=300_000,
                code="invalid_multimodal_deadline",
            ),
            "max_attempts": _bounded_int(
                raw.get("max_attempts"),
                default=blueprint["max_attempts"],
                lower=1,
                upper=16,
                code="invalid_multimodal_attempt_budget",
            ),
            "priority": 0 if required else -10,
            "required": required,
        }

    if not {"ocr_fast", "asr_fast"}.issubset(selected):
        fail(422, "multimodal_ocr_and_asr_required")
    if policy["audio_overlap_ms"] >= policy["audio_segment_ms"]:
        fail(422, "invalid_multimodal_audio_overlap")

    timeline_consumes = ["observation.ocr_blocks", "observation.asr_segment"]
    if "vlm_enrich" in selected:
        timeline_consumes.append("observation.vision.scene_description")
    nodes = [selected[key] for key in sorted(selected)]
    nodes.append(
        {
            "id": "timeline_fusion",
            "plugin_id": RUNTIME_TIMELINE_PLUGIN,
            "plugin_version": RUNTIME_TIMELINE_VERSION,
            "artifact_digest": RUNTIME_TIMELINE_DIGEST,
            "config_hash": _sha256_json(policy),
            "consumes": timeline_consumes,
            "produces": ["material.unit"],
            "placement": "data_plane_local",
            "deadline_ms": 60_000,
            "max_attempts": 2,
            "priority": 10,
            "required": True,
            # 策略属于不可变 Revision，不能让执行器在全局 YAML 里重新猜一次。
            "execution_policy": policy,
        }
    )
    edges = [
        {
            "from_node_id": "ocr_fast",
            "to_node_id": "timeline_fusion",
            "modality": "observation.ocr_blocks",
            "join_policy": "same_stream_window",
            "required": True,
        },
        {
            "from_node_id": "asr_fast",
            "to_node_id": "timeline_fusion",
            "modality": "observation.asr_segment",
            "join_policy": "same_stream_window",
            "required": True,
        },
    ]
    if "vlm_enrich" in selected:
        edges.append(
            {
                "from_node_id": "vlm_enrich",
                "to_node_id": "timeline_fusion",
                "modality": "observation.vision.scene_description",
                "join_policy": "same_stream_window",
                "required": False,
            }
        )
    valid, errors, _, _, _ = orchestration.validate_and_normalize_graph(nodes, edges)
    if not valid:
        fail(422, errors[0])
    return nodes, edges, policy

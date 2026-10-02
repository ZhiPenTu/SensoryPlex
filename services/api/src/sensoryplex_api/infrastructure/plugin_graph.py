"""按已验证注册能力编译插件图，发布时锁定全部身份。"""

import re

from ..contracts import fail, one
from .catalog import plugin
from .plugin_configurations import configuration_hash, normalize_configuration
from .plugin_registry import require_release_trust


def build_graph(conn, settings, body):
    from . import multimodal

    raw_nodes, raw_edges = body.get("nodes"), body.get("edges", [])
    if (
        not isinstance(raw_nodes, list)
        or not 1 <= len(raw_nodes) <= 32
        or not isinstance(raw_edges, list)
        or len(raw_edges) > 128
    ):
        fail(422, "plugin_graph_size_rejected")
    policy = multimodal._policy(body.get("policy"))
    selected = {}
    for raw in raw_nodes:
        node_id = raw.get("id", raw.get("node_id", ""))
        if (
            not isinstance(node_id, str)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", node_id)
            or node_id == "timeline_fusion"
            or node_id in selected
        ):
            fail(422, "plugin_graph_node_identity_invalid")
        release = one(
            conn, "SELECT * FROM plugin_release WHERE release_id=%s", (raw.get("release_id", ""),)
        )
        if not release:
            fail(422, "plugin_release_not_found")
        require_release_trust(conn, release)
        entry = plugin(settings, release["plugin_id"], conn, release["release_id"])
        if entry.get("manifest_version") != "edge.material.plugin/v2":
            fail(422, "plugin_graph_registered_v2_required")
        config = one(
            conn,
            "SELECT * FROM console_plugin_config WHERE id=%s AND plugin_id=%s",
            (raw.get("config_id", ""), release["plugin_id"]),
        )
        if not config:
            fail(422, "plugin_config_not_found")
        if (
            configuration_hash(normalize_configuration(entry, config["config"]))
            != config["config_hash"]
        ):
            fail(422, "plugin_config_schema_mismatch")
        mode = raw.get("execution_mode", "sync")
        if mode not in entry["execution_modes"]:
            fail(422, "plugin_execution_mode_unsupported")
        selector = raw.get("input_selector", "media")
        if not isinstance(selector, str) or not (
            selector == "media" or selector.startswith("node:")
        ):
            fail(422, "plugin_input_selector_invalid")
        if selector == "media" and not set(entry["consumes"]) <= {
            "media.video_frame",
            "media.audio_segment",
        }:
            fail(422, "plugin_input_selector_type_mismatch")
        if len(entry["consumes"]) != 1:
            fail(422, "plugin_input_mixed_types_unsupported")
        selected[node_id] = {
            "id": node_id,
            "release_id": release["release_id"],
            "plugin_id": release["plugin_id"],
            "plugin_version": release["plugin_version"],
            "artifact_digest": release["artifact_digest"],
            "config_id": config["id"],
            "config_hash": config["config_hash"],
            "consumes": entry["consumes"],
            "produces": entry["produces"],
            "input_contracts": entry["input_contracts"],
            "output_contracts": entry["output_contracts"],
            "execution_mode": mode,
            "input_selector": selector,
            "placement": "data_plane_local",
            "deadline_ms": release["default_deadline_ms"],
            "max_attempts": multimodal._bounded_int(
                raw.get("max_attempts"),
                default=2,
                lower=1,
                upper=16,
                code="invalid_orchestration_attempt_budget",
            ),
            "priority": 0,
            "required": mode == "sync",
        }
    edges = []
    for node in selected.values():
        if node["input_selector"] == "media":
            continue
        upstream_id = node["input_selector"][5:]
        upstream = selected.get(upstream_id)
        if (
            not upstream
            or upstream["execution_mode"] != "sync"
            or not set(node["consumes"]) <= set(upstream["produces"])
        ):
            fail(422, "plugin_graph_input_not_reachable")
        for contract in node["input_contracts"]:
            if contract not in upstream["output_contracts"]:
                fail(422, "plugin_graph_schema_mismatch")
        if node["execution_mode"] == "sync":
            for modality in node["consumes"]:
                edges.append(
                    {
                        "from_node_id": upstream_id,
                        "to_node_id": node["id"],
                        "modality": modality,
                        "join_policy": "same_stream_window",
                        "required": True,
                    }
                )
    # 边表单必须与受控输入选择一致，不能从页面附加未锁定的依赖。
    for edge in raw_edges:
        normalized = {
            "from_node_id": edge.get("from_node_id", edge.get("from")),
            "to_node_id": edge.get("to_node_id", edge.get("to")),
            "modality": edge.get("modality"),
            "join_policy": edge.get("join_policy", "same_stream_window"),
            "required": bool(edge.get("required", True)),
        }
        if normalized not in edges:
            fail(422, "plugin_graph_edge_selector_mismatch")
    sync = [node for node in selected.values() if node["execution_mode"] == "sync"]
    if not sync or not any(node["input_selector"] == "media" for node in sync):
        fail(422, "plugin_graph_sync_media_source_required")
    delayed = [node for node in selected.values() if node["execution_mode"] != "sync"]
    outputs = sorted({value for node in sync for value in node["produces"]})
    policy.update(
        {
            "generic_plugin_graph": True,
            "fast_modalities": outputs,
            "enrichment_modalities": sorted(
                {value for node in delayed for value in node["produces"]}
            ),
        }
    )
    return compile_graph(sync, delayed, policy, edges)


def compile_graph(sync, delayed, policy, edges=None):
    """预置组合和注册插件共用 Timeline/DAG 编译，兼容旧组件的执行元数据。"""
    from . import multimodal, orchestration

    edges = list(edges or [])
    outputs = sorted({value for node in sync for value in node["produces"]})
    timeline = {
        "id": "timeline_fusion",
        "plugin_id": multimodal.RUNTIME_TIMELINE_PLUGIN,
        "plugin_version": multimodal.RUNTIME_TIMELINE_VERSION,
        "artifact_digest": multimodal.RUNTIME_TIMELINE_DIGEST,
        "config_hash": multimodal._sha256_json(policy),
        "consumes": outputs,
        "produces": ["material.unit"],
        "placement": "data_plane_local",
        "deadline_ms": 60000,
        "max_attempts": 2,
        "priority": 10,
        "required": True,
        "execution_policy": policy,
        "delayed_enrichments": delayed,
    }
    for node in sync:
        for modality in node["produces"]:
            edges.append(
                {
                    "from_node_id": node["id"],
                    "to_node_id": "timeline_fusion",
                    "modality": modality,
                    "join_policy": "same_stream_window",
                    "required": True,
                }
            )
    nodes = [*sync, timeline]
    valid, errors, _, _, _ = orchestration.validate_and_normalize_graph(nodes, edges)
    if not valid:
        fail(422, errors[0])
    return nodes, edges, policy

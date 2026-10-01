"""SensoryPlex Model Context Protocol (MCP) Server.

Exposes SensoryPlex multimodal video understanding and edge platform capabilities
as standardized MCP tools, resources, and prompts for AI assistants (Claude, Codex, Cursor).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

try:
    from mcp.server import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

from .client import (
    SensoryPlexAPIError,
    SensoryPlexClient,
    SensoryPlexNotFoundError,
)

# Crucial: All diagnostic logs MUST go to stderr so stdout remains clean for stdio JSON-RPC.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("sensoryplex.mcp")

# Global client instance initialized on demand
_CLIENT: SensoryPlexClient | None = None


def get_client() -> SensoryPlexClient:
    """Get or create the global SensoryPlexClient."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = SensoryPlexClient()
    return _CLIENT


def format_ms(ms: int) -> str:
    """Format milliseconds into HH:MM:SS.mmm."""
    seconds, msec = divmod(ms, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minute:02d}:{sec:02d}.{msec:03d}"
    return f"{minute:02d}:{sec:02d}.{msec:03d}"


server = MCPServer(
    name="sensoryplex",
    instructions=(
        "SensoryPlex Multimodal Video Understanding Platform MCP Server. "
        "Use this server to search video contents (OCR visual text, ASR audio transcripts, "
        "VLM scene descriptions), inspect material facts and bounding boxes, generate video "
        "playback URLs, dispatch processing jobs, and inspect cluster node topology."
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Platform & System Health Tools
# ─────────────────────────────────────────────────────────────────────────────
@server.tool()
async def get_system_status() -> str:
    """Check SensoryPlex platform health, schema version, and capability readiness.

    Returns operational status of the platform, including whether keyword search and
    vector semantic search are currently available.
    """
    client = get_client()
    try:
        health = await client.get_health()
        caps = await client.get_capabilities()
        info = {
            "health": health,
            "capabilities": caps,
            "base_url": client.base_url,
        }
        return json.dumps(info, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error querying SensoryPlex system status: {exc}"


@server.tool()
async def get_audit_events(limit: int = 20, offset: int = 0) -> str:
    """Retrieve platform security and administrative audit log events.

    Args:
        limit: Maximum number of audit events to return (1-100).
        offset: Offset into audit log entries.
    """
    client = get_client()
    try:
        events = await client.get_audit_events(limit=limit, offset=offset)
        return json.dumps(events, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error retrieving audit events: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Multimodal Material Search & Inspection Tools
# ─────────────────────────────────────────────────────────────────────────────
@server.tool()
async def search_materials(
    query: str,
    mode: str = "keyword",
    limit: int = 20,
    start_ms: int | None = None,
    end_ms: int | None = None,
    min_confidence: float | None = None,
    modalities: list[str] | None = None,
) -> str:
    """Search multimodal video materials by text, speech, or visual recognition.

    Supports two search modes:
    - 'keyword': Substring search across transcribed text and OCR facts.
    - 'semantic': Vector embedding semantic search against indexed materials.

    Args:
        query: Search keywords or natural language question.
        mode: Search mode, either 'keyword' or 'semantic'.
        limit: Number of results to return (max 100).
        start_ms: Filter materials occurring after this millisecond offset.
        end_ms: Filter materials occurring before this millisecond offset.
        min_confidence: Minimum observation confidence threshold (0.0 to 1.0).
        modalities: Optional list of modalities to filter by (e.g. ['ocr', 'asr', 'vlm']).
    """
    client = get_client()
    try:
        raw_result = await client.search_materials(
            query=query,
            mode=mode,
            limit=limit,
            start_ms=start_ms,
            end_ms=end_ms,
            min_confidence=min_confidence,
            modalities=modalities,
        )

        materials_list = raw_result.get("materials", [])
        hits = raw_result.get("hits", [])

        formatted_items = []
        for idx, m in enumerate(materials_list):
            unit_id = m.get("material_unit_id") or m.get("key", f"item-{idx}")
            s_ms = m.get("start_ms", 0)
            e_ms = m.get("end_ms", 0)
            stream_id = m.get("stream_id", "")
            observations = m.get("observations", [])

            # Extract human-readable text facts from observations
            facts = []
            for obs in observations:
                modality = obs.get("modality", "")
                conf = obs.get("confidence", 0.0)
                payload = obs.get("payload_jsonb") or obs.get("payload", {})
                text_content = ""
                if isinstance(payload, dict):
                    text_content = (
                        payload.get("text")
                        or payload.get("transcript")
                        or payload.get("description", "")
                    )
                elif isinstance(payload, str):
                    text_content = payload
                facts.append(
                    {
                        "modality": modality,
                        "confidence": round(conf, 3) if conf else None,
                        "text": text_content,
                    }
                )

            item_info = {
                "material_unit_id": unit_id,
                "time_range": f"{format_ms(s_ms)} -> {format_ms(e_ms)} ({s_ms}ms..{e_ms}ms)",
                "stream_id": stream_id,
                "observations_count": len(observations),
                "facts": facts,
            }
            if idx < len(hits):
                item_info["search_hit_score"] = round(hits[idx].get("score", 0.0), 4)

            formatted_items.append(item_info)

        summary = {
            "query": query,
            "mode": raw_result.get("mode", mode),
            "total_matches": len(materials_list),
            "results": formatted_items,
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)
    except SensoryPlexAPIError as exc:
        return f"SensoryPlex search failed [{exc.status_code}]: {exc.detail}"
    except Exception as exc:
        return f"Unexpected error during search: {exc}"


@server.tool()
async def get_material_detail(
    key: str,
    revision: int | None = None,
    execution_id: str = "",
) -> str:
    """Retrieve full observation details and source references for a specific MaterialUnit.

    Returns the complete set of multimodal observations (OCR bounding boxes, ASR transcripts,
    scene descriptions), confidence scores, and source video file references.

    Args:
        key: The MaterialUnit ID or key.
        revision: Optional specific revision number.
        execution_id: Optional execution scope identifier.
    """
    client = get_client()
    try:
        data = await client.get_material(key=key, revision=revision, execution_id=execution_id)
        return json.dumps(data, ensure_ascii=False, indent=2)
    except SensoryPlexNotFoundError:
        return f"Material '{key}' not found."
    except Exception as exc:
        return f"Error retrieving material '{key}': {exc}"


@server.tool()
async def get_timeline_coverage(
    execution_id: str,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Fetch 1-second grid slice coverage for a video processing execution.

    Allows AI assistants to inspect which temporal slices have verified observations,
    which are awaiting background VLM completion, or which have empty detection.

    Args:
        execution_id: The job execution ID.
        limit: Number of window slices to retrieve (default 100).
        offset: Offset into the second-window slice array.
    """
    client = get_client()
    try:
        timeline_data = await client.get_execution_timeline(execution_id=execution_id)
        return json.dumps(timeline_data, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error retrieving timeline coverage for execution '{execution_id}': {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Video Assets & Playback Tools
# ─────────────────────────────────────────────────────────────────────────────
@server.tool()
async def list_media_assets(limit: int = 50, offset: int = 0) -> str:
    """List uploaded video assets in SensoryPlex.

    Returns asset IDs, file names, content sha256 hashes, durations, file sizes,
    and admission readiness states.

    Args:
        limit: Maximum number of assets to return.
        offset: Offset into asset list.
    """
    client = get_client()
    try:
        uploads = await client.list_uploads(limit=limit, offset=offset)
        items = uploads.get("items", [])
        formatted = []
        for u in items:
            dur = u.get("duration_ms", 0)
            formatted.append(
                {
                    "asset_id": u.get("id"),
                    "filename": u.get("filename"),
                    "sha256": u.get("sha256"),
                    "state": u.get("state"),
                    "size_bytes": u.get("size_bytes"),
                    "duration": f"{format_ms(dur)} ({dur}ms)" if dur else "pending_admission",
                    "content_type": u.get("content_type"),
                }
            )
        payload = {"total": uploads.get("total", len(formatted)), "assets": formatted}
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error listing media assets: {exc}"


@server.tool()
async def get_playback_info(
    asset_id: str,
    start_ms: int = 0,
    end_ms: int | None = None,
) -> str:
    """Generate HTTP Range video streaming playback URL and clip review guidance.

    Returns the direct stream URL that browsers, media players, or Web console can play,
    annotated with the requested time offset.

    Args:
        asset_id: Video asset identifier (e.g. 'upload_...' or 'asset_...').
        start_ms: Optional start offset in milliseconds.
        end_ms: Optional end offset in milliseconds.
    """
    client = get_client()
    stream_url = client.get_playback_stream_url(asset_id)
    time_label = f"{format_ms(start_ms)}" + (f" to {format_ms(end_ms)}" if end_ms else "")
    info = {
        "asset_id": asset_id,
        "stream_url": stream_url,
        "time_range": time_label,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "playback_notes": (
            "This URL supports HTTP 206 Partial Content Range streaming. "
            "You can provide this link to the user to directly review or verify the video clip."
        ),
    }
    return json.dumps(info, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline Orchestration & Job Processing Tools
# ─────────────────────────────────────────────────────────────────────────────
@server.tool()
async def list_pipelines(limit: int = 50, offset: int = 0) -> str:
    """List available multimodal processing DAG pipelines.

    Returns pipeline IDs, human-readable names, revisions, execution modes,
    and descriptions.

    Args:
        limit: Maximum number of pipelines to return.
        offset: Offset into pipeline list.
    """
    client = get_client()
    try:
        res = await client.list_pipelines(limit=limit, offset=offset)
        items = res.get("items", [])
        payload = {"total": res.get("total", len(items)), "pipelines": items}
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error listing pipelines: {exc}"


@server.tool()
async def submit_job_run(
    asset_id: str,
    pipeline_id: str,
    name: str = "",
    node_id: str | None = None,
) -> str:
    """Dispatch a new video processing job on an uploaded asset with a selected pipeline.

    Args:
        asset_id: Uploaded video asset ID (from list_media_assets).
        pipeline_id: Pipeline ID to execute (from list_pipelines).
        name: Optional custom display name for the job.
        node_id: Optional specific compute node ID to dispatch to.
    """
    client = get_client()
    try:
        job = await client.create_and_dispatch_job(
            asset_id=asset_id,
            pipeline_id=pipeline_id,
            name=name,
            node_id=node_id,
        )
        return json.dumps(job, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error dispatching job for asset '{asset_id}': {exc}"


@server.tool()
async def get_job_run_status(
    run_id: str = "",
    execution_id: str = "",
) -> str:
    """Track pipeline run execution state, task status, and modality progress.

    Provide either run_id or execution_id to inspect progress.
    States: 'pending', 'running', 'ready_for_review', 'succeeded', 'failed'.

    Args:
        run_id: The pipeline run ID.
        execution_id: The job execution ID.
    """
    client = get_client()
    try:
        output: dict[str, Any] = {}
        if execution_id:
            exec_data = await client.get_execution(execution_id)
            output["execution"] = exec_data
            if not run_id and "run_id" in exec_data.get("execution", {}):
                run_id = exec_data["execution"]["run_id"]

        if run_id:
            run_data = await client.get_run(run_id)
            tasks_data = await client.get_run_tasks(run_id)
            output["run"] = run_data
            output["tasks"] = tasks_data

        if not output:
            return "Please provide either run_id or execution_id."

        return json.dumps(output, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error fetching run status: {exc}"


@server.tool()
async def cancel_job_run(run_id: str) -> str:
    """Cancel an active pipeline run and cascading subtasks.

    Args:
        run_id: The pipeline run ID to cancel.
    """
    client = get_client()
    try:
        res = await client.cancel_run(run_id)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error cancelling run '{run_id}': {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Cluster Nodes & Catalog Tools (Admin)
# ─────────────────────────────────────────────────────────────────────────────
@server.tool()
async def list_nodes(limit: int = 50) -> str:
    """List cluster compute nodes, online health, hardware accelerators, and resource metrics.

    Inspects node topology (macOS, Linux, GPU/NPU accelerators like Apple Silicon Metal,
    NVIDIA CUDA, CoreML) and assigned tasks.

    Args:
        limit: Maximum number of nodes to return.
    """
    client = get_client()
    try:
        nodes = await client.list_nodes(limit=limit)
        return json.dumps(nodes, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error listing compute nodes: {exc}"


@server.tool()
async def approve_candidate_node(node_id: str) -> str:
    """Approve an enrolling candidate worker node to join the cluster.

    Args:
        node_id: The candidate node ID to approve.
    """
    client = get_client()
    try:
        res = await client.approve_node(node_id)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error approving node '{node_id}': {exc}"


@server.tool()
async def list_plugin_catalog() -> str:
    """List registered multimodal model processor plugins in the platform catalog."""
    client = get_client()
    try:
        catalog = await client.get_catalog()
        return json.dumps(catalog, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error retrieving plugin catalog: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# MCP Resource & Prompt Templates
# ─────────────────────────────────────────────────────────────────────────────
@server.resource("sensoryplex://system/status")
async def system_status_resource() -> str:
    """Live JSON status of the SensoryPlex multimodal platform."""
    return await get_system_status()


@server.prompt()
def multimodal_video_qa(asset_id: str, question: str) -> str:
    """Guide the assistant to answer questions by searching SensoryPlex multimodal materials."""
    lines = [
        "You are a multimodal video analysis assistant using SensoryPlex.",
        f"Target video asset: {asset_id}",
        f"User question: {question}",
        "",
        "Instructions:",
        "1. Use `search_materials` with relevant keywords to locate corresponding time segments.",
        "2. Inspect observations with `get_material_detail` if bounding boxes are needed.",
        "3. Generate a playback link with `get_playback_info` so the user can verify the clip.",
        "4. Summarize your answer with clear timestamps and verifiable evidence.",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entrypoint
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="SensoryPlex MCP Server")
    parser.add_argument(
        "--base-url",
        default=os.getenv("SENSORYPLEX_BASE_URL", "http://127.0.0.1:8091"),
        help="SensoryPlex API Base URL (default: http://127.0.0.1:8091)",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("SENSORYPLEX_API_TOKEN", ""),
        help="SensoryPlex API Bearer Token",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    args = parser.parse_args()

    # Configure client settings
    global _CLIENT
    _CLIENT = SensoryPlexClient(base_url=args.base_url, api_token=args.token or None)

    LOGGER.info(
        "Starting SensoryPlex MCP Server (base_url=%s, transport=%s)",
        args.base_url,
        args.transport,
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()

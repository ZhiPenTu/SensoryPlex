# SensoryPlex MCP Server

Model Context Protocol (MCP) server for SensoryPlex multimodal video understanding and edge processing platform.

Allows AI assistants (Claude Desktop, Claude Code, Codex CLI, Codex Desktop, Cursor) to interact with SensoryPlex directly:
- **Multimodal Video Search**: Locate text, speech, and scene events in videos using keyword or vector semantic search.
- **Fact Inspection**: Inspect OCR bounding boxes, ASR transcripts, and VLM scene descriptions with millisecond precision.
- **Range Stream Playback**: Generate HTTP 206 stream URLs for in-browser video verification.
- **Job Orchestration**: Dispatch and monitor multimodal processing pipelines.
- **Cluster Diagnostics**: Inspect edge worker nodes, hardware accelerators (Metal, CUDA, CoreML), and plugin catalog.

---

## Quick Start

### 1. Run Directly with `uv`

```bash
# Using uvx from local checkout
uvx --from /path/to/SensoryPlex/services/mcp-server sensoryplex-mcp

# Or within the virtual environment
cd /path/to/SensoryPlex/services/mcp-server
uv run sensoryplex-mcp
```

### 2. Configure in Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "sensoryplex": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/SensoryPlex/services/mcp-server",
        "sensoryplex-mcp"
      ],
      "env": {
        "SENSORYPLEX_BASE_URL": "http://127.0.0.1:8091",
        "SENSORYPLEX_API_TOKEN": "YOUR_SENSORYPLEX_API_TOKEN"
      }
    }
  }
}
```

> **Zero-Config Tip**: In local development with demo mode (`make demo-seed`), `SENSORYPLEX_API_TOKEN` can be omitted—the server will automatically discover and use the local demo account session.

### 3. Configure in Codex CLI

```bash
codex mcp add sensoryplex -- uv run --directory /path/to/SensoryPlex/services/mcp-server sensoryplex-mcp
```

Or run the automated setup helper:
```bash
python tools/setup_mcp.py --auto
```

---

## Available MCP Tools

| Tool | Category | Description |
| :--- | :--- | :--- |
| `get_system_status` | System | Check platform health, schema version, and search capabilities |
| `search_materials` | Retrieval | Search video materials by keyword or vector semantic query |
| `get_material_detail` | Retrieval | Fetch full observation facts, bounding boxes, and time ranges |
| `get_timeline_coverage` | Timeline | Inspect continuous 1-second grid slice coverage for an execution |
| `list_media_assets` | Media | List uploaded videos, durations, and admission status |
| `get_playback_info` | Media | Get HTTP Range playback stream URL with clip time range |
| `list_pipelines` | Orchestration | List available DAG processing pipelines |
| `submit_job_run` | Orchestration | Dispatch a video processing job on an asset |
| `get_job_run_status` | Orchestration | Track pipeline run execution state and task progress |
| `cancel_job_run` | Orchestration | Cancel an ongoing pipeline execution |
| `list_nodes` | Topology | List cluster nodes, online status, accelerators, and memory |
| `approve_candidate_node`| Topology | Approve an enrolling worker node |
| `list_plugin_catalog` | Catalog | List available model processor plugins |
| `get_audit_events` | Admin | Query platform security and administrative audit logs |

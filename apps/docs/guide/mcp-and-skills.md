# AI Agents (MCP & Skills)

SensoryPlex provides a standard **Model Context Protocol (MCP)** Server and **AI Skills** (supporting **Claude Desktop / Code**, **Codex CLI / Desktop**, **Cursor / Windsurf**), enabling LLMs to call multimodal video processing capabilities natively.

---

## Key Features

- **Multimodal Search**: Precise keyword and vector semantic search across OCR on-screen text, ASR dialogue transcripts, and VLM scene descriptions.
- **Second-Level Timeline Grounding**: Millisecond intervals `[start_ms, end_ms)` and normalized bounding box coordinates for every observation.
- **HTTP Range Stream Playback**: Playable stream URLs for instant video clip review in browsers.
- **Intelligent Job Dispatch**: Associate video assets with DAG pipelines and monitor execution status.
- **Cluster & Accelerator Diagnostics**: Inspect edge worker nodes and hardware accelerators (Apple Silicon Metal, NVIDIA CUDA, CoreML).

---

## Quick Setup

### Method 1: Automated Setup (Recommended)

Run the automated setup tool from the repository root:

```bash
# Automatically detects and updates Claude Desktop config and installs the Codex Skill
make mcp-setup

# Or run the script directly
python tools/setup_mcp.py --auto
```

This will:
1. Detect local API ports and authentication credentials;
2. Safely merge the `sensoryplex` MCP entry into Claude Desktop configuration;
3. Install the SensoryPlex skill to `~/.codex/skills/sensoryplex`;
4. Output ready-to-use commands for Codex CLI and Cursor.

---

### Method 2: Manual Configuration

#### 1. Claude Desktop Configuration

Edit your Claude Desktop configuration file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Add the `sensoryplex` server entry under `mcpServers`:

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
        "SENSORYPLEX_API_TOKEN": "YOUR_API_TOKEN"
      }
    }
  }
}
```

> **Zero-Config Tip**: In local development, if `SENSORYPLEX_API_TOKEN` is omitted, the MCP server will automatically discover and use the local demo account session!

#### 2. Codex CLI & Desktop Setup

Run in your terminal:
```bash
codex mcp add sensoryplex -- uv run --directory /absolute/path/to/SensoryPlex/services/mcp-server sensoryplex-mcp
```

Install the Codex skill:
```bash
make skill-install
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

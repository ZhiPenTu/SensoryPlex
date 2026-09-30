"""Unit tests for SensoryPlex MCP Server tools and client."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sensoryplex_mcp.client import (
    SensoryPlexAuthError,
    SensoryPlexClient,
)
from sensoryplex_mcp.server import (
    format_ms,
    get_playback_info,
    get_system_status,
    search_materials,
    server,
)


def test_format_ms():
    assert format_ms(0) == "00:00.000"
    assert format_ms(1500) == "00:01.500"
    assert format_ms(65432) == "01:05.432"
    assert format_ms(3665123) == "01:01:05.123"


@pytest.mark.asyncio
async def test_all_tools_registered():
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    expected_tools = {
        "get_system_status",
        "get_audit_events",
        "search_materials",
        "get_material_detail",
        "get_timeline_coverage",
        "list_media_assets",
        "get_playback_info",
        "list_pipelines",
        "submit_job_run",
        "get_job_run_status",
        "cancel_job_run",
        "list_nodes",
        "approve_candidate_node",
        "list_plugin_catalog",
    }
    assert expected_tools.issubset(tool_names)
    assert len(tools) >= 14
    for tool in tools:
        assert tool.description, f"Tool {tool.name} missing description"


@pytest.mark.asyncio
async def test_get_system_status():
    mock_client = AsyncMock()
    mock_client.base_url = "http://127.0.0.1:8091"
    mock_client.get_health.return_value = {"status": "alive", "schema_version": "0015"}
    mock_client.get_capabilities.return_value = {
        "capabilities": [{"name": "keyword_search", "available": True}]
    }

    with patch("sensoryplex_mcp.server.get_client", return_value=mock_client):
        res = await get_system_status()
        data = json.loads(res)
        assert data["health"]["status"] == "alive"
        assert data["capabilities"]["capabilities"][0]["name"] == "keyword_search"


@pytest.mark.asyncio
async def test_search_materials_formatting():
    mock_client = AsyncMock()
    mock_client.search_materials.return_value = {
        "materials": [
            {
                "material_unit_id": "mat_123",
                "start_ms": 1000,
                "end_ms": 5000,
                "stream_id": "stream_abc",
                "observations": [
                    {
                        "modality": "ocr",
                        "confidence": 0.95,
                        "payload_jsonb": {"text": "Architecture Overview"},
                    }
                ],
            }
        ],
        "hits": [{"score": 0.88}],
        "mode": "keyword",
    }

    with patch("sensoryplex_mcp.server.get_client", return_value=mock_client):
        res = await search_materials(query="Architecture", mode="keyword")
        data = json.loads(res)
        assert data["total_matches"] == 1
        assert data["results"][0]["material_unit_id"] == "mat_123"
        assert data["results"][0]["facts"][0]["text"] == "Architecture Overview"
        assert "00:01.000 -> 00:05.000" in data["results"][0]["time_range"]


@pytest.mark.asyncio
async def test_get_playback_info():
    mock_client = MagicMock()
    mock_client.get_playback_stream_url.return_value = (
        "http://127.0.0.1:8091/v1/uploads/upload_123/stream"
    )

    with patch("sensoryplex_mcp.server.get_client", return_value=mock_client):
        res = await get_playback_info(asset_id="upload_123", start_ms=2000, end_ms=8000)
        data = json.loads(res)
        assert data["asset_id"] == "upload_123"
        assert "http://127.0.0.1:8091/v1/uploads/upload_123/stream" in data["stream_url"]
        assert "00:02.000 to 00:08.000" == data["time_range"]


@pytest.mark.asyncio
async def test_client_error_mapping():
    client = SensoryPlexClient(base_url="http://mock-api:8091")

    mock_resp_401 = MagicMock()
    mock_resp_401.status_code = 401
    mock_resp_401.text = "unauthorized"

    with patch.object(client, "get_client") as mock_get:
        mock_http = AsyncMock()
        mock_http.request.return_value = mock_resp_401
        mock_http.get.return_value = MagicMock(status_code=404)
        mock_get.return_value = mock_http

        with pytest.raises(SensoryPlexAuthError):
            await client.request("GET", "/v1/test")

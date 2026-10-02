"""验证显式身份不会自动切换为演示用户，及真实 Proto JSON 时间值。"""

import json

import httpx
import pytest
from sensoryplex_mcp.client import SensoryPlexAPIError, SensoryPlexClient
from sensoryplex_mcp.server import format_ms


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_explicit_credentials_keep_identity(monkeypatch, success):
    monkeypatch.delenv("SENSORYPLEX_API_TOKEN", raising=False)
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/auth/v1/session":
            assert json.loads(request.content)["username"] == "operator"
            return httpx.Response(
                200 if success else 401,
                json={"csrf_token": "csrf"} if success else {},
                headers={"set-cookie": "sensoryplex_session=session; Path=/"},
            )
        assert request.headers.get("cookie") == "sensoryplex_session=session"
        return httpx.Response(200, json={"materials": []})

    client = SensoryPlexClient(base_url="http://platform", username="operator", password="password")
    client._http_client = httpx.AsyncClient(
        base_url="http://platform", transport=httpx.MockTransport(handle)
    )
    try:
        if success:
            assert await client.request("GET", "/v1/materials") == {"materials": []}
        else:
            with pytest.raises(SensoryPlexAPIError):
                await client.request("GET", "/v1/materials")
        assert "/auth/v1/demo-account" not in requests
    finally:
        await client.close()


def test_proto_json_milliseconds():
    assert format_ms("1001") == "00:01.001"

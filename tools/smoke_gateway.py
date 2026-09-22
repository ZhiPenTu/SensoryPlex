"""对已部署 Gateway 的只读冒烟检查。绝不打印凭据。"""

import json
import urllib.error
import urllib.request

from sensoryplex_gateway.settings import Settings

settings = Settings()
BASE = "http://127.0.0.1:8090"


def request(path, body=None, authenticated=True):
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
    req = urllib.request.Request(
        BASE + path, headers=headers, data=json.dumps(body).encode() if body is not None else None
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


status, health = request("/v1/health")
assert status == 200 and health["metadata_store"] == "ready"
status, result = request("/v1/materials:search", {})
assert status == 200 and result["mode"] == "keyword" and isinstance(result["materials"], list)
assert request("/v1/materials:search", {}, authenticated=False)[0] == 401
assert request("/v1/materials:search", {"mode": "semantic"})[0] == 501
print("Deployed Gateway: metadata readiness, authenticated search, 401 and explicit 501: PASS")

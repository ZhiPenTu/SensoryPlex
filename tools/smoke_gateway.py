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
# semantic 不再是 501（ADR-023）：Gateway 复用 sensoryplex_api 的同一份实现，因此
# 未配置检索面时是"显式不可用"（503 + 稳定原因码 + retryable=false），空查询是输入错误（422）。
semantic_status, semantic_error = request(
    "/v1/materials:search", {"mode": "semantic", "query": "销售"}
)
assert semantic_status == 503, (semantic_status, semantic_error)
assert semantic_error["reason_code"] == "semantic_search_unavailable", semantic_error
# 配置问题不可重试：503 不等于"稍后重试就好"。
assert semantic_error["retryable"] is False, semantic_error
assert request("/v1/materials:search", {"mode": "semantic"})[0] == 422
print(
    "Deployed Gateway: metadata readiness, authenticated search, 401, "
    "semantic 503 (semantic_search_unavailable, retryable=false) and 422 on empty query: PASS"
)

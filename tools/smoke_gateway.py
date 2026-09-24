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
        # 20s 而不是 5s：检索面没起来时，API 自己要先等满 gRPC 的 `index_search_timeout_s`
        # （默认 5s）才会给出 `semantic_index_unreachable`。客户端超时更短的话，冒烟会
        # 先在自己的超时上炸掉，看到的是"脚本太急"而不是"服务怎么答"。
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


status, health = request("/v1/health")
assert status == 200 and health["metadata_store"] == "ready"
status, result = request("/v1/materials:search", {})
assert status == 200 and result["mode"] == "keyword" and isinstance(result["materials"], list)
assert request("/v1/materials:search", {}, authenticated=False)[0] == 401
# semantic 不再是 501（ADR-023）：Gateway 复用 sensoryplex_api 的同一份实现。这里断言的是
# **"声明"与"行为"一致**，而不是某一种部署形态下的固定状态码——因为本项目有两种合法形态：
#   - 默认栈不含 `events` profile：检索面"已声明但没起来"→ 503 且必须 **retryable**；
#   - `make events-up` 之后：检索面在跑 → 200 且带检索面自己的字段。
# 唯一不被允许的是把这两种说成同一种：没部署却给可重试码、没起来却给配置类原因码、
# 或者用"空命中"冒充成功。空查询在任何形态下都是输入错误（422）。
#
# 判定基准取 `/v1/health` 的 `capabilities.semantic_search`（服务自己的声明），不取
# `os.environ` 也不取 `Settings`：容器里 `Settings` 还会读到 bind mount 进来的仓库 `.env`
# （ADR-027 §10.3 / §10.4），按它断言等于把"我在哪跑"当成契约——这正是本条断言上次变红的原因。
semantic_status, semantic_body = request(
    "/v1/materials:search", {"mode": "semantic", "query": "销售"}
)
assert semantic_status != 501, semantic_body
if not health["capabilities"]["semantic_search"]:
    assert semantic_status == 503, (semantic_status, semantic_body)
    assert semantic_body["reason_code"] == "semantic_search_unavailable", semantic_body
    # 未部署是配置问题、不可重试：503 不等于"稍后重试就好"。
    assert semantic_body["retryable"] is False, semantic_body
    semantic_outcome = "503 显式不可用（未部署, retryable=false）"
elif semantic_status == 200:
    assert semantic_body["mode"] == "semantic", semantic_body
    assert semantic_body["vector_index_key"], semantic_body
    assert semantic_body["index_version"], semantic_body
    assert isinstance(semantic_body["hits"], list), semantic_body
    semantic_outcome = "200 真实检索"
else:
    # 已声明可用、但检索面进程没起来（默认栈就是这种）：只允许"可重试的不可达"。
    assert semantic_status == 503, (semantic_status, semantic_body)
    assert semantic_body["retryable"] is True, semantic_body
    assert semantic_body["reason_code"] == "semantic_index_unreachable", semantic_body
    semantic_outcome = "503 检索面不可达（retryable=true）"
assert request("/v1/materials:search", {"mode": "semantic"})[0] == 422
print(
    "Deployed Gateway: metadata readiness, authenticated search, 401, "
    f"semantic {semantic_outcome} (never 501) and 422 on empty query: PASS"
)

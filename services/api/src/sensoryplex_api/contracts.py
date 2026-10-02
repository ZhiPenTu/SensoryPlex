"""HTTP 使用 Proto JSON；这里不重新声明跨语言字段。"""

import hashlib
import re
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from psycopg.rows import dict_row

RETRYABLE_HEADER = "X-Retryable"


def validate_processor_reason(value, *, required=False):
    """处理器原因只接收有界机器码，防止异常文本泄漏到控制台账。"""
    if (
        not isinstance(value, str)
        or (not value and required)
        or (value and not re.fullmatch(r"[a-z][a-z0-9_]{0,119}", value))
    ):
        raise ValueError("plugin_result_reason_invalid")


def hash_token(token: str) -> str:
    """节点会话/入网令牌只以 sha256 落库；明文不落库、不进日志。"""
    return hashlib.sha256(token.encode()).hexdigest()


def fail(status, reason, *, retryable=None):
    """显式失败。`retryable` 为 None 时沿用"429/503 可重试"这条默认口径。

    传 True/False 时以调用方的判定为准：HTTP 状态码表达不了"不可重试的 503"
    （`semantic_search_unavailable` 就是这种），而调用方要按 `retryable` 决定重试还是改配置。
    该标记只在本进程内传递——它挂在 `HTTPException.headers` 上被本项目的错误处理器消费，
    不会出现在响应头里。
    """
    headers = {} if retryable is None else {RETRYABLE_HEADER: "true" if retryable else "false"}
    raise HTTPException(status, reason, headers=headers)


def parse(body, cls):
    try:
        return ParseDict(body, cls(), ignore_unknown_fields=False)
    except (ParseError, TypeError, ValueError):
        fail(422, "invalid_contract")


def out(value, cls=None):
    if cls:
        value = ParseDict(clean(value), cls(), ignore_unknown_fields=True)
    result = MessageToDict(
        value, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )

    # v1 血缘的 JSON 保持原有形状；v2 显式声明时保留追加字段。
    def compatible(item):
        if isinstance(item, dict):
            provenance = item.get("provenance")
            if isinstance(provenance, dict) and not provenance.get("processor_release_id"):
                provenance.pop("processor_release_id", None)
                if provenance.get("model_applicability") == "MODEL_APPLICABILITY_UNSPECIFIED":
                    provenance.pop("model_applicability", None)
            for child in item.values():
                compatible(child)
        elif isinstance(item, list):
            for child in item:
                compatible(child)

    compatible(result)
    return result


def clean(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def rows(conn, query, params=()):
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(query, params).fetchall()


def one(conn, query, params=()):
    values = rows(conn, query, params)
    return values[0] if values else None


def identifier(prefix):
    return f"{prefix}_{uuid4().hex}"


def audit(conn, actor, action, target):
    conn.execute(
        "INSERT INTO console_audit(id,actor,action,target) VALUES (%s,%s,%s,%s)",
        (identifier("audit"), actor, action, target),
    )


def text_field(value, maximum=120):
    if not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        fail(422, "invalid_text_field")
    return value.strip()

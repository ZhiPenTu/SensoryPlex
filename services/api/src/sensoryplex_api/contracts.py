"""HTTP 使用 Proto JSON；这里不重新声明跨语言字段。"""

from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from psycopg.rows import dict_row


def fail(status, reason):
    raise HTTPException(status, reason)


def parse(body, cls):
    try:
        return ParseDict(body, cls(), ignore_unknown_fields=False)
    except (ParseError, TypeError, ValueError):
        fail(422, "invalid_contract")


def out(value, cls=None):
    if cls:
        value = ParseDict(clean(value), cls(), ignore_unknown_fields=True)
    return MessageToDict(
        value, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
    )


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

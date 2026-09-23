"""会话、业务凭据与对象授权。管理员权限不会隐式扩大内容访问范围。"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request

from .contracts import fail, one

ROLES = {
    "viewer": {"materials:read", "assets:read", "jobs:read"},
    "operator": {"materials:read", "assets:read", "assets:write", "jobs:read", "jobs:write"},
    "admin": {"plugins:manage", "pipelines:manage", "audit:read", "users:manage"},
}
BUSINESS = ROLES["operator"]
COOKIE = "sensoryplex_session"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    derived = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt:{salt}:{derived}"


def verify_password(password, stored):
    try:
        _, salt, _ = stored.split(":")
        return hmac.compare_digest(password_hash(password, salt), stored)
    except (ValueError, TypeError):
        return False


def validate_user(username, password, roles):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,64}", username):
        fail(422, "invalid_username")
    if not 12 <= len(password) <= 256:
        fail(422, "password_length_12_to_256_required")
    if not roles or set(roles) - ROLES.keys():
        fail(422, "invalid_roles")


def permissions(roles):
    return set().union(*(ROLES.get(role, set()) for role in roles))


@dataclass(frozen=True)
class Principal:
    name: str
    display_name: str
    roles: list[str]
    scopes: set[str]
    csrf: str = ""


class Authorization:
    def __init__(self, pool, settings):
        self.pool, self.settings = pool, settings

    def check_origin(self, request):
        origin = request.headers.get("origin")
        if origin and origin not in self.settings.allowed_origins.split(","):
            fail(403, "origin_not_allowed")

    def identify(self, request: Request):
        self.check_origin(request)
        header = request.headers.get("authorization", "")
        if header:
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token or len(token) > 512:
                fail(401, "authentication_required")
            legacy = self.settings.api_token
            if legacy and secrets.compare_digest(token, legacy.get_secret_value()):
                return Principal(self.settings.principal, self.settings.principal, [], BUSINESS)
            with self.pool.connection() as conn:
                row = one(
                    conn,
                    "SELECT u.username,u.display_name,u.roles,t.scopes FROM console_token t "
                    "JOIN console_user u ON u.username=t.username WHERE t.token_hash=%s "
                    "AND NOT t.revoked AND NOT u.disabled AND t.expires_at>now()",
                    (digest(token),),
                )
            if not row:
                fail(401, "authentication_required")
            return Principal(
                row["username"],
                row["display_name"],
                row["roles"],
                permissions(row["roles"]) & set(row["scopes"]),
            )
        token = request.cookies.get(COOKIE, "")
        if not token or len(token) > 512:
            fail(401, "authentication_required")
        with self.pool.connection() as conn:
            row = one(
                conn,
                "SELECT u.username,u.display_name,u.roles,s.csrf_token FROM console_session s "
                "JOIN console_user u ON u.username=s.username WHERE s.token_hash=%s "
                "AND s.expires_at>now() AND NOT u.disabled",
                (digest(token),),
            )
        if not row:
            fail(401, "authentication_required")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), row["csrf_token"]
        ):
            fail(403, "csrf_required")
        return Principal(
            row["username"],
            row["display_name"],
            row["roles"],
            permissions(row["roles"]),
            row["csrf_token"],
        )

    def require(self, scope):
        def dependency(request: Request):
            principal = self.identify(request)
            if scope and scope not in principal.scopes:
                fail(403, "permission_denied")
            return principal

        return dependency


def now():
    return datetime.now(UTC)

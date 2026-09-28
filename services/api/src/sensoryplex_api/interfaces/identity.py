"""认证与凭据路由；持久限流避免进程重启绕过登录预算。"""

import secrets
from datetime import timedelta
from typing import Annotated

from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from fastapi import Body, Depends, Request, Response

from ..auth import (
    BUSINESS,
    COOKIE,
    digest,
    now,
    password_hash,
    permissions,
    validate_user,
    verify_password,
)
from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field

DUMMY_HASH = password_hash("not-an-account-password")


def register(app, pool, auth, settings):
    @app.get("/auth/v1/demo-account")
    def demo_account(request: Request):
        auth.check_origin(request)
        if not settings.demo_username or not settings.demo_password:
            return out(pb.DemoAccount(enabled=False))
        password = settings.demo_password.get_secret_value()
        with pool.connection() as conn:
            user = one(
                conn,
                "SELECT password_hash,disabled FROM console_user WHERE username=%s",
                (settings.demo_username,),
            )
            if not user or user["disabled"] or not verify_password(password, user["password_hash"]):
                conn.execute(
                    "INSERT INTO console_user "
                    "(username, display_name, password_hash, roles, disabled, created_at) "
                    "VALUES (%s, %s, %s, %s, false, now()) "
                    "ON CONFLICT (username) DO UPDATE SET "
                    "password_hash=EXCLUDED.password_hash, disabled=false",
                    (
                        settings.demo_username,
                        "演示账号",
                        password_hash(password),
                        ["admin", "operator"],
                    ),
                )
                conn.commit()
        return out(pb.DemoAccount(enabled=True, username=settings.demo_username, password=password))

    @app.post("/auth/v1/session")
    def login(request: Request, response: Response, body: Annotated[dict, Body()] = ...):
        auth.check_origin(request)
        req = parse(body, pb.LoginRequest)
        if len(req.username) > 64 or len(req.password) > 256:
            fail(401, "invalid_credentials")
        buckets = [digest("user:" + req.username), digest("ip:" + request.client.host)]
        limited, user, valid = False, None, False
        with pool.connection() as conn:
            for bucket in sorted(buckets):
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (bucket,))
            conn.execute(
                "DELETE FROM console_login_attempt WHERE window_start<now()-interval '10 minutes'"
            )
            for bucket, maximum in zip(buckets, (5, 30), strict=True):
                attempt = one(
                    conn, "SELECT failures FROM console_login_attempt WHERE bucket=%s", (bucket,)
                )
                limited |= bool(attempt and attempt["failures"] >= maximum)
            if not limited:
                user = one(conn, "SELECT * FROM console_user WHERE username=%s", (req.username,))
                valid = verify_password(req.password, user["password_hash"] if user else DUMMY_HASH)
                valid = bool(valid and user and not user["disabled"])
                if not valid:
                    for bucket in buckets:
                        conn.execute(
                            "INSERT INTO console_login_attempt VALUES (%s,1,now()) "
                            "ON CONFLICT(bucket) DO UPDATE SET "
                            "failures=console_login_attempt.failures+1",
                            (bucket,),
                        )
                else:
                    conn.execute("DELETE FROM console_login_attempt WHERE bucket=%s", (buckets[0],))
                    conn.execute("DELETE FROM console_session WHERE expires_at<=now()")
                    count = conn.execute(
                        "SELECT count(*) FROM console_session WHERE username=%s", (req.username,)
                    ).fetchone()[0]
                    if count >= 20:
                        conn.execute(
                            "DELETE FROM console_session WHERE token_hash IN (SELECT "
                            "token_hash FROM console_session WHERE username=%s "
                            "ORDER BY created_at LIMIT 1)",
                            (req.username,),
                        )
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    conn.execute(
                        (
                            "INSERT INTO "
                            "console_session(token_hash,username,csrf_token,expires_at) "
                            "VALUES (%s,%s,%s,%s)"
                        ),
                        (
                            digest(token),
                            req.username,
                            csrf,
                            now() + timedelta(hours=settings.session_hours),
                        ),
                    )
                    audit(conn, req.username, "session.login", req.username)
        if limited:
            fail(429, "login_rate_limited")
        if not valid:
            fail(401, "invalid_credentials")
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_hours * 3600,
        )
        return out(
            pb.Identity(
                principal=req.username,
                display_name=user["display_name"],
                roles=user["roles"],
                permissions=sorted(permissions(user["roles"])),
                csrf_token=csrf,
            )
        )

    @app.get("/auth/v1/me")
    def me(p: Annotated[object, Depends(auth.require(None))] = None):
        return out(
            pb.Identity(
                principal=p.name,
                display_name=p.display_name,
                roles=p.roles,
                permissions=sorted(p.scopes),
                csrf_token=p.csrf,
            )
        )

    @app.delete("/auth/v1/session")
    def logout(
        request: Request,
        response: Response,
        p: Annotated[object, Depends(auth.require(None))] = None,
    ):
        with pool.connection() as conn:
            conn.execute(
                "DELETE FROM console_session WHERE token_hash=%s",
                (digest(request.cookies.get(COOKIE, "")),),
            )
            audit(conn, p.name, "session.logout", p.name)
        response.delete_cookie(COOKIE)
        return out(pb.EmptyResponse())

    @app.get("/auth/v1/access-tokens")
    def tokens(p: Annotated[object, Depends(auth.require(None))] = None):
        if not p.csrf:
            fail(403, "browser_session_required")
        with pool.connection() as conn:
            items = rows(
                conn,
                "SELECT * FROM console_token WHERE username=%s ORDER BY created_at DESC LIMIT 100",
                (p.name,),
            )
        return out({"items": items, "total": len(items)}, pb.TokenList)

    @app.post("/auth/v1/access-tokens", status_code=201)
    def create_token(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require(None))] = None,
    ):
        if not p.csrf:
            fail(403, "browser_session_required")
        req = parse(body, pb.CreateToken)
        text_field(req.name)
        if not req.scopes or set(req.scopes) - (p.scopes & BUSINESS):
            fail(403, "token_scope_exceeds_grant")
        if not 1 <= req.expires_in_days <= 90:
            fail(422, "token_expiry_out_of_range")
        token, key = "spx_" + secrets.token_urlsafe(32), identifier("token")
        with pool.connection() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("tokens:" + p.name,)
            )
            if (
                conn.execute(
                    (
                        "SELECT count(*) FROM console_token WHERE username=%s AND NOT "
                        "revoked AND expires_at>now()"
                    ),
                    (p.name,),
                ).fetchone()[0]
                >= 20
            ):
                fail(429, "token_limit_reached")
            if not one(conn, "SELECT username FROM console_user WHERE username=%s", (p.name,)):
                fail(403, "session_account_required")
            record = one(
                conn,
                (
                    "INSERT INTO "
                    "console_token(id,username,name,token_hash,scopes,expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *"
                ),
                (
                    key,
                    p.name,
                    req.name,
                    digest(token),
                    list(req.scopes),
                    now() + timedelta(days=req.expires_in_days),
                ),
            )
            audit(conn, p.name, "token.create", key)
        return out({**record, "token": token}, pb.AccessToken)

    @app.post("/auth/v1/access-tokens/{key}:revoke")
    def revoke(key: str, p: Annotated[object, Depends(auth.require(None))] = None):
        if not p.csrf:
            fail(403, "browser_session_required")
        with pool.connection() as conn:
            result = conn.execute(
                "UPDATE console_token SET revoked=true WHERE id=%s AND username=%s RETURNING id",
                (key, p.name),
            ).fetchone()
            if not result:
                fail(404, "token_not_found")
            audit(conn, p.name, "token.revoke", key)
        return out(pb.EmptyResponse())

    @app.get("/admin/v1/users")
    def users(p: Annotated[object, Depends(auth.require("users:manage"))] = None):
        with pool.connection() as conn:
            items = rows(
                conn,
                (
                    "SELECT username,display_name,roles,disabled FROM console_user "
                    "ORDER BY username LIMIT 100"
                ),
            )
        return out({"items": items, "total": len(items)}, pb.UserList)

    @app.post("/admin/v1/users", status_code=201)
    def create_user(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("users:manage"))] = None,
    ):
        req = parse(body, pb.CreateUser)
        validate_user(req.username, req.password, req.roles)
        text_field(req.display_name)
        with pool.connection() as conn:
            conn.execute(
                (
                    "INSERT INTO "
                    "console_user(username,display_name,password_hash,roles) VALUES "
                    "(%s,%s,%s,%s)"
                ),
                (req.username, req.display_name, password_hash(req.password), list(req.roles)),
            )
            audit(conn, p.name, "user.create", req.username)
        return out(pb.User(username=req.username, display_name=req.display_name, roles=req.roles))

    @app.put("/admin/v1/users/{username}")
    def update_user(
        username: str,
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("users:manage"))] = None,
    ):
        req = parse(body, pb.UpdateUser)
        text_field(req.display_name)
        if not req.roles or set(req.roles) - {"admin", "operator", "viewer"}:
            fail(422, "invalid_roles")
        if username == p.name and (req.disabled or "admin" not in req.roles):
            fail(409, "cannot_remove_own_administration")
        if req.HasField("password"):
            validate_user(username, req.password, req.roles)
        with pool.connection() as conn:
            user = one(conn, "SELECT * FROM console_user WHERE username=%s FOR UPDATE", (username,))
            if not user:
                fail(404, "user_not_found")
            credentials_changed = (
                req.HasField("password") or req.disabled or set(req.roles) != set(user["roles"])
            )
            conn.execute(
                "UPDATE console_user SET display_name=%s,roles=%s,disabled=%s,password_hash=%s "
                "WHERE username=%s",
                (
                    req.display_name,
                    list(req.roles),
                    req.disabled,
                    password_hash(req.password)
                    if req.HasField("password")
                    else user["password_hash"],
                    username,
                ),
            )
            if credentials_changed:
                conn.execute("DELETE FROM console_session WHERE username=%s", (username,))
                conn.execute("UPDATE console_token SET revoked=true WHERE username=%s", (username,))
            audit(conn, p.name, "user.update", username)
        return out(
            pb.User(
                username=username,
                display_name=req.display_name,
                roles=req.roles,
                disabled=req.disabled,
            )
        )

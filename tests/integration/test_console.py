"""真实 PostgreSQL 验证控制台准备流程；媒体字节仅用于传输测试，不是 AI E2E。"""

import hashlib
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sensoryplex_api.app import create_app
from sensoryplex_api.auth import password_hash
from sensoryplex_api.settings import Settings

from tools.migrate import migrate

pytestmark = pytest.mark.integration
PASSWORD = "test-account-password-2026"


@pytest.fixture
def console_database():
    url = os.environ["SENSORYPLEX_TEST_DATABASE_URL"]
    schema = "console_test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(url, options=f"-c search_path={schema}")
    try:
        migrate(isolated)
        migrate(isolated)
        with psycopg.connect(isolated) as conn:
            for name, roles in (
                ("admin", ["admin", "operator"]),
                ("viewer", ["viewer"]),
                ("other", ["operator"]),
                ("manager", ["admin"]),
            ):
                conn.execute(
                    "INSERT INTO console_user(username,display_name,password_hash,roles) "
                    "VALUES (%s,%s,%s,%s)",
                    (name, name, password_hash(PASSWORD), roles),
                )
        yield isolated
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def console_app(console_database, tmp_path):
    return create_app(
        Settings(
            database_url=console_database,
            api_token=None,
            blob_root=tmp_path / "blobs",
            console_dist=tmp_path / "dist",
            max_upload_bytes=1024,
            allowed_origins="http://testserver",
        )
    )


def login(client, username="admin"):
    result = client.post("/auth/v1/session", json={"username": username, "password": PASSWORD})
    assert result.status_code == 200, result.text
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    return result.json()


def config(client):
    entries = client.get("/admin/v1/catalog").json()["items"]
    assert len(entries) >= 1
    item = next(item for item in entries if item["id"] == "org.sensoryplex.vlm-moondream")
    assert item["state"] == "source_available" and item["trust"] == "unverified"
    result = client.post(
        "/admin/v1/plugin-configurations",
        json={
            "plugin_id": item["id"],
            "name": "vision",
            "config": {"model": "moondream:v2", "timeout_s": 180},
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def pipeline(client, config):
    result = client.post(
        "/admin/v1/pipelines",
        json={
            "name": "vision",
            "description": "contract test",
            "plugin_id": config["plugin_id"],
            "config_id": config["id"],
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def upload(client):
    # 只验证文件传输、摘要和授权；不冒充可解码或获准处理的样本。
    data = b"\x00\x00\x00\x18ftypmp42" + b"transport-contract-only" * 3
    result = client.post(
        "/v1/uploads",
        json={
            "filename": "transport.mp4",
            "size_bytes": str(len(data)),
            "content_type": "video/mp4",
        },
    )
    assert result.status_code == 201, result.text
    key = result.json()["id"]
    stored = client.put(f"/v1/uploads/{key}/content", content=data)
    assert stored.status_code == 200, stored.text
    assert stored.json()["state"] == "awaiting_admission"
    assert stored.json()["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()
    return key, data


def test_auth_csrf_roles_and_origin(console_app):
    with TestClient(console_app) as client:
        assert client.get("/v1/assets").status_code == 401
        result = login(client, "viewer")
        assert "plugins:manage" not in result["permissions"]
        assert client.get("/v1/assets").status_code == 200
        assert client.get("/admin/v1/catalog").status_code == 403
        assert client.post("/admin/v1/plugin-installations", json={}).status_code == 403
        assert client.post("/v1/uploads", json={}).status_code == 403
        client.headers.pop("X-CSRF-Token")
        assert client.delete("/auth/v1/session").status_code == 403
        client.headers["X-CSRF-Token"] = result["csrf_token"]
        assert (
            client.get("/auth/v1/me", headers={"Origin": "https://evil.invalid"}).status_code == 403
        )
        assert client.delete("/auth/v1/session").status_code == 200
        assert client.get("/auth/v1/me").status_code == 401
        login(client, "manager")
        assert client.get("/admin/v1/catalog").status_code == 200
        assert client.get("/v1/assets").status_code == 403
        assert client.post("/v1/materials:search", json={}).status_code == 403


def test_upload_range_owner_and_restart(console_app):
    with TestClient(console_app) as client:
        login(client)
        key, data = upload(client)
        read = client.get(f"/v1/assets/{key}/content", headers={"Range": "bytes=4-11"})
        assert read.status_code == 206 and read.content == data[4:12]
        assert client.head(f"/v1/assets/{key}/content").headers["content-length"] == str(len(data))
        assert (
            client.get(f"/v1/assets/{key}/content", headers={"Range": "bytes=9999-"}).status_code
            == 416
        )
        assert client.put(f"/v1/uploads/{key}/content", content=data).status_code == 409
        cookie = client.cookies.get("sensoryplex_session")
    # 新的应用实例复用同一数据库与 Blob；不依赖进程内状态。
    with TestClient(create_app(console_app.state.settings)) as client:
        client.cookies.set("sensoryplex_session", cookie)
        assert client.get("/v1/assets").json()["items"][0]["id"] == key
        assert client.get(f"/v1/assets/{key}/content").content == data
        login(client, "other")
        assert client.get("/v1/assets").json()["items"] == []
        assert client.get(f"/v1/assets/{key}").status_code == 404
        assert client.get(f"/v1/assets/{key}/content").status_code == 404


def test_upload_limits_unknown_contract_and_content_rejection(console_app):
    with TestClient(console_app) as client:
        login(client)
        base = {"filename": "x.mp4", "size_bytes": "20", "content_type": "video/mp4"}
        for patch, status in (
            ({"filename": "../escape.mp4"}, 422),
            ({"size_bytes": "1025"}, 413),
            ({"unexpected": True}, 422),
        ):
            assert client.post("/v1/uploads", json={**base, **patch}).status_code == status
        item = client.post("/v1/uploads", json=base).json()
        assert client.put(f"/v1/uploads/{item['id']}/content", content=b"x" * 20).status_code == 422
        assert client.get(f"/v1/assets/{item['id']}").json()["state"] == "pending"
        assert client.post("/v1/uploads", content="x" * 70000).status_code == 413
        assert not list(console_app.state.settings.blob_root.glob("upload-*"))
        assert client.delete(f"/v1/uploads/{item['id']}").status_code == 200
        assert client.get(f"/v1/assets/{item['id']}").status_code == 404


def test_config_versions_pipeline_references_and_drafts(console_app, console_database):
    with TestClient(console_app) as client:
        login(client)
        first, second = config(client), config(client)
        assert first["revision"] == 1 and second["revision"] == 2
        assert first["config_hash"] == second["config_hash"]
        plan = pipeline(client, first)
        key, _ = upload(client)
        draft = client.post(
            "/v1/job-drafts", json={"name": "review", "asset_id": key, "pipeline_id": plan["id"]}
        )
        assert draft.status_code == 201, draft.text
        assert draft.json()["state"] == "draft"
        assert client.post(f"/admin/v1/pipelines/{plan['id']}:archive").status_code == 409
        assert client.post(f"/v1/job-drafts/{draft.json()['id']}:archive").status_code == 200
        assert client.post(f"/admin/v1/pipelines/{plan['id']}:archive").status_code == 200
        assert (
            client.post(
                "/v1/job-drafts",
                json={"name": "invalid", "asset_id": key, "pipeline_id": plan["id"]},
            ).status_code
            == 422
        )
        assert client.post("/admin/v1/plugin-installations", json={}).status_code == 501
        assert client.post("/v1/jobs", json={}).status_code in {422, 501}
        assert client.post(f"/admin/v1/pipelines/{plan['id']}:publish", json={}).status_code in {
            200,
            501,
        }
        assert client.get("/admin/v1/plugins").status_code == 501
        events = client.get("/admin/v1/audit-events").json()["items"]
        assert "plugin.config.save" in {e["action"] for e in events}
        assert PASSWORD not in str(events)
    with psycopg.connect(console_database) as conn:
        assert conn.execute("SELECT count(*) FROM material_unit").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM processing_job").fetchone()[0] == 0


def test_token_scope_revocation_and_secret_storage(console_app, console_database):
    with TestClient(console_app) as client:
        login(client)
        assert (
            client.post(
                "/auth/v1/access-tokens",
                json={"name": "bad", "scopes": ["plugins:manage"], "expires_in_days": 1},
            ).status_code
            == 403
        )
        response = client.post(
            "/auth/v1/access-tokens",
            json={"name": "agent", "scopes": ["materials:read"], "expires_in_days": 1},
        )
        assert response.status_code == 201, response.text
        token = response.json()
        headers = {"Authorization": "Bearer " + token["token"]}
        assert client.post("/v1/materials:search", json={}, headers=headers).status_code == 200
        assert client.get("/v1/assets", headers=headers).status_code == 403
        assert client.get("/admin/v1/catalog", headers=headers).status_code == 403
        assert client.get("/auth/v1/access-tokens", headers=headers).status_code == 403
        assert (
            client.post(f"/auth/v1/access-tokens/{token['id']}:revoke", headers=headers).status_code
            == 403
        )
        assert client.post("/auth/v1/access-tokens", json={}, headers=headers).status_code == 403
        assert token["token"] not in client.get("/auth/v1/access-tokens").text
        with psycopg.connect(console_database) as conn:
            assert (
                conn.execute("SELECT token_hash FROM console_token").fetchone()[0] != token["token"]
            )
        assert client.post(f"/auth/v1/access-tokens/{token['id']}:revoke").status_code == 200
        assert client.post("/v1/materials:search", json={}, headers=headers).status_code == 401


def test_login_throttle_and_user_creation(console_app):
    with TestClient(console_app) as client:
        for _ in range(5):
            assert (
                client.post(
                    "/auth/v1/session", json={"username": "ghost", "password": "wrong"}
                ).status_code
                == 401
            )
        assert (
            client.post(
                "/auth/v1/session", json={"username": "ghost", "password": PASSWORD}
            ).status_code
            == 429
        )
        login(client)
        body = {
            "username": "new-user",
            "display_name": "new",
            "password": PASSWORD,
            "roles": ["viewer"],
        }
        assert client.post("/admin/v1/users", json=body).status_code == 201
        assert client.post("/admin/v1/users", json=body).status_code == 409
        login(client, "new-user")
        assert client.get("/admin/v1/users").status_code == 403
        assert client.get("/v1/assets").status_code == 200


def test_account_changes_revoke_sessions_and_tokens(console_app):
    with TestClient(console_app) as client:
        login(client, "other")
        old_cookie = client.cookies.get("sensoryplex_session")
        token = client.post(
            "/auth/v1/access-tokens",
            json={"name": "worker", "scopes": ["assets:read"], "expires_in_days": 1},
        ).json()["token"]
        login(client)
        body = {"display_name": "只读用户", "roles": ["viewer"], "disabled": False}
        assert client.put("/admin/v1/users/other", json=body).status_code == 200
        assert (
            client.get("/v1/assets", headers={"Authorization": "Bearer " + token}).status_code
            == 401
        )
        with TestClient(create_app(console_app.state.settings)) as revoked:
            revoked.cookies.set("sensoryplex_session", old_cookie)
            assert revoked.get("/v1/assets").status_code == 401
        assert client.put("/admin/v1/users/admin", json=body).status_code == 409
        assert (
            client.put("/admin/v1/users/other", json={**body, "disabled": True}).status_code == 200
        )
        assert (
            client.post(
                "/auth/v1/session", json={"username": "other", "password": PASSWORD}
            ).status_code
            == 401
        )
        assert (
            client.put(
                "/admin/v1/users/other", json={**body, "password": "replacement-secret-2026"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/auth/v1/session", json={"username": "other", "password": PASSWORD}
            ).status_code
            == 401
        )
        result = client.post(
            "/auth/v1/session", json={"username": "other", "password": "replacement-secret-2026"}
        )
        assert result.status_code == 200
        assert result.json()["roles"] == ["viewer"]


def test_demo_account_is_explicit_and_respects_account_state(console_app, console_database):
    from pydantic import SecretStr

    with TestClient(console_app) as client:
        response = client.get("/auth/v1/demo-account")
        assert response.status_code == 200
        assert response.json() == {"enabled": False, "username": "", "password": ""}
        assert response.headers["cache-control"] == "no-store"
    settings = console_app.state.settings.model_copy(
        update={"demo_username": "other", "demo_password": SecretStr(PASSWORD)}
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/auth/v1/demo-account")
        assert response.json() == {"enabled": True, "username": "other", "password": PASSWORD}
        assert client.get("/auth/v1/me").status_code == 401
        assert (
            client.get(
                "/auth/v1/demo-account", headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        login(client, "other")
        with psycopg.connect(console_database) as conn:
            conn.execute("UPDATE console_user SET disabled=true WHERE username='other'")
        assert client.get("/auth/v1/demo-account").json()["enabled"] is False
        with psycopg.connect(console_database) as conn:
            conn.execute(
                "UPDATE console_user SET disabled=false,password_hash=%s WHERE username='other'",
                (password_hash("changed-password-2026"),),
            )
        assert client.get("/auth/v1/demo-account").json()["enabled"] is False

"""素材回看授权与来源绑定的真实 PostgreSQL 测试；构造观测不是模型输出。"""

import psycopg
import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict
from sensoryplex_api.infrastructure.materials import append_material
from test_console import console_app as console_app
from test_console import console_database as console_database
from test_console import login, upload

pytestmark = pytest.mark.integration


def seed_review(database, material, upload_id, digest):
    material.source_refs[0].content_hash = digest
    observation = material.observations[0]
    provenance = observation.provenance
    with psycopg.connect(database) as conn:
        conn.execute(
            "INSERT INTO media_source(source_id,type,uri_redacted,owner) "
            "VALUES (%s,'file','[redacted]','admin')",
            (observation.source_id,),
        )
        conn.execute(
            "INSERT INTO stream_session(stream_id,source_id,started_at,status) "
            "VALUES (%s,%s,now(),'stopped')",
            (material.stream_id, observation.source_id),
        )
        conn.execute(
            "INSERT INTO media_asset VALUES (%s,%s,%s,%s,'contract-only',1000)",
            (material.source_refs[0].asset_id, material.stream_id, f"upload://{upload_id}", digest),
        )
        conn.execute(
            "INSERT INTO model_release(model_release_id,name,version,artifact_hash,backend,"
            "config_hash) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                provenance.model_release_id,
                provenance.model_id,
                provenance.model_version,
                provenance.model_artifact_digest,
                provenance.execution_backend,
                provenance.config_hash,
            ),
        )
        conn.execute(
            "INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms) "
            "VALUES (%s,%s,'frame',0,1000)",
            (observation.source_item_id, material.stream_id),
        )
        assert append_material(conn, material, trace_id="material-review-contract")
    return (
        f"/v1/materials/{material.material_unit_id}/sources/"
        f"{material.source_refs[0].asset_id}?revision=1"
    )


def test_source_requires_material_asset_permissions_owner_and_reference(
    console_app, console_database, material
):
    with TestClient(console_app) as client:
        login(client)
        key, data = upload(client)
        digest = client.get(f"/v1/assets/{key}").json()["sha256"]
        route = seed_review(console_database, material, key, digest)
        resolved = client.get(route)
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["id"] == key
        assert "object_uri" not in resolved.json()
        assert resolved.headers["cache-control"] == "no-store"
        content = client.get(f"/v1/assets/{key}/content", headers={"Range": "bytes=4-11"})
        assert content.status_code == 206 and content.content == data[4:12]
        assert client.get(route.replace("revision=1", "revision=2")).status_code == 404
        assert client.get(route.replace("asset_contract", "missing")).status_code == 404
        for scopes in (["materials:read"], ["assets:read"]):
            token = client.post(
                "/auth/v1/access-tokens",
                json={"name": "review-scope-test", "scopes": scopes, "expires_in_days": 1},
            ).json()["token"]
            assert (
                client.get(route, headers={"Authorization": "Bearer " + token}).status_code == 403
            )
        login(client, "other")
        assert client.get(route).status_code == 404
        login(client, "viewer")
        assert client.get(route).status_code == 404
        login(client, "manager")
        assert client.get(route).status_code == 403
        client.cookies.clear()
        client.headers.pop("X-CSRF-Token", None)
        assert client.get(route).status_code == 401


@pytest.mark.parametrize(
    ("change", "status", "reason"),
    [
        ("uri", 409, "material_source_unmapped"),
        ("remote", 409, "material_source_unmapped"),
        ("stream_type", 409, "material_source_unmapped"),
        ("hash", 409, "material_source_mismatch"),
        ("duration", 409, "material_source_time_invalid"),
        ("upload_owner", 404, "material_source_not_found"),
        ("upload_hash", 409, "material_source_mismatch"),
        ("upload_state", 409, "upload_incomplete"),
        ("missing_blob", 503, "blob_unavailable"),
        ("symlink", 503, "blob_unavailable"),
    ],
)
def test_untrusted_or_missing_source_never_falls_back(
    console_app, console_database, material, change, status, reason
):
    with TestClient(console_app) as client:
        login(client)
        key, _ = upload(client)
        digest = client.get(f"/v1/assets/{key}").json()["sha256"]
        route = seed_review(console_database, material, key, digest)
        with psycopg.connect(console_database) as conn:
            statements = {
                "uri": ("UPDATE media_asset SET object_uri=%s", ("file:///etc/passwd",)),
                "remote": (
                    "UPDATE media_asset SET object_uri=%s",
                    ("https://example.invalid/video",),
                ),
                "stream_type": ("UPDATE media_source SET type=%s", ("srt",)),
                "hash": ("UPDATE media_asset SET sha256=%s", ("sha256:" + "f" * 64,)),
                "duration": ("UPDATE media_asset SET duration_ms=100", ()),
                "upload_owner": ("UPDATE console_upload SET owner='other' WHERE id=%s", (key,)),
                "upload_hash": (
                    "UPDATE console_upload SET sha256=%s WHERE id=%s",
                    ("sha256:" + "f" * 64, key),
                ),
                "upload_state": ("UPDATE console_upload SET state='pending' WHERE id=%s", (key,)),
            }
            if change in statements:
                conn.execute(*statements[change])
        if change in {"missing_blob", "symlink"}:
            blob = console_app.state.settings.blob_root / digest[7:]
            blob.unlink()
            if change == "symlink":
                blob.symlink_to("/etc/passwd")
        result = client.get(route)
        assert result.status_code == status, result.text
        assert result.json()["reason_code"] == reason
        assert "/etc/passwd" not in result.text and "object_uri" not in result.text


def test_revision_source_is_resolved_from_requested_snapshot(
    console_app, console_database, material
):
    with TestClient(console_app) as client:
        login(client)
        key, _ = upload(client)
        digest = client.get(f"/v1/assets/{key}").json()["sha256"]
        route = seed_review(console_database, material, key, digest)
        material.revision = 2
        material.created_at_unix_ms += 1
        material.source_refs[0].asset_id = "asset_unmapped"
        with psycopg.connect(console_database) as conn:
            conn.execute(
                "INSERT INTO media_asset VALUES (%s,%s,%s,%s,'contract-only',1000)",
                ("asset_unmapped", material.stream_id, "private://unmapped", digest),
            )
            append_material(conn, material, trace_id="review-revision-2")
        assert client.get(route).status_code == 200
        assert client.get(route.replace("revision=1", "revision=2")).status_code == 404
        latest = client.get(f"/v1/materials/{material.material_unit_id}").json()
        assert latest["revision"] == 2 and not latest["superseded"]
        historical = client.get(f"/v1/materials/{material.material_unit_id}?revision=1").json()
        assert historical["superseded"]
        assert historical["source_refs"][0]["asset_id"] == "asset_contract"


def test_search_filters_literal_matching_and_unknown_confidence(
    console_app, console_database, material
):
    with TestClient(console_app) as client:
        login(client)
        key, _ = upload(client)
        digest = client.get(f"/v1/assets/{key}").json()["sha256"]
        observation = material.observations[0]
        observation.ClearField("confidence")
        observation.confidence_unavailable_reason = "contract_fixture_unknown"
        seed_review(console_database, material, key, digest)

        def search(**filters):
            result = client.post("/v1/materials:search", json=filters)
            assert result.status_code == 200, result.text
            return result.json()["materials"]

        assert (
            len(
                search(
                    query="100%",
                    tags=["财务"],
                    modalities=["asr_segment"],
                    stream_id=material.stream_id,
                    start_ms="199",
                    end_ms="201",
                )
            )
            == 1
        )
        assert search(start_ms="200") == []
        assert search(end_ms="100") == []
        assert search(tags=["不存在"]) == []
        assert search(query="_") == []
        assert search(min_confidence=0) == []
        detail = client.get(f"/v1/materials/{material.material_unit_id}").json()
        assert "confidence" not in detail["observations"][0]
        assert detail["observations"][0]["provenance"] == MessageToDict(
            observation.provenance, preserving_proto_field_name=True
        )
        assert client.post("/v1/materials:search", json={"mode": "semantic"}).status_code == 501
        assert (
            client.post("/v1/materials:search", json={"start_ms": "10", "end_ms": "10"}).status_code
            == 422
        )

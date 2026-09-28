"""真实 PostgreSQL 验证控制台准备流程；媒体字节仅用于传输测试，不是 AI E2E。"""

import base64
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from edge_material_sdk.generated.media.v1 import media_pb2
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
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


def test_vlm_delayed_config_is_bound_to_local_decode(console_app):
    """ADR-031 不能让可发布的 VLM Revision 留在 Runtime descriptor 路径。"""
    with TestClient(console_app) as client:
        login(client)
        accepted = client.post(
            "/admin/v1/plugin-configurations",
            json={
                "plugin_id": "org.sensoryplex.vlm-moondream",
                "name": "delayed-vlm",
                "config": {},
            },
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["config"]["data_plane_mode"] == "local_decode"
        assert accepted.json()["config"]["min_free_memory_bytes"] == 268_435_456

        rejected = client.post(
            "/admin/v1/plugin-configurations",
            json={
                "plugin_id": "org.sensoryplex.vlm-moondream",
                "name": "wrong-vlm-path",
                "config": {"data_plane_mode": "per_request"},
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["reason_code"] == "console_vlm_config_requires_local_decode"


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


def multimodal_pipeline(client):
    """创建并发布供 Agent v2 接口验证的不可变多模态 Revision。"""
    config_ids = {}
    for key, plugin_id in (
        ("ocr_fast", "org.sensoryplex.ocr-rapidocr"),
        ("asr_fast", "org.sensoryplex.asr-whisper-mlx"),
        ("vlm_enrich", "org.sensoryplex.vlm-moondream"),
    ):
        response = client.post(
            "/admin/v1/plugin-configurations",
            json={"plugin_id": plugin_id, "name": f"agent-{key}", "config": {}},
        )
        assert response.status_code == 201, response.text
        config_ids[key] = response.json()["id"]
    response = client.post(
        "/admin/v1/multimodal-pipelines",
        json={
            "name": "agent-v2",
            "description": "agent task contract",
            "components": [
                {"node_id": key, "config_id": value} for key, value in config_ids.items()
            ],
            "policy": {
                "window_ms": 1000,
                "sample_interval_ms": 1000,
                "audio_segment_ms": 6000,
                "audio_overlap_ms": 500,
                "vlm_sample_interval_ms": 5000,
            },
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    published = client.post(f"/admin/v1/pipelines/{plan['id']}:publish")
    assert published.status_code == 200, published.text
    return plan


def enroll_v2_agent(client, *, node_id="v2-agent"):
    token = client.post(
        "/admin/v1/nodes/enrollment-tokens",
        json={"node_id": node_id, "expires_in_minutes": 30},
    ).json()["token"]
    enrolled = client.post(
        "/v1/agent/enroll",
        json={
            "enrollment_token": token,
            "node_id": node_id,
            "display_name": node_id,
            "is_co_located": True,
            "capabilities": {
                "platform": "macos",
                "arch": "aarch64",
                "cpu_cores": 8,
                "memory_bytes": "17179869184",
                "supported_artifacts": ["local_native"],
            },
        },
    )
    assert enrolled.status_code == 200, enrolled.text
    return enrolled.json()["session_token"]


def activate_v2_plugins(console_database, *, pipeline_id, node_id):
    """仅构造热部署台账事实，供 API 契约测试通过调度前置核验。

    这里不模拟模型调用；真正的 Runtime/插件调用由宿主机 `TaskExecutor` 与媒体验收覆盖。
    """
    with psycopg.connect(console_database) as conn:
        definition = conn.execute(
            """
            SELECT revision.definition_json
            FROM console_pipeline pipeline
            JOIN pipeline_revision revision
              ON revision.pipeline_id=pipeline.orchestration_pipeline_id
             AND revision.revision=pipeline.orchestration_revision
            WHERE pipeline.id=%s
            """,
            (pipeline_id,),
        ).fetchone()[0]
        for node in definition["nodes"]:
            if node["id"] == "timeline_fusion":
                continue
            suffix = uuid.uuid4().hex
            slot_id = f"slot-{suffix}"
            release_id = f"release-{suffix}"
            runtime_id = f"runtime-{suffix}"
            bundle_digest = (
                "sha256:" + hashlib.sha256(f"bundle:{node['plugin_id']}".encode()).hexdigest()
            )
            auxiliary_digest = (
                "sha256:" + hashlib.sha256(f"release:{node['plugin_id']}".encode()).hexdigest()
            )
            conn.execute(
                """
                INSERT INTO plugin_release(
                    release_id,plugin_id,plugin_version,platform,arch,form,artifact_digest,
                    bundle_digest,manifest_digest,config_schema_digest,sbom_digest,bundle_bytes,
                    entrypoint,runtime_requirements,trust,authenticated,authentication_method,
                    signature_status,bundle_path,created_by
                ) VALUES (%s,%s,%s,'macos','aarch64','local_native',%s,%s,%s,%s,%s,1,
                          %s,%s,'first_party',true,'test','valid','test.bundle','admin')
                """,
                (
                    release_id,
                    node["plugin_id"],
                    node["plugin_version"],
                    node["artifact_digest"],
                    bundle_digest,
                    auxiliary_digest,
                    auxiliary_digest,
                    auxiliary_digest,
                    Jsonb({}),
                    Jsonb({}),
                ),
            )
            conn.execute(
                """
                INSERT INTO console_plugin_instance(
                    instance_id,node_id,plugin_id,plugin_version,artifact_digest,desired_state,
                    actual_state,config_hash,created_by
                ) VALUES (%s,%s,%s,%s,%s,'ready','ready',%s,'admin')
                """,
                (
                    slot_id,
                    node_id,
                    node["plugin_id"],
                    node["plugin_version"],
                    node["artifact_digest"],
                    node["config_hash"],
                ),
            )
            conn.execute(
                """
                INSERT INTO plugin_runtime_instance(
                    runtime_instance_id,instance_id,node_id,plugin_id,release_id,artifact_digest,
                    bundle_digest,generation,role,state,endpoint
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,1,'active','active','127.0.0.1:1')
                """,
                (
                    runtime_id,
                    slot_id,
                    node_id,
                    node["plugin_id"],
                    release_id,
                    node["artifact_digest"],
                    bundle_digest,
                ),
            )
            conn.execute(
                """
                UPDATE console_plugin_instance
                SET active_runtime_instance_id=%s,active_release_id=%s,generation=1,
                    endpoint='127.0.0.1:1'
                WHERE instance_id=%s
                """,
                (runtime_id, release_id, slot_id),
            )


def task_receipt(manifest, *, reason=""):
    """按 API 的 canonical digest 生成测试回执，不用客户端响应伪造身份。"""
    started_ms, completed_ms = 1_770_000_000_000, 1_770_000_000_001
    task = manifest["task"]
    plugin = manifest["plugin"]
    started_at = datetime.fromtimestamp(started_ms / 1000, tz=UTC).isoformat()
    completed_at = datetime.fromtimestamp(completed_ms / 1000, tz=UTC).isoformat()
    fields = {
        "run_id": manifest["run"]["run_id"],
        "task_id": task["task_id"],
        "attempt": task["attempt"],
        "assignment_id": task["assignment_id"],
        "plugin_id": plugin["plugin_id"],
        "artifact_digest": plugin["artifact_digest"],
        "config_hash": plugin["config_hash"],
        "input_count": 0,
        "output_count": 0,
        "result_manifest_ref": "agent-result:test",
        "reason_code": reason,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return {
        **{
            key: value for key, value in fields.items() if key not in {"started_at", "completed_at"}
        },
        "receipt_digest": digest,
        "started_at_unix_ms": started_ms,
        "completed_at_unix_ms": completed_ms,
    }


def agent_result_payload(manifest, *, success=True, reason=""):
    task = manifest["task"]
    return {
        "intent_id": manifest["intent_id"],
        "run_id": manifest["run"]["run_id"],
        "attempt": task["attempt"],
        "assignment_id": task["assignment_id"],
        "success": success,
        "output_ref": "agent-result:test",
        "retryable": False,
        "reason_code": reason,
        "error_detail": "",
        "receipt": task_receipt(manifest, reason=reason),
    }


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


def test_multimodal_pipeline_revision_locks_delayed_vlm_without_blocking_fast_path(console_app):
    """VLM 身份随 Revision 锁定为延迟补全，只有 L1 节点必须在派发前 ready。"""
    with TestClient(console_app) as client:
        login(client)

        config_ids = {}
        for key, plugin_id in (
            ("ocr_fast", "org.sensoryplex.ocr-rapidocr"),
            ("asr_fast", "org.sensoryplex.asr-whisper-mlx"),
            ("vlm_enrich", "org.sensoryplex.vlm-moondream"),
        ):
            response = client.post(
                "/admin/v1/plugin-configurations",
                json={"plugin_id": plugin_id, "name": f"v2-{key}", "config": {}},
            )
            assert response.status_code == 201, response.text
            config_ids[key] = response.json()["id"]

        body = {
            "name": "multimodal-file",
            "description": "1-second execution contract",
            "components": [
                {"node_id": key, "config_id": value} for key, value in config_ids.items()
            ],
            "policy": {
                "window_ms": 1000,
                "sample_interval_ms": 1000,
                "audio_segment_ms": 6000,
                "audio_overlap_ms": 500,
                "vlm_sample_interval_ms": 5000,
            },
        }
        validated = client.post("/admin/v1/multimodal-pipelines:validate", json=body)
        assert validated.status_code == 200, validated.text
        assert validated.json()["valid"] is True
        unsupported_overlap = client.post(
            "/admin/v1/multimodal-pipelines:validate",
            json={**body, "policy": {**body["policy"], "audio_overlap_ms": 6000}},
        )
        assert unsupported_overlap.status_code == 422
        assert unsupported_overlap.json()["reason_code"] == "invalid_multimodal_audio_overlap"
        graph_digest = validated.json()["graph_digest"]
        nodes = validated.json()["nodes"]
        assert {item["id"] for item in nodes} == {
            "ocr_fast",
            "asr_fast",
            "timeline_fusion",
        }
        timeline = next(item for item in nodes if item["id"] == "timeline_fusion")
        assert len(timeline["delayed_enrichments"]) == 1
        delayed_vlm = timeline["delayed_enrichments"][0]
        assert {key: delayed_vlm[key] for key in ("id", "plugin_id", "required", "placement")} == {
            "id": "vlm_enrich",
            "plugin_id": "org.sensoryplex.vlm-moondream",
            "required": False,
            "placement": "data_plane_local",
        }

        plan = client.post("/admin/v1/multimodal-pipelines", json=body)
        assert plan.status_code == 201, plan.text
        assert plan.json()["execution_mode"] == "orchestrated_v2"
        assert plan.json()["graph_digest"] == graph_digest
        assert client.post(f"/admin/v1/pipelines/{plan.json()['id']}:publish").status_code == 200

        asset_id, _ = upload(client)
        draft = client.post(
            "/v1/job-drafts",
            json={
                "name": "v2 should preflight",
                "asset_id": asset_id,
                "pipeline_id": plan.json()["id"],
            },
        ).json()
        token = client.post(
            "/admin/v1/nodes/enrollment-tokens",
            json={"node_id": "v2-worker", "expires_in_minutes": 30},
        ).json()["token"]
        enrolled = client.post(
            "/v1/agent/enroll",
            json={
                "enrollment_token": token,
                "node_id": "v2-worker",
                "display_name": "v2 worker",
                "is_co_located": True,
                "capabilities": {
                    "platform": "macos",
                    "arch": "aarch64",
                    "cpu_cores": 8,
                    "memory_bytes": "17179869184",
                    "supported_artifacts": ["local_native"],
                },
            },
        )
        assert enrolled.status_code == 200, enrolled.text
        blocked = client.post(
            f"/v1/job-drafts/{draft['id']}:dispatch", json={"node_id": "v2-worker"}
        )
        assert blocked.status_code == 409
        assert blocked.json()["reason_code"] == "plugin_instance_unavailable"


def test_v2_agent_manifest_receipt_and_delivery(console_app, console_database):
    """v2 Agent 只能以受绑定 manifest 和合法 receipt 完成 delivery intent。"""
    with TestClient(console_app) as client:
        login(client)
        plan = multimodal_pipeline(client)
        session_token = enroll_v2_agent(client)
        activate_v2_plugins(console_database, pipeline_id=plan["id"], node_id="v2-agent")
        asset_id, media = upload(client)
        drafted = client.post(
            "/v1/job-drafts",
            json={"name": "agent manifest", "asset_id": asset_id, "pipeline_id": plan["id"]},
        )
        assert drafted.status_code == 201, drafted.text
        dispatched = client.post(f"/v1/job-drafts/{drafted.json()['id']}:dispatch")
        assert dispatched.status_code == 200, dispatched.text

        agent_headers = {"Authorization": f"Bearer {session_token}"}
        heartbeat = client.post(
            "/v1/agent/heartbeat",
            json={"node_id": "v2-agent", "session_token": session_token},
        )
        assert heartbeat.status_code == 200, heartbeat.text
        pending_intents = heartbeat.json()["pending_intents"]
        assert len(pending_intents) == 2
        with psycopg.connect(console_database) as conn:
            pending_nodes = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT task.node_id
                    FROM console_deployment_intent intent
                    JOIN pipeline_task task ON task.task_id=intent.config->>'task_id'
                    WHERE intent.job_id=%s
                    """,
                    (drafted.json()["id"],),
                )
            }
            assert pending_nodes == {"ocr_fast", "asr_fast"}
            ocr_intent = conn.execute(
                """
                SELECT intent.id
                FROM console_deployment_intent intent
                JOIN pipeline_task task ON task.task_id=intent.config->>'task_id'
                WHERE intent.job_id=%s AND task.node_id='ocr_fast'
                """,
                (drafted.json()["id"],),
            ).fetchone()[0]
        manifest = client.get(
            f"/v1/agent/task-intents/{ocr_intent}/manifest", headers=agent_headers
        )
        assert manifest.status_code == 200, manifest.text
        manifest = manifest.json()
        assert manifest["plugin"]["runtime_instance_id"]
        assert "endpoint" not in manifest["plugin"]
        assert (
            client.get(f"/v1/agent/task-intents/{ocr_intent}/asset", headers=agent_headers).content
            == media
        )

        missing_receipt = client.post(
            f"/v1/agent/tasks/{manifest['task']['task_id']}:result",
            headers=agent_headers,
            json={"intent_id": ocr_intent},
        )
        assert missing_receipt.status_code == 422
        bad = agent_result_payload(manifest)
        bad["receipt"]["receipt_digest"] = "sha256:" + "0" * 64
        assert (
            client.post(
                f"/v1/agent/tasks/{manifest['task']['task_id']}:result",
                headers=agent_headers,
                json=bad,
            ).status_code
            == 422
        )
        accepted = client.post(
            f"/v1/agent/tasks/{manifest['task']['task_id']}:result",
            headers=agent_headers,
            json=agent_result_payload(manifest),
        )
        assert accepted.status_code == 200, accepted.text
        delivered = client.post(
            "/v1/agent/report",
            headers=agent_headers,
            json={
                "intent_id": ocr_intent,
                "instance_id": "",
                "node_id": "v2-agent",
                "action": "DEPLOYMENT_ACTION_TASK_PROCESS",
                "success": True,
                "actual_state": "PLUGIN_INSTANCE_STATE_READY",
            },
        )
        assert delivered.status_code == 200, delivered.text
        capabilities = {
            item["name"]: item for item in client.get("/v1/capabilities").json()["capabilities"]
        }
        assert capabilities["task_execution"] == {
            "name": "task_execution",
            "available": True,
            "reason": "",
        }

    with psycopg.connect(console_database) as conn:
        assert (
            conn.execute(
                "SELECT state FROM console_deployment_intent WHERE id=%s", (ocr_intent,)
            ).fetchone()[0]
            == "completed"
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM task_execution_receipt WHERE task_id=%s",
                (manifest["task"]["task_id"],),
            ).fetchone()[0]
            == 1
        )


def test_v2_timeline_allows_empty_materials_but_requires_coverage(console_app, console_database):
    """没有 Observation 的片段不造假素材，却必须有完整的 1 秒覆盖事实。"""
    with TestClient(console_app) as client:
        login(client)
        plan = multimodal_pipeline(client)
        session_token = enroll_v2_agent(client, node_id="coverage-agent")
        activate_v2_plugins(console_database, pipeline_id=plan["id"], node_id="coverage-agent")
        asset_id, media = upload(client)
        drafted = client.post(
            "/v1/job-drafts",
            json={"name": "coverage only", "asset_id": asset_id, "pipeline_id": plan["id"]},
        )
        assert drafted.status_code == 201, drafted.text
        assert client.post(f"/v1/job-drafts/{drafted.json()['id']}:dispatch").status_code == 200
        agent_headers = {"Authorization": f"Bearer {session_token}"}
        initial = client.post(
            "/v1/agent/heartbeat",
            json={"node_id": "coverage-agent", "session_token": session_token},
        )
        assert initial.status_code == 200, initial.text

        with psycopg.connect(console_database) as conn:
            task_intents = dict(
                conn.execute(
                    """
                    SELECT task.node_id,intent.id
                    FROM console_deployment_intent intent
                    JOIN pipeline_task task ON task.task_id=intent.config->>'task_id'
                    WHERE intent.job_id=%s
                    """,
                    (drafted.json()["id"],),
                ).fetchall()
            )
        for node_id in ("ocr_fast", "asr_fast"):
            intent_id = task_intents[node_id]
            manifest_response = client.get(
                f"/v1/agent/task-intents/{intent_id}/manifest", headers=agent_headers
            )
            assert manifest_response.status_code == 200, manifest_response.text
            manifest = manifest_response.json()
            assert (
                client.post(
                    f"/v1/agent/tasks/{manifest['task']['task_id']}:result",
                    headers=agent_headers,
                    json=agent_result_payload(manifest),
                ).status_code
                == 200
            )
            assert (
                client.post(
                    "/v1/agent/report",
                    headers=agent_headers,
                    json={
                        "intent_id": intent_id,
                        "instance_id": "",
                        "node_id": "coverage-agent",
                        "action": "DEPLOYMENT_ACTION_TASK_PROCESS",
                        "success": True,
                        "actual_state": "PLUGIN_INSTANCE_STATE_READY",
                    },
                ).status_code
                == 200
            )

        scheduled = client.post(
            "/v1/agent/heartbeat",
            json={"node_id": "coverage-agent", "session_token": session_token},
        )
        assert scheduled.status_code == 200, scheduled.text
        with psycopg.connect(console_database) as conn:
            timeline_intent = conn.execute(
                """
                SELECT intent.id
                FROM console_deployment_intent intent
                JOIN pipeline_task task ON task.task_id=intent.config->>'task_id'
                WHERE intent.job_id=%s AND task.node_id='timeline_fusion'
                """,
                (drafted.json()["id"],),
            ).fetchone()[0]
        timeline_response = client.get(
            f"/v1/agent/task-intents/{timeline_intent}/manifest", headers=agent_headers
        )
        assert timeline_response.status_code == 200, timeline_response.text
        timeline = timeline_response.json()
        before_coverage = client.post(
            f"/v1/agent/tasks/{timeline['task']['task_id']}:result",
            headers=agent_headers,
            json=agent_result_payload(timeline),
        )
        assert before_coverage.status_code == 409
        content_hash = "sha256:" + hashlib.sha256(media).hexdigest()
        source = media_pb2.MediaSourceDescription(
            source=media_pb2.MediaSourceRef(
                stream_id=f"stream-{asset_id}",
                source_id=f"source-{asset_id}",
                kind=media_pb2.MEDIA_SOURCE_KIND_FILE,
                content_hash=content_hash,
            ),
            tracks=[media_pb2.MediaTrack(track_kind="video", codec="h264", timing_known=True)],
            duration_ms=9056,
            probe_tool="test-contract",
        )
        coverage = [
            {
                "start_ms": start_ms,
                "end_ms": min(start_ms + 1000, 9056),
                "sampling_state": "not_sampled_by_policy",
                "modality_states": {
                    "ocr": "not_sampled_by_policy",
                    "asr": "not_scheduled",
                    "vlm": "not_sampled_by_policy",
                },
                "reason_codes": {},
            }
            for start_ms in range(0, 9056, 1000)
        ]
        ingested = client.post(
            f"/v1/agent/tasks/{timeline['task']['task_id']}:timeline",
            headers=agent_headers,
            json={
                "intent_id": timeline_intent,
                "source_description_b64": base64.b64encode(
                    source.SerializeToString(deterministic=True)
                ).decode(),
                "materials_b64": [],
                "timeline_items": [],
                "coverage": coverage,
            },
        )
        assert ingested.status_code == 200, ingested.text
        assert ingested.json()["materials"] == 0
        assert ingested.json()["coverage_windows"] == 10
        assert (
            client.post(
                f"/v1/agent/tasks/{timeline['task']['task_id']}:result",
                headers=agent_headers,
                json=agent_result_payload(timeline),
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/agent/report",
                headers=agent_headers,
                json={
                    "intent_id": timeline_intent,
                    "instance_id": "",
                    "node_id": "coverage-agent",
                    "action": "DEPLOYMENT_ACTION_TASK_PROCESS",
                    "success": True,
                    "actual_state": "PLUGIN_INSTANCE_STATE_READY",
                },
            ).status_code
            == 200
        )

    with psycopg.connect(console_database) as conn:
        last_window = conn.execute(
            """
            SELECT start_ms,end_ms FROM timeline_window_state
            WHERE execution_id=(
                SELECT execution_id FROM console_job_execution WHERE job_id=%s
            ) ORDER BY start_ms DESC LIMIT 1
            """,
            (drafted.json()["id"],),
        ).fetchone()
        assert last_window == (9000, 9056)
        assert conn.execute("SELECT count(*) FROM material_unit").fetchone()[0] == 0


def test_task_dispatch_surfaces_unattached_runtime(console_app, console_database):
    """任务意图必须有明确动作与终态失败，不能无限停在 processing。"""
    with TestClient(console_app) as client:
        login(client)
        cfg = config(client)
        plan = pipeline(client, cfg)
        asset_id, _ = upload(client)
        draft = client.post(
            "/v1/job-drafts",
            json={"name": "runtime unavailable", "asset_id": asset_id, "pipeline_id": plan["id"]},
        ).json()

        token = client.post(
            "/admin/v1/nodes/enrollment-tokens",
            json={"node_id": "task-worker", "expires_in_minutes": 30},
        ).json()["token"]
        session_token = client.post(
            "/v1/agent/enroll",
            json={
                "enrollment_token": token,
                "node_id": "task-worker",
                "display_name": "Task worker",
                "is_co_located": True,
                "capabilities": {
                    "platform": "macos",
                    "arch": "aarch64",
                    "cpu_cores": 8,
                    "memory_bytes": "17179869184",
                    "supported_artifacts": ["local_native"],
                },
            },
        ).json()["session_token"]

        dispatched = client.post(f"/v1/job-drafts/{draft['id']}:dispatch")
        assert dispatched.status_code == 200, dispatched.text
        assert dispatched.json()["state"] == "processing"

        heartbeat = client.post(
            "/v1/agent/heartbeat",
            json={"node_id": "task-worker", "session_token": session_token},
        )
        assert heartbeat.status_code == 200, heartbeat.text
        intent = heartbeat.json()["pending_intents"]
        assert len(intent) == 1
        assert intent[0]["action"] == "DEPLOYMENT_ACTION_TASK_PROCESS"

        reported = client.post(
            "/v1/agent/report",
            json={
                "intent_id": intent[0]["intent_id"],
                "instance_id": "",
                "node_id": "task-worker",
                "action": "DEPLOYMENT_ACTION_TASK_PROCESS",
                "success": False,
                "actual_state": "PLUGIN_INSTANCE_STATE_UNSPECIFIED",
                "error_code": "runtime_task_service_not_attached",
                "error_detail": "task_process requires a controlled runtime executor",
            },
        )
        assert reported.status_code == 200, reported.text

        listed = client.get("/v1/job-drafts").json()["items"]
        visible = next(item for item in listed if item["id"] == draft["id"])
        assert visible["state"] == "failed"
        assert visible["reason"] == "runtime_task_service_not_attached"
        capability_items = client.get("/v1/capabilities").json()["capabilities"]
        capabilities = {item["name"]: item for item in capability_items}
        assert capabilities["task_execution"] == {
            "name": "task_execution",
            "available": True,
            "reason": "",
        }

    with psycopg.connect(console_database) as conn:
        intent_state, intent_code = conn.execute(
            "SELECT state,error_code FROM console_deployment_intent WHERE job_id=%s", (draft["id"],)
        ).fetchone()
        assert (intent_state, intent_code) == ("failed", "runtime_task_service_not_attached")


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

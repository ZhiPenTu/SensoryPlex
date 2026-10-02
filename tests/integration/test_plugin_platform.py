"""通用插件的真实 PostgreSQL/API 边界；此处夹具不是媒体 E2E。"""

import base64
import time

import psycopg
import pytest
from edge_material_sdk.cli import create
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as pb
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.manifest import load_manifest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from sensoryplex_api.contracts import one
from sensoryplex_api.infrastructure import enrichments, orchestration, second_windows
from sensoryplex_api.interfaces.enrichments import register
from sensoryplex_api.settings import Settings

from tests.integration.test_vlm_delayed import (
    ASSET_ID,
    DIGEST,
    _seed_fast_path,
)
from tests.integration.test_vlm_delayed import delayed_database as delayed_database

pytestmark = pytest.mark.integration


@pytest.fixture
def platform(delayed_database, tmp_path):
    project = tmp_path / "external"
    create(project, "com.example.test")
    registration = load_manifest(project)
    output = registration["manifest"]["spec"]["outputs"][0]
    with psycopg.connect(delayed_database) as conn:
        _seed_fast_path(conn)
        conn.execute(
            "INSERT INTO plugin_release(release_id,plugin_id,plugin_version,platform,arch,form,"
            "artifact_digest,bundle_digest,manifest_digest,config_schema_digest,sbom_digest,"
            "bundle_bytes,entrypoint,runtime_requirements,trust,authenticated,authentication_method,"
            "signature_status,bundle_path,created_by) VALUES ('release','com.example.test','0.1.0',"
            "'macos','aarch64','local_native',%s,%s,%s,%s,%s,1,'{}','{}','trusted_publisher',"
            "true,'ed25519','verified','test/bundle','owner')",
            (DIGEST,) * 5,
        )
        conn.execute(
            "INSERT INTO plugin_registration(release_id,manifest,config_schema,schemas) "
            "VALUES ('release',%s,%s,%s)",
            tuple(Jsonb(registration[k]) for k in ("manifest", "config_schema", "schemas")),
        )
        conn.execute(
            "INSERT INTO console_plugin_instance(instance_id,node_id,plugin_id,plugin_version,"
            "artifact_digest,config,config_hash,desired_state,actual_state,created_by) "
            "VALUES ('slot','node','com.example.test','0.1.0',%s,'{}',%s,'ready','ready','owner')",
            (DIGEST, DIGEST),
        )
        conn.execute(
            "INSERT INTO plugin_runtime_instance(runtime_instance_id,instance_id,node_id,"
            "plugin_id,release_id,artifact_digest,bundle_digest,generation,role,state,endpoint,"
            "config_hash) VALUES ('runtime','slot','node','com.example.test','release',%s,%s,1,"
            "'active','active','127.0.0.1:1234',%s)",
            (DIGEST,) * 3,
        )
        units = second_windows.ensure_materials(
            conn, execution_id="execution", asset_id=ASSET_ID, pending=[output["modality"]]
        )
        task = pb.EnrichmentTask(
            task_id="task",
            execution_id="execution",
            run_id="run",
            node_id="node",
            data_plane_node_id="node",
            route_id="route",
            release_id="release",
            config_id="config",
            plugin={
                "plugin_id": "com.example.test",
                "version": "0.1.0",
                "artifact_digest": DIGEST,
                "config_hash": DIGEST,
            },
            asset_id=ASSET_ID,
            stream_id="stream",
            source_id="source",
            material_unit_id=units[0].material_unit_id,
            time_range={"start_ms": 0, "end_ms": 1000},
            content_hash=DIGEST,
            max_attempts=2,
            deadline_unix_ms=int(time.time() * 1000) + 60000,
            input_modality="media.video_frame",
            process_timeout_ms=30000,
        )
        conn.execute(
            "INSERT INTO enrichment_task(task_id,execution_id,run_id,node_id,target_node_id,"
            "route_id,release_id,config_id,material_unit_id,start_ms,end_ms,manifest_bytes,"
            "deadline_unix_ms) VALUES ('task','execution','run','node','node','route','release',"
            "'config',%s,0,1000,%s,%s)",
            (
                task.material_unit_id,
                task.SerializeToString(deterministic=True),
                task.deadline_unix_ms,
            ),
        )
        conn.execute(
            "INSERT INTO enrichment_outbox(task_id,subject,contract_bytes) VALUES ('task',%s,%s)",
            (enrichments.TASK_PREFIX + "route", task.SerializeToString(deterministic=True)),
        )
    with ConnectionPool(delayed_database) as pool:
        app = FastAPI()

        def authenticate(conn, authorization, node):
            if authorization != "Bearer node-session" or node != "node":
                raise HTTPException(401, "invalid_node_credentials")

        register(app, pool, Settings(_env_file=None), authenticate)
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer node-session"
            yield client, delayed_database, task


def no_observations(task):
    result = pb.EnrichmentResult(
        task=task,
        outcome=runtime.PROCESS_OUTCOME_NO_OBSERVATIONS,
        outcome_reason="below_threshold",
        input_receipts=[
            {
                "source_item_id": "buf-task@" + DIGEST[7:23],
                "content_hash": DIGEST,
                "kind": "video_frame",
                "time_range": {"start_ms": 0, "end_ms": 33},
            }
        ],
    )
    result.result_digest = enrichments.digest_result(result)
    return result


def stage(client, result, lease):
    return client.post(
        "/v1/agent/enrichments/task:result",
        json={
            "lease_id": lease,
            "result_b64": base64.b64encode(result.SerializeToString()).decode(),
        },
    )


@pytest.mark.parametrize("reason", ["/private/media.mov", "failure: secret", "x" * 121])
def test_result_reason_rejects_unstructured_detail(platform, reason):
    client, _, task = platform
    acquired = client.post("/v1/agent/enrichments/task:claim").json()
    result = no_observations(task)
    result.outcome_reason = reason
    result.result_digest = enrichments.digest_result(result)
    response = stage(client, result, acquired["lease_id"])
    assert response.status_code == 422
    assert response.json()["detail"] == "plugin_result_reason_invalid"


def test_node_authorization_lease_fencing_and_crash_reclaim(platform):
    client, url, _ = platform
    assert (
        client.post(
            "/v1/agent/enrichments/task:claim", headers={"Authorization": "Bearer other"}
        ).status_code
        == 401
    )
    first = client.post("/v1/agent/enrichments/task:claim").json()
    assert first["attempt"] == 1 and first["runtime_instance_id"] == "runtime"
    assert client.post("/v1/agent/enrichments/task:claim").status_code == 409
    with psycopg.connect(url) as conn:
        conn.execute("UPDATE enrichment_task SET lease_expires_at=now()-interval '1 second'")
    second = client.post("/v1/agent/enrichments/task:claim").json()
    assert second["attempt"] == 2 and second["lease_id"] != first["lease_id"]
    assert (
        client.post(
            "/v1/agent/enrichments/task:renew", json={"lease_id": first["lease_id"]}
        ).status_code
        == 409
    )
    exhausted = client.post(
        "/v1/agent/enrichments/task:retry", json={"lease_id": second["lease_id"]}
    ).json()
    assert exhausted["terminal"]
    with psycopg.connect(url) as conn:
        assert one(conn, "SELECT state,reason_code FROM enrichment_task") == {
            "state": "failed",
            "reason_code": "enrichment_retries_exhausted",
        }
        assert (
            one(conn, "SELECT state FROM console_job_execution")["state"]
            == "succeeded_with_partial_enrichment"
        )


def test_duplicate_stage_and_fuse_append_no_fake_observation(platform):
    client, url, task = platform
    lease = client.post("/v1/agent/enrichments/task:claim").json()["lease_id"]
    result = no_observations(task)
    assert stage(client, result, lease).status_code == 200
    assert stage(client, result, lease).json()["duplicate"]
    reference = pb.EnrichmentResultReference(task_id="task", result_digest=result.result_digest)
    with psycopg.connect(url) as conn:
        assert enrichments.fuse(conn, reference) == "succeeded"
        count = conn.execute("SELECT count(*) FROM material_unit").fetchone()[0]
        assert enrichments.fuse(conn, reference) == "succeeded"
        assert count == conn.execute("SELECT count(*) FROM material_unit").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM observation").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM enrichment_result_outbox").fetchone()[0] == 1


def test_cancel_after_stage_discards_late_fusion(platform):
    client, url, task = platform
    lease = client.post("/v1/agent/enrichments/task:claim").json()["lease_id"]
    result = no_observations(task)
    assert stage(client, result, lease).status_code == 200
    with psycopg.connect(url) as conn:
        orchestration.cancel_pipeline_run(conn, "run", "owner", "acceptance")
        count = conn.execute("SELECT count(*) FROM material_unit").fetchone()[0]
        assert (
            enrichments.fuse(
                conn,
                pb.EnrichmentResultReference(task_id="task", result_digest=result.result_digest),
            )
            == "cancelled"
        )
        assert count == conn.execute("SELECT count(*) FROM material_unit").fetchone()[0]


def test_staged_fusion_rejection_closes_task_without_facts(platform):
    client, url, task = platform
    lease = client.post("/v1/agent/enrichments/task:claim").json()["lease_id"]
    result = no_observations(task)
    assert stage(client, result, lease).status_code == 200
    with psycopg.connect(url) as conn:
        wrong = pb.EnrichmentResultReference(task_id="task", result_digest="sha256:" + "f" * 64)
        assert not enrichments.reject_staged_result(conn, wrong, "payload_schema_invalid")
        assert one(conn, "SELECT state FROM enrichment_task")["state"] == "queued"
        reference = pb.EnrichmentResultReference(task_id="task", result_digest=result.result_digest)
        assert enrichments.reject_staged_result(conn, reference, "payload_schema_invalid")
        assert one(conn, "SELECT state,reason_code FROM enrichment_task") == {
            "state": "failed",
            "reason_code": "payload_schema_invalid",
        }
        assert conn.execute("SELECT count(*) FROM observation").fetchone()[0] == 0
        assert (
            one(conn, "SELECT state FROM console_job_execution")["state"]
            == "succeeded_with_partial_enrichment"
        )
        assert conn.execute("SELECT state FROM pipeline_run").fetchone()[0] == "succeeded"
    assert stage(client, result, lease).json()["duplicate"]
    conflict = no_observations(task)
    conflict.outcome_reason = "other_reason"
    conflict.result_digest = enrichments.digest_result(conflict)
    assert stage(client, conflict, lease).status_code == 409


@pytest.mark.parametrize("mismatch", ["manifest", "empty", "source", "digest"])
def test_invalid_result_cannot_enter_outbox(platform, mismatch):
    client, url, task = platform
    lease = client.post("/v1/agent/enrichments/task:claim").json()["lease_id"]
    result = no_observations(task)
    if mismatch == "manifest":
        result.task.plugin.version = "other"
    elif mismatch == "empty":
        result.outcome_reason = ""
    elif mismatch == "source":
        result.input_receipts[0].source_item_id = "other"
    if mismatch != "digest":
        result.result_digest = enrichments.digest_result(result)
    else:
        result.result_digest = "sha256:" + "b" * 64
    assert stage(client, result, lease).status_code == 422
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT count(*) FROM enrichment_result_outbox").fetchone()[0] == 0


def test_expired_deadline_is_terminal_without_fetching_media(platform, monkeypatch):
    client, url, task = platform
    monkeypatch.setattr(time, "time", lambda: task.deadline_unix_ms / 1000 + 1)
    result = client.post("/v1/agent/enrichments/task:claim").json()
    assert result["terminal"] and result["reason_code"] == "enrichment_deadline_exceeded"
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT attempt FROM enrichment_task").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_outbox_publish_does_not_hold_database_row_locks(platform):
    from sensoryplex_api.enrichment_service import publish_tick

    _, url, _ = platform

    class Bus:
        async def publish(self, subject, data, **kwargs):
            with psycopg.connect(url) as conn:
                conn.execute("SET LOCAL lock_timeout='200ms'")
                assert one(conn, "SELECT task_id FROM enrichment_outbox FOR UPDATE NOWAIT")
            assert kwargs["headers"]["Nats-Msg-Id"] == "enrichment_outbox:task"

    await publish_tick(Bus(), url)
    with psycopg.connect(url) as conn:
        assert (
            one(conn, "SELECT published_at,claim_until,attempts FROM enrichment_outbox")["attempts"]
            == 1
        )


def test_retirement_preserves_inflight_pin_and_immutable_revision(platform):
    from sensoryplex_api.infrastructure.runtime_bindings import runtime_is_pinned

    _, url, _ = platform
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO pipeline_revision"
            "(pipeline_id,revision,graph_digest,definition_json,created_by) "
            "VALUES ('pipeline',2,%s,%s,'owner')",
            (DIGEST, Jsonb({"nodes": [{"release_id": "release", "config_hash": DIGEST}]})),
        )
        runtime_instance = one(conn, "SELECT * FROM plugin_runtime_instance")
        assert runtime_is_pinned(conn, runtime_instance)
        conn.execute("INSERT INTO pipeline_revision_retirement VALUES ('pipeline',2,'owner',now())")
        assert not runtime_is_pinned(conn, runtime_instance)
        conn.execute(
            "INSERT INTO pipeline_run(run_id,pipeline_id,revision,input_ref,idempotency_key,"
            "deadline_unix_ms,state,owner) VALUES ('inflight','pipeline',2,'asset','key',"
            "9999999999999,'running','owner')"
        )
        assert runtime_is_pinned(conn, runtime_instance)
        with pytest.raises(HTTPException, match="pipeline_revision_retired"):
            orchestration.submit_pipeline_run(conn, "owner", "pipeline", 2, "asset", "another")
        conn.execute("UPDATE pipeline_run SET state='succeeded' WHERE run_id='inflight'")
        assert not runtime_is_pinned(conn, runtime_instance)


def test_execution_scoped_latest_survives_other_execution_revision(platform):
    from edge_material_sdk.generated.gateway.v1 import gateway_pb2
    from edge_material_sdk.generated.material.v1 import material_pb2
    from sensoryplex_api.infrastructure import materials

    _, url, _ = platform
    with psycopg.connect(url) as conn:
        unit = material_pb2.MaterialUnit(
            material_unit_id="scoped",
            stream_id="stream",
            revision=1,
            time_range={"start_ms": 0, "end_ms": 1000},
            status="partial",
        )
        for revision in [1, 2]:
            unit.revision = revision
            conn.execute(
                "INSERT INTO material_unit(material_unit_id,revision,stream_id,start_ms,"
                "end_ms,status,search_text,contract_bytes,content_hash) "
                "VALUES ('scoped',%s,'stream',0,1000,'partial','',%s,%s)",
                (revision, unit.SerializeToString(), DIGEST),
            )
        conn.execute("INSERT INTO material_execution VALUES ('scoped',1,'execution')")
        assert materials.get_material(conn, "owner", "scoped").revision == 2
        assert (
            materials.get_material(conn, "owner", "scoped", execution_id="execution").revision == 1
        )
        values = materials.search_materials(
            conn, "owner", gateway_pb2.SearchRequest(execution_id="execution")
        )
        scoped = [
            (m.material_unit_id, m.revision) for m in values if m.material_unit_id == "scoped"
        ]
        assert scoped == [("scoped", 1)]

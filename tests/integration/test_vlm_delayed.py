"""VLM 延迟满足写侧：真实 PostgreSQL 的事务、幂等和增量融合。"""

import os
import uuid

import psycopg
import pytest
from edge_material_sdk.generated.material.v1 import material_pb2
from edge_material_sdk.generated.media.v1 import media_pb2
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2
from edge_material_sdk.vlm_tasks import result_digest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from sensoryplex_api.infrastructure import vlm_delayed

from tools.migrate import migrate

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "a" * 64
ASSET_ID = "asset-" + "a" * 12


@pytest.fixture
def delayed_database():
    url = os.environ["SENSORYPLEX_TEST_DATABASE_URL"]
    schema = "vlm_delayed_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated = make_conninfo(url, options=f"-c search_path={schema}")
    try:
        migrate(isolated)
        yield isolated
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_fast_path(conn):
    """建立已完成的 L1 事实与两个已登记的时间锚点，不伪造模型结果。"""
    conn.execute(
        """
        INSERT INTO console_upload(id,owner,filename,size_bytes,content_type,state,sha256)
        VALUES ('upload','owner','authorized.mp4',1,'video/mp4','awaiting_admission',%s)
        """,
        (DIGEST,),
    )
    conn.execute(
        """
        INSERT INTO console_plugin_config(id,plugin_id,name,revision,config,config_hash,created_by)
        VALUES ('config','org.sensoryplex.vlm-moondream','vlm',1,%s,%s,'owner')
        """,
        (Jsonb({"prompt": vlm_delayed.DEFAULT_PROMPT}), DIGEST),
    )
    conn.execute(
        """
        INSERT INTO console_pipeline(
            id,name,description,plugin_id,plugin_digest,config_id,revision,created_by
        ) VALUES (
            'console-pipeline','pipeline','',
            'org.sensoryplex.vlm-moondream',%s,'config',1,'owner'
        )
        """,
        (DIGEST,),
    )
    conn.execute(
        """
        INSERT INTO console_job_draft(id,owner,asset_id,pipeline_id,name,state)
        VALUES ('job','owner','upload','console-pipeline','job','ready_for_review')
        """
    )
    conn.execute(
        "INSERT INTO pipeline_definition(pipeline_id,name,owner) VALUES ('pipeline','p','owner')"
    )
    revision = {
        "nodes": [
            {
                "id": "timeline_fusion",
                "execution_policy": {"vlm_sample_interval_ms": 1_000},
                "delayed_enrichments": [
                    {
                        "plugin_id": vlm_delayed.VLM_PLUGIN_ID,
                        "plugin_version": "0.1.1",
                        "artifact_digest": DIGEST,
                        "config_hash": DIGEST,
                        "config_id": "config",
                    }
                ],
            }
        ]
    }
    conn.execute(
        """
        INSERT INTO pipeline_revision(pipeline_id,revision,graph_digest,definition_json,created_by)
        VALUES ('pipeline',1,%s,%s,'owner')
        """,
        (DIGEST, Jsonb(revision)),
    )
    conn.execute(
        """
        INSERT INTO pipeline_run(
            run_id,pipeline_id,revision,input_ref,idempotency_key,deadline_unix_ms,
            state,owner,completed_at
        ) VALUES (
            'run','pipeline',1,'console_upload:upload','key',9999999999999,
            'succeeded','owner',now()
        )
        """
    )
    conn.execute(
        """
        INSERT INTO console_node(node_id,display_name,platform,arch)
        VALUES ('node','node','macos','aarch64')
        """
    )
    conn.execute(
        """
        INSERT INTO console_job_execution(
            execution_id,job_id,run_id,pipeline_id,pipeline_revision,graph_digest,
            target_node_id,input_ref,state
        ) VALUES (
            'execution','job','run','pipeline',1,%s,
            'node','console_upload:upload','ready_for_review'
        )
        """,
        (DIGEST,),
    )
    conn.execute(
        """
        INSERT INTO media_source(source_id,type,uri_redacted,owner)
        VALUES ('source','file','private://not-exposed','owner')
        """
    )
    conn.execute(
        """
        INSERT INTO stream_session(stream_id,source_id,started_at,status)
        VALUES ('stream','source',now(),'stopped')
        """
    )
    conn.execute(
        """
        INSERT INTO media_asset(asset_id,stream_id,object_uri,sha256,codec,duration_ms)
        VALUES (%s,'stream','upload://upload',%s,'video:h264',2000)
        """,
        (ASSET_ID, DIGEST),
    )
    for item_id, start_ms, end_ms in (("frame-0", 0, 33), ("frame-1", 1000, 1033)):
        conn.execute(
            """
            INSERT INTO timeline_item(item_id,stream_id,kind,start_ms,end_ms)
            VALUES (%s,'stream','video_frame',%s,%s)
            """,
            (item_id, start_ms, end_ms),
        )
        conn.execute(
            """
            INSERT INTO timeline_window_state(
                execution_id,stream_id,start_ms,end_ms,state_revision,sampling_state,
                modality_states,reason_codes
            ) VALUES ('execution','stream',%s,%s,1,'sampled',%s,'{}'::jsonb)
            """,
            (start_ms, start_ms + 1000, Jsonb({"ocr_blocks": "observed"})),
        )
    return revision


def _description():
    return media_pb2.MediaSourceDescription(
        source=media_pb2.MediaSourceRef(
            stream_id="stream",
            source_id="source",
            kind=media_pb2.MEDIA_SOURCE_KIND_FILE,
            content_hash=DIGEST,
        )
    )


def _success(task):
    observation = material_pb2.Observation(
        observation_id="obs-" + task.task_id,
        modality=vlm_delayed.VLM_MODALITY,
        stream_id=task.stream_id,
        source_id=task.source_id,
        source_item_id=task.source_item_id,
        time_range=task.time_range,
        content_hash=DIGEST,
        timing_source="media_pts",
        confidence_unavailable_reason="model_does_not_report_calibrated_confidence",
        quality_state="final",
        created_at_unix_ms=1,
        provenance=material_pb2.Provenance(
            plugin=vlm_delayed.VLM_PLUGIN_ID,
            plugin_version=task.plugin.version,
            artifact_digest=task.plugin.artifact_digest,
            model_release_id="model-release",
            model_id="moondream",
            model_version="v2",
            model_artifact_digest=DIGEST,
            execution_backend="ollama-test",
            config_hash=task.plugin.config_hash,
        ),
    )
    observation.payload.update({"text": "A real delayed scene description."})
    result = orchestration_pb2.VlmTaskResult(
        task=task,
        success=True,
        observation=observation,
        completed_at_unix_ms=1,
    )
    result.result_digest = result_digest(result)
    return result


def _failure(task):
    result = orchestration_pb2.VlmTaskResult(
        task=task,
        success=False,
        retryable=False,
        reason_code="model_rejected_input",
        completed_at_unix_ms=2,
    )
    result.result_digest = result_digest(result)
    return result


def test_fast_path_is_reviewable_before_delayed_results_and_fuser_is_idempotent(delayed_database):
    with psycopg.connect(delayed_database) as conn:
        revision = _seed_fast_path(conn)
        created = vlm_delayed.enqueue_vlm_tasks(
            conn,
            execution={"execution_id": "execution", "run_id": "run"},
            revision={"definition_json": revision},
            description=_description(),
            items={
                "frame-0": ("video_frame", 0, 33),
                "frame-1": ("video_frame", 1000, 1033),
            },
            units=[],
        )
        conn.commit()

        assert len(created) == 2
        assert (
            conn.execute("SELECT state FROM console_job_execution").fetchone()[0]
            == "ready_for_review"
        )
        contracts = [
            orchestration_pb2.TaskInputManifest.FromString(row[0])
            for row in conn.execute(
                "SELECT contract_bytes FROM vlm_task_outbox ORDER BY task_id"
            ).fetchall()
        ]
        assert {task.media_locator for task in contracts} == {f"media_asset:{ASSET_ID}"}
        assert all(not task.descriptor_ref and not task.observation_refs for task in contracts)

        forged_task = orchestration_pb2.TaskInputManifest()
        forged_task.CopyFrom(contracts[0])
        forged_task.plugin.version = "unpublished-version"
        with pytest.raises(vlm_delayed.VlmDelayedError, match="vlm_result_task_binding_invalid"):
            vlm_delayed.apply_vlm_result(conn, _success(forged_task))

        first = _success(contracts[0])
        assert vlm_delayed.apply_vlm_result(conn, first) == {
            "task_id": contracts[0].task_id,
            "replayed": False,
            "state": "succeeded",
        }
        assert vlm_delayed.apply_vlm_result(conn, first)["replayed"] is True

        second = _failure(contracts[1])
        assert vlm_delayed.apply_vlm_result(conn, second)["state"] == "failed"
        assert vlm_delayed.apply_vlm_result(conn, second)["replayed"] is True
        assert (
            conn.execute(
                "SELECT observation_id FROM vlm_enrichment_result WHERE task_id=%s",
                (contracts[1].task_id,),
            ).fetchone()[0]
            is None
        )

        assert conn.execute("SELECT count(*) FROM material_unit").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM event_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM console_job_execution").fetchone()[0] == (
            "succeeded_with_partial_enrichment"
        )
        assert conn.execute("SELECT state FROM console_job_draft").fetchone()[0] == "completed"

import time

import pytest
from edge_material_sdk.generated.common.v1.common_pb2 import TimeRange
from edge_material_sdk.generated.material.v1.material_pb2 import (
    MaterialUnit,
    Observation,
    Provenance,
    SourceReference,
)

DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def observation():
    """仅作为契约 fixture，不代表模型或媒体 E2E 的真实输出。"""
    obs = Observation(
        observation_id="obs_contract",
        modality="asr_segment",
        stream_id="stream_contract",
        source_id="source_contract",
        source_item_id="item_contract",
        time_range=TimeRange(start_ms=100, end_ms=200),
        confidence=0.9,
        quality_state="final",
        content_hash=DIGEST,
        timing_source="media_pts",
        created_at_unix_ms=int(time.time() * 1000),
        provenance=Provenance(
            plugin="org.sensoryplex.contract-test",
            plugin_version="0.1.0",
            artifact_digest=DIGEST,
            model_release_id="model_contract",
            model_id="contract-only",
            model_version="0.1.0",
            config_hash=DIGEST,
            execution_backend="contract-test",
            model_artifact_digest=DIGEST,
        ),
    )
    obs.payload.update({"text": "季度销售数据 100%"})
    return obs


@pytest.fixture
def material(observation):
    return MaterialUnit(
        material_unit_id="material_contract",
        stream_id=observation.stream_id,
        time_range=observation.time_range,
        status="fast_ready",
        revision=1,
        observations=[observation],
        tags=["财务"],
        pipeline_version="contract-test-v1",
        source_refs=[
            SourceReference(
                asset_id="asset_contract", time_range=observation.time_range, content_hash=DIGEST
            )
        ],
        pending_enrichments=["vlm"],
        created_at_unix_ms=observation.created_at_unix_ms,
    )

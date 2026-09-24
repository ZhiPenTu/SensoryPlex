import time

import pytest
from edge_material_sdk.generated.common.v1.common_pb2 import TimeRange
from edge_material_sdk.generated.material.v1.material_pb2 import (
    MaterialUnit,
    Observation,
    Provenance,
    SourceReference,
)
from sensoryplex_api.settings import Settings

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


# 检索面配置只从参数来：POC 容器栈（compose environment + 仓库 `.env`）是**真的**把
# SENSORYPLEX_INDEX_SEARCH_* 配上了。断言"未配置 / 半配置会被拒绝"的用例必须自己控制
# 环境，否则验的是宿主环境（还可能是别人机器上的环境），而不是这条契约。
INDEX_SEARCH_ENV = (
    "SENSORYPLEX_INDEX_SEARCH_ENDPOINT",
    "SENSORYPLEX_INDEX_SEARCH_TOKEN",
    "SENSORYPLEX_INDEX_SEARCH_TIMEOUT_S",
)


@pytest.fixture
def bare_settings(monkeypatch):
    """构造不受宿主/容器环境影响的 `Settings`（`_env_file=None` + 摘掉检索面变量）。

    显式传进来的 `index_search_*` 仍然生效——被挡掉的只有"环境替用例做决定"这件事。
    """

    def build(**overrides):
        for name in INDEX_SEARCH_ENV:
            monkeypatch.delenv(name, raising=False)
        return Settings(_env_file=None, **overrides)

    return build

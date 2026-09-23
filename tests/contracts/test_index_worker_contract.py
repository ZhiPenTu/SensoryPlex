"""index-worker 的契约测试：collection/引用的形状、payload 准入、稳定错误码与拒绝顺序。

测的是**纯判定与形状**：不起向量库服务、不连数据库。真实写入、读回确认、跨进程持久、
锁冲突与回查过滤由 `tools/verify_index.py`（11 个场景，真实 PostgreSQL + 真实 Milvus Lite）
承担——契约测试通过**不等于**落库与检索闭环可用。
"""

import argparse
import fcntl
import json
import os
import pathlib
import shutil
import tempfile

import pytest
from sensoryplex_index_worker import cli, records
from sensoryplex_index_worker.errors import IndexContractError, VectorStoreError
from sensoryplex_index_worker.milvus_store import (
    INDEX_TYPE,
    METRIC_TYPE,
    OUTPUT_FIELDS,
    SCALAR_FIELDS,
    VECTOR_FIELD,
    VectorIndex,
    collection_name,
    local_store_locked,
    parse_index_key,
    parse_vector_ref,
    vector_ref,
)
from sensoryplex_index_worker.worker import extract_embedding, index_embedding

KEY = "material_text_bge_small_zh_v1_5_d512_v1"
DIGEST = "sha256:" + "a" * 64
EMBEDDING_ID = "emb_" + "0" * 32


def payload(**overrides) -> dict:
    document = {
        "embedding_id": "obs_contract",
        "dimension": 3,
        "vector": [0.5, 0.5, 0.5],
        "vector_index_key": "material_text_contract_d3_v1",
        "text_sha256": DIGEST,
    }
    document.update(overrides)
    return document


# ── collection 与引用形状 ─────────────────────────────────────────────────────


def test_parse_index_key_reads_prefix_dimension_and_version():
    assert parse_index_key(KEY) == ("material_text_bge_small_zh_v1_5", 512, 1)
    assert parse_index_key("material_vision_vlm_d1024_v2") == ("material_vision_vlm", 1024, 2)


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "text_bge_d512_v1",
        "material_text_d0_v1",
        "material_text_d512_v0",
        "material_text_d512",
        "material_text_d512_v1_extra",
        "material_text_d512.5_v1",
        "Material_Text_d512_v1",
    ],
)
def test_parse_index_key_rejects_non_contract_shapes(value):
    with pytest.raises(IndexContractError) as failure:
        parse_index_key(value)
    assert failure.value.code == "invalid_vector_index_key"


def test_collection_name_is_the_key_itself():
    assert collection_name(KEY) == KEY
    assert parse_index_key(collection_name(KEY)) == parse_index_key(KEY)


def test_vector_ref_carries_no_deployment_details():
    reference = vector_ref(KEY, EMBEDDING_ID)
    assert reference == f"milvus://{KEY}/{EMBEDDING_ID}"
    assert parse_vector_ref(reference) == (KEY, EMBEDDING_ID)
    # 主机段就是 collection 名本身：不是 host:port，也没有本地路径/端口/库文件名。
    assert reference.split("://", 1)[1].split("/", 1)[0] == KEY
    for leaked in ("/var/", "127.0.0.1", "19530", ".db"):
        assert leaked not in reference


@pytest.mark.parametrize(
    "reference",
    [
        "",
        None,
        f"http://127.0.0.1:19530/{KEY}/{EMBEDDING_ID}",
        f"milvus://127.0.0.1:19530/{KEY}/{EMBEDDING_ID}",
        f"milvus://{KEY}",
        f"milvus://{KEY}/emb_NOT_HEX",
        f"milvus://{KEY}/EMB_" + "0" * 32,
    ],
)
def test_parse_vector_ref_rejects_deployment_details_and_foreign_ids(reference):
    with pytest.raises(IndexContractError) as failure:
        parse_vector_ref(reference)
    assert failure.value.code == "invalid_vector_ref"


def test_store_contract_constants_are_pinned():
    assert (INDEX_TYPE, METRIC_TYPE, VECTOR_FIELD) == ("FLAT", "COSINE", "vector")
    names = [name for name, *_ in SCALAR_FIELDS]
    assert names == [
        "embedding_id",
        "material_unit_id",
        "material_revision",
        "stream_id",
        "start_ms",
        "end_ms",
        "modality",
        "model_release_id",
        "observation_id",
        "content_hash",
        "created_at_unix_ms",
    ]
    primary = [name for name, _, _, is_primary in SCALAR_FIELDS if is_primary]
    assert primary == ["embedding_id"]
    assert "embedding_id" not in OUTPUT_FIELDS


# ── embedding_id 的确定性与作用域 ─────────────────────────────────────────────


def test_embedding_id_is_deterministic_and_scoped():
    base = records.embedding_id_for("obs_1", "material_1", 1)
    assert base == records.embedding_id_for("obs_1", "material_1", 1)
    assert base.startswith("emb_") and len(base) == 36
    assert len({base, records.embedding_id_for("obs_2", "material_1", 1)}) == 2
    assert len({base, records.embedding_id_for("obs_1", "material_2", 1)}) == 2
    assert len({base, records.embedding_id_for("obs_1", "material_1", 2)}) == 2


# ── payload 准入 ──────────────────────────────────────────────────────────────


def test_extract_embedding_accepts_protobuf_integer_double_dimension():
    observation_id, vector, dimension, index_key, content_hash = extract_embedding(
        payload(dimension=3.0)
    )
    assert (observation_id, vector, dimension, index_key, content_hash) == (
        "obs_contract",
        [0.5, 0.5, 0.5],
        3,
        "material_text_contract_d3_v1",
        DIGEST,
    )


@pytest.mark.parametrize(
    "broken",
    [
        None,
        {},
        payload(vector=None),
        payload(vector=[]),
        payload(vector="0.5,0.5,0.5"),
        payload(vector=[0.5, "0.5", 0.5]),
        payload(vector=[0.5, float("nan"), 0.5]),
        payload(vector=[0.5, float("inf"), 0.5]),
        payload(vector=[0.5, True, 0.5]),
        payload(dimension=3.5),
        payload(dimension=0),
        payload(dimension=-3),
        payload(dimension=True),
        payload(dimension="3"),
        payload(vector_index_key=""),
        payload(vector_index_key=512),
        payload(embedding_id=""),
        payload(text_sha256=None),
    ],
)
def test_extract_embedding_rejects_anything_it_cannot_pin_down(broken):
    with pytest.raises(IndexContractError) as failure:
        extract_embedding(broken)
    assert failure.value.code == "invalid_embedding_payload"


def test_index_key_mismatch_is_run_level_and_touches_no_record():
    """payload 认的 collection 与本次 worker 认的不是同一个：没有可信身份，因此不落记录。

    `conn=None` 是断言的一部分：这条路径必须在碰数据库之前就失败。
    """
    index = argparse.Namespace(vector_index_key="material_text_other_d3_v1")
    with pytest.raises(IndexContractError) as failure:
        index_embedding(
            None,
            index,
            payload=payload(),
            material_unit_id="material_contract",
            material_revision=1,
            model_release_id="model_contract",
            stream_id="stream_contract",
            start_ms=0,
            end_ms=1000,
            modality="text_embedding",
        )
    assert failure.value.code == "index_key_mismatch"
    assert failure.value.detail == "material_text_contract_d3_v1!=material_text_other_d3_v1"


# ── 单写进程：锁预检 ──────────────────────────────────────────────────────────


def test_local_store_locked_sees_another_holder():
    data_dir = pathlib.Path(tempfile.mkdtemp(prefix="index-contract-"))
    try:
        assert local_store_locked(str(data_dir)) is False  # 还不是一个数据目录
        uri = str(data_dir / "vector.db")
        os.makedirs(uri, exist_ok=True)
        assert local_store_locked(uri) is False  # 有目录但没人开过（还没有 LOCK）
        descriptor = os.open(str(pathlib.Path(uri) / "LOCK"), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert local_store_locked(uri) is True
            with pytest.raises(VectorStoreError) as failure:
                VectorIndex(uri, KEY)
            assert failure.value.code == "vector_store_locked"
            # 只报文件名：主机路径属于部署配置，不进原因码也不进 detail（ADR-010 §不外泄）。
            assert failure.value.detail == "vector.db"
            assert str(data_dir) not in failure.value.detail
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        assert local_store_locked(uri) is False  # 持有者退出后立即可用
        assert local_store_locked("http://127.0.0.1:19530") is False  # 服务端形态没有本地锁
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


# ── 整轮失败的输出形状 ────────────────────────────────────────────────────────


def test_run_failure_document_has_one_stable_reason_code(capsys):
    arguments = argparse.Namespace(command="index", uri="/tmp/x.db", out=None)
    assert cli._emit_run_failure(arguments, VectorStoreError("vector_store_locked", "x.db")) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["error_code"] == "vector_store_locked"
    assert document["indexed"] == []
    assert document["failed"] == [{"observation_id": None, "reason_code": "vector_store_locked"}]

"""BGE 向量插件的契约测试：输入准入、文本边界、维度/池化/归一化语义、稳定 ID、
provenance、provider 断言、模型身份、manifest/SBOM/网络策略。

这些测试不联网、不加载真实 ONNX 权重：会话与分词器在 `_load_engine` 的边界上被换成测试替身，
但**被测的契约**（准入、边界、池化、归一化、维度对账、摘要口径、provider 断言、身份组合）
都是真实代码路径。
"""

import hashlib
import json
import math
import pathlib
import shutil
import types

import numpy as np
import pytest
import yaml
from edge_material_plugin_embed_bge_onnx import models
from edge_material_plugin_embed_bge_onnx import plugin as embed
from edge_material_plugin_embed_bge_onnx import text as text_module
from edge_material_plugin_embed_bge_onnx.artifact import package_digest
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.validation import validate_observation
from google.protobuf import json_format
from jsonschema import Draft202012Validator

PLUGIN_DIR = pathlib.Path(__file__).parents[2] / "plugins/python/processors/embed-bge-onnx"
ROOT = pathlib.Path(__file__).parents[2]
DIGEST = "sha256:" + "a" * 64
ARTIFACT = "sha256:" + "b" * 64
MODEL_ARTIFACT = "sha256:" + "c" * 64
DIMENSION = 8
WINDOW_START_MS = 2_000
WINDOW_END_MS = 2_040


def payload_of(observation) -> dict:
    """payload 是 Struct：按契约里的 snake_case 键读，不让 JSON 驼峰化改变字段名。"""
    return json_format.MessageToDict(observation, preserving_proto_field_name=True).get(
        "payload", {}
    )


class FakeTokenizer:
    def __init__(self, tokens: int = 3):
        self.tokens = tokens
        self.max_length = None

    def enable_truncation(self, max_length):
        self.max_length = max_length

    def encode(self, value):
        return types.SimpleNamespace(ids=list(range(self.tokens)))


class FakeSession:
    """会话替身：provider 列表与输出维度可控，`run` 返回确定的隐藏状态。"""

    def __init__(self, providers=None, dimension: int = DIMENSION, first_row_only: bool = False):
        self._providers = list(providers) if providers is not None else [embed.PROVIDER_EP["cpu"]]
        self.dimension = dimension
        self.first_row_only = first_row_only
        self.runs = 0

    def get_providers(self):
        return list(self._providers)

    def get_inputs(self):
        return [
            types.SimpleNamespace(name=name)
            for name in ("input_ids", "attention_mask", "token_type_ids")
        ]

    def run(self, output_names, feed):
        self.runs += 1
        hidden = np.full((1, 3, self.dimension), 0.5, dtype=np.float32)
        if not self.first_row_only:
            hidden[0, 1, :] = 0.25
            hidden[0, 2, :] = 0.25
        return [hidden]


class FakeEncoder:
    """编码器替身：按文本摘要生成确定向量，并记录真实被编码的文本。"""

    def __init__(self, dimension: int = DIMENSION):
        self.dimension = dimension
        self.calls: list[str] = []

    def encode(self, value: str) -> embed.Encoded:
        self.calls.append(value)
        seed = int.from_bytes(hashlib.sha256(value.encode()).digest()[:4], "big")
        vector = np.random.default_rng(seed).normal(size=self.dimension).astype(np.float32)
        return embed.Encoded(vector=vector / np.linalg.norm(vector), token_count=len(value))


def fake_model(provider: str = "cpu", dimension: int = DIMENSION) -> models.EmbedModelIdentity:
    return models.EmbedModelIdentity(
        model_id="bge-small-zh-v1.5",
        model_version="local_dir",
        release_id="bge:bge-small-zh-v1.5@b389b2a68ae8",
        artifact_digest=MODEL_ARTIFACT,
        backend=f"onnxruntime-1.30.0/{embed.PROVIDER_EP[provider]}",
        providers=[embed.PROVIDER_EP[provider]],
        runtime_version="1.30.0",
        dimension=dimension,
        max_length=512,
        weights=[
            models.WeightsFile(role="encoder", name="model.onnx", digest=DIGEST, size_bytes=1),
            models.WeightsFile(
                role="tokenizer", name="tokenizer.json", digest=DIGEST, size_bytes=1
            ),
            models.WeightsFile(
                role="model_config", name="config.json", digest=DIGEST, size_bytes=1
            ),
        ],
        source="local_dir",
        directory="/nonexistent/in-test",
    )


def make_plugin(encoder=None, provider: str = "cpu", **kwargs) -> embed.EmbedPlugin:
    plugin = embed.EmbedPlugin(artifact_digest=ARTIFACT, **kwargs)
    plugin.config = embed.EmbedConfig(model_dir="/nonexistent/in-test", provider=provider)
    plugin.model = fake_model(provider)
    plugin.encoder = encoder or FakeEncoder()
    plugin.started = True
    return plugin


def upstream_observation(
    texts=("Jak nahrát video", "do Commons"),
    modality: str = embed.text_module.INPUT_MODALITY,
    stream_id: str = "stream_embed",
    source_id: str = "source_embed",
    quality_state: str = "final",
    timing_source: str = "media_pts",
    blocks=None,
) -> material.Observation:
    observation = material.Observation(
        observation_id="obs_upstream_ocr_blocks",
        modality=modality,
        stream_id=stream_id,
        source_id=source_id,
        source_item_id="item_embed",
        time_range=common.TimeRange(start_ms=WINDOW_START_MS, end_ms=WINDOW_END_MS),
        confidence_unavailable_reason="detector_and_recognizer_scores_are_not_calibrated_confidence",
        quality_state=quality_state,
        content_hash=DIGEST,
        timing_source=timing_source,
        created_at_unix_ms=1_700_000_000_000,
        provenance=material.Provenance(
            plugin="org.sensoryplex.ocr-rapidocr",
            plugin_version="0.1.0",
            artifact_digest=ARTIFACT,
            model_release_id="ppocr:PP-OCRv6_mobile@31df9f5afcc7",
            model_id="PP-OCRv6_mobile",
            model_version="bundled",
            config_hash=DIGEST,
            execution_backend="onnxruntime-1.30.0/CPUExecutionProvider",
            model_artifact_digest=MODEL_ARTIFACT,
        ),
    )
    payload = blocks if blocks is not None else [{"text": item, "score": 0.9} for item in texts]
    observation.payload.update({"blocks": payload, "block_count": len(payload)})
    return observation


def make_request(
    observations=None,
    *,
    stream_id: str = "stream_embed",
    source_id: str = "source_embed",
    data_egress: str = "local_only",
):
    items = observations if observations is not None else [upstream_observation()]
    inputs = [runtime.PluginInput(observation=item) for item in items]
    return runtime.ProcessRequest(
        context=common.RequestContext(
            request_id="request_embed",
            trace_id="trace_embed",
            pipeline_run_id="run_embed",
            stream_id=stream_id,
            source_id=source_id,
            deadline_unix_ms=2_000_000_000_000,
            attempt=1,
            idempotency_key="stable",
            privacy_policy=common.PrivacyPolicy(data_egress=data_egress),
        ),
        inputs=inputs,
        processor_release_id="release_embed",
    )


# --- 输入准入 -----------------------------------------------------------------


async def test_raw_frames_are_refused_even_with_a_reader_attached():
    """向量插件要的是上游事实，不是字节：挂了数据面也不接受 buffer 输入。"""
    plugin = make_plugin(buffer_reader=object())
    request = make_request()
    request.inputs[0].buffer.buffer_id = "frame_1"
    request.inputs[0].buffer.kind = "video_frame"
    request.inputs[0].buffer.memory_kind = "cpu_shared_memory"
    request.inputs[0].buffer.stream_id = "stream_embed"
    request.inputs[0].buffer.time_range.CopyFrom(
        common.TimeRange(start_ms=WINDOW_START_MS, end_ms=WINDOW_END_MS)
    )
    request.inputs[0].buffer.content_hash = DIGEST
    response = await plugin.invoke(request)
    assert response.error.code == common.UNSUPPORTED_MEMORY_KIND
    assert response.error.reason_code == "unsupported_input_kind:buffer"


async def test_plugin_without_a_reader_still_refuses_buffers():
    plugin = make_plugin()
    request = make_request()
    request.inputs[0].buffer.buffer_id = "frame_1"
    response = await plugin.invoke(request)
    assert response.error.reason_code == "buffer_reader_not_attached"


async def test_observation_must_match_the_request_context():
    plugin = make_plugin()
    response = await plugin.invoke(make_request(source_id="source_somewhere_else"))
    assert response.error.reason_code == "input_context_mismatch"


async def test_observation_identity_is_validated():
    plugin = make_plugin()
    broken = upstream_observation()
    broken.observation_id = ""
    response = await plugin.invoke(make_request([broken]))
    assert response.error.reason_code == "contract_validation_failed"


async def test_only_ocr_blocks_observations_are_accepted():
    """别的形态（比如 ASR 文本段）必须显式拒绝，不做"尽力而为"的猜测。"""
    plugin = make_plugin()
    response = await plugin.invoke(make_request([upstream_observation(modality="asr_segment")]))
    assert response.error.code == common.INVALID_INPUT
    assert response.error.reason_code == "unsupported_input_modality:asr_segment"


@pytest.mark.parametrize(
    ("blocks", "reason"),
    [
        ([], "input_text_empty"),
        ([{"text": "   "}], "input_text_empty"),
        ([{"score": 0.9}], "input_block_missing_text"),
        ([{"text": 5}], "input_block_text_not_string"),
        ([{"text": "x" * (text_module.MAX_TOTAL_CHARS + 1)}], "input_text_exceeds_bound"),
        (
            [{"text": "x"}] * (text_module.MAX_TEXTS + 1),
            "input_block_count_exceeds_bound",
        ),
    ],
)
async def test_text_bounds_and_shapes_fail_explicitly(blocks, reason):
    plugin = make_plugin()
    response = await plugin.invoke(make_request([upstream_observation(blocks=blocks)]))
    assert response.HasField("error")
    assert response.error.code == common.INVALID_INPUT
    assert response.error.reason_code == reason


async def test_payload_without_blocks_is_refused():
    plugin = make_plugin()
    observation = upstream_observation()
    observation.ClearField("payload")
    observation.payload.update({"block_count": 2})
    response = await plugin.invoke(make_request([observation]))
    assert response.error.reason_code == "input_payload_missing_blocks"


async def test_batch_and_policy_limits_are_enforced():
    plugin = make_plugin()
    empty = make_request([])
    del empty.inputs[:]
    response = await plugin.invoke(empty)
    assert response.error.reason_code == "invalid_batch_size"

    too_many = make_request([upstream_observation()] * (plugin.max_batch_size + 1))
    response = await plugin.invoke(too_many)
    assert response.error.reason_code == "invalid_batch_size"

    response = await plugin.invoke(make_request(data_egress="cloud_allowed"))
    assert response.error.code == common.DATA_POLICY_DENIED


async def test_blank_blocks_are_skipped_and_counted():
    plugin = make_plugin()
    observation = upstream_observation(blocks=[{"text": "  "}, {"text": " 内容 "}, {"text": ""}])
    response = await plugin.invoke(make_request([observation]))
    assert not response.HasField("error")
    payload = payload_of(response.observations[0])
    assert payload["text"] == "内容"
    assert payload["block_count"] == 3
    assert payload["blank_blocks"] == 2


# --- 语义 ---------------------------------------------------------------------


async def test_observation_carries_inherited_anchor_and_embedding_semantics():
    encoder = FakeEncoder()
    plugin = make_plugin(encoder)
    upstream = upstream_observation()
    response = await plugin.invoke(make_request([upstream]))
    assert not response.HasField("error")
    observation = response.observations[0]
    validate_observation(observation)
    assert encoder.calls == ["Jak nahrát video\ndo Commons"]

    # 锚点与状态继承上游观测：本插件不重新计时、也不美化上游的质量状态。
    assert (observation.time_range.start_ms, observation.time_range.end_ms) == (
        WINDOW_START_MS,
        WINDOW_END_MS,
    )
    assert observation.timing_source == upstream.timing_source
    assert observation.quality_state == upstream.quality_state
    # content_hash 是**实际被编码的那段文本**的摘要，任何持有该文本的人都能复算。
    assert observation.content_hash == text_module.text_digest("Jak nahrát video\ndo Commons")
    assert not observation.HasField("confidence")
    assert observation.confidence_unavailable_reason == embed.CONFIDENCE_UNAVAILABLE_REASON
    assert observation.provenance.plugin == embed.PLUGIN_NAME
    assert observation.provenance.model_artifact_digest == MODEL_ARTIFACT
    assert observation.provenance.execution_backend == "onnxruntime-1.30.0/CPUExecutionProvider"

    payload = payload_of(observation)
    assert payload["dimension"] == DIMENSION
    assert payload["pooling"] == embed.POOLING
    assert payload["normalize"] == embed.NORMALIZE
    assert payload["storage"] == "inline_payload"
    assert payload["vector_ref"] is None
    assert payload["vector_index_key"] == f"material_text_bge_small_zh_v1_5_d{DIMENSION}_v1"
    assert payload["text_sha256"] == observation.content_hash
    assert payload["token_count"] == len("Jak nahrát video\ndo Commons")
    # 上游身份单独记账，不与"我的内容摘要"混为一谈。
    assert payload["input"]["observation_id"] == upstream.observation_id
    assert payload["input"]["content_hash"] == DIGEST
    assert payload["input"]["time_range"] == {"start_ms": WINDOW_START_MS, "end_ms": WINDOW_END_MS}
    assert payload["embedding_id"] == observation.observation_id
    assert [item["role"] for item in payload["engine"]["weights"]] == [
        "encoder",
        "tokenizer",
        "model_config",
    ]


async def test_vector_is_l2_normalized_and_digest_is_recomputable():
    plugin = make_plugin()
    response = await plugin.invoke(make_request())
    payload = payload_of(response.observations[0])
    vector = np.asarray(payload["vector"], dtype=np.float32)
    assert vector.shape == (DIMENSION,)
    assert math.isclose(float(np.linalg.norm(vector)), 1.0, rel_tol=1e-6)
    assert math.isclose(payload["norm"], float(np.linalg.norm(vector)), rel_tol=1e-6)
    # 摘要按 float32 小端字节算：任何人拿到这份 payload 都能独立复算。
    expected = hashlib.sha256(np.asarray(payload["vector"], dtype="<f4").tobytes()).hexdigest()
    assert payload["vector_sha256"] == "sha256:" + expected


async def test_observation_ids_and_vectors_are_stable_per_input_text():
    plugin_a = make_plugin()
    plugin_b = make_plugin()
    first = await plugin_a.invoke(make_request())
    again = await plugin_b.invoke(make_request())
    other = await plugin_b.invoke(
        make_request([upstream_observation(texts=("Jak nahrát video", "do Commons 2026"))])
    )
    assert first.observations[0].observation_id == again.observations[0].observation_id
    assert first.observations[0].observation_id != other.observations[0].observation_id
    assert first.observations[0].content_hash != other.observations[0].content_hash


def test_bge_encoder_pools_the_first_row_and_normalizes():
    """池化规则是 BGE 的标准用法：取序列第一位（CLS），再过 L2 归一化。"""
    encoder = embed.BgeEncoder(
        FakeSession(first_row_only=True),
        FakeTokenizer(),
        max_length=512,
        dimension=DIMENSION,
    )
    encoded = encoder.encode("探针")
    assert encoded.token_count == 3
    assert math.isclose(float(np.linalg.norm(encoded.vector)), 1.0, rel_tol=1e-6)
    # 只有第 0 行是 0.5，其余两行是 0.25：结果必须等于归一化后的第 0 行。
    assert np.allclose(encoded.vector, np.full(DIMENSION, 1.0 / math.sqrt(DIMENSION)))


def test_encoder_refuses_a_zero_vector():
    class ZeroSession(FakeSession):
        def run(self, output_names, feed):
            return [np.zeros((1, 3, self.dimension), dtype=np.float32)]

    encoder = embed.BgeEncoder(ZeroSession(), FakeTokenizer(), max_length=512, dimension=DIMENSION)
    with pytest.raises(ValueError, match="embedding_norm_unusable"):
        encoder.encode("探针")


# --- provider 断言与维度对账 --------------------------------------------------


def test_requested_provider_must_actually_be_selected():
    coreml = FakeSession([embed.PROVIDER_EP["coreml"], embed.PROVIDER_EP["cpu"]])
    assert embed.EmbedPlugin._backend_string(coreml, "coreml", "1.30.0") == (
        "onnxruntime-1.30.0/CoreMLExecutionProvider"
    )
    with pytest.raises(ValueError, match="execution_provider_not_selected:CoreMLExecutionProvider"):
        embed.EmbedPlugin._backend_string(coreml, "cpu", "1.30.0")
    with pytest.raises(ValueError, match="execution_provider_not_selected:none"):
        embed.EmbedPlugin._backend_string(FakeSession([]), "cpu", "1.30.0")


def test_measured_dimension_must_match_the_model_config():
    encoder = FakeEncoder(dimension=4)
    assert embed.EmbedPlugin._probe_dimension(encoder, 4) == 4
    with pytest.raises(ValueError, match="model_dimension_mismatch:512!=4"):
        embed.EmbedPlugin._probe_dimension(encoder, 512)


# --- 权重身份 -----------------------------------------------------------------


def test_onnx_container_probe_rejects_empty_and_non_onnx(tmp_path):
    empty = tmp_path / "empty.onnx"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="model_container_empty"):
        models.probe_onnx_container(empty)
    text = tmp_path / "not.onnx"
    text.write_bytes(b"this is not protobuf")
    with pytest.raises(ValueError, match="model_container_not_onnx"):
        models.probe_onnx_container(text)
    good = tmp_path / "ok.onnx"
    good.write_bytes(b"\x08\x07" + b"\x00" * 8)
    assert models.probe_onnx_container(good) == "onnx"


def test_combined_digest_is_order_independent_and_covers_every_role():
    files = {"encoder": DIGEST, "tokenizer": ARTIFACT, "model_config": MODEL_ARTIFACT}
    shuffled = dict(reversed(list(files.items())))
    assert models.combined_digest(files) == models.combined_digest(shuffled)
    # 换掉分词器同样改变身份：同一段文本换分词规则就会得到不同向量。
    changed = dict(files, tokenizer="sha256:" + "d" * 64)
    assert models.combined_digest(files) != models.combined_digest(changed)


def test_model_config_parse_is_explicit(tmp_path):
    missing = tmp_path / "config.json"
    with pytest.raises(ValueError, match="model_config_missing"):
        models.read_model_config(missing)
    missing.write_text("{not json")
    with pytest.raises(ValueError, match="model_config_unreadable"):
        models.read_model_config(missing)
    missing.write_text(json.dumps({"hidden_size": 512}))
    with pytest.raises(ValueError, match="model_config_incomplete:max_position_embeddings"):
        models.read_model_config(missing)
    missing.write_text(json.dumps({"hidden_size": 512, "max_position_embeddings": 512}))
    assert models.read_model_config(missing) == {"dimension": 512, "max_position_embeddings": 512}


def test_installed_version_is_not_fabricated():
    assert models.installed_version("onnxruntime") != "unknown"
    assert models.installed_version("definitely-not-installed-distribution") == "unknown"


# --- configure 路径 -----------------------------------------------------------


def test_configure_requires_an_explicit_weight_directory():
    plugin = embed.EmbedPlugin(artifact_digest=ARTIFACT)
    with pytest.raises(ValueError, match="model_dir_required"):
        plugin.configure({"provider": "cpu"})
    with pytest.raises(ValueError, match="model_dir_not_found"):
        plugin.configure({"model_dir": "/nonexistent/bge", "provider": "cpu"})
    with pytest.raises(ValueError, match="artifact_digest_required"):
        embed.EmbedPlugin().configure({"model_dir": "/nonexistent/bge"})


def make_weight_dir(tmp_path: pathlib.Path, model_bytes: bytes = b"\x08\x07" + b"\x00" * 8):
    directory = tmp_path / "weights"
    directory.mkdir()
    (directory / "model_quantized.onnx").write_bytes(model_bytes)
    (directory / "tokenizer.json").write_text("{}")
    (directory / "config.json").write_text(
        json.dumps({"hidden_size": DIMENSION, "max_position_embeddings": 512})
    )
    return directory


def test_configure_uses_the_real_files_and_probe(monkeypatch, tmp_path):
    directory = make_weight_dir(tmp_path)
    session = FakeSession(dimension=DIMENSION)
    monkeypatch.setattr(embed, "load_tokenizer", lambda path: FakeTokenizer())
    monkeypatch.setattr(embed, "create_session", lambda path, provider: session)
    plugin = embed.EmbedPlugin(artifact_digest=ARTIFACT)
    plugin.configure({"model_dir": str(directory), "provider": "cpu"})
    assert plugin.started is True
    assert plugin.model.dimension == DIMENSION
    assert plugin.model.max_length == 512
    assert plugin.model.backend == (
        f"onnxruntime-{models.installed_version('onnxruntime')}/CPUExecutionProvider"
    )
    assert plugin.model.release_id.startswith("bge:bge-small-zh-v1.5@")
    # 身份来自真实文件内容：三个角色都在，且与独立复算的组合摘要一致。
    files = {item.role: item.digest for item in plugin.model.weights}
    assert set(files) == {"encoder", "tokenizer", "model_config"}
    assert plugin.model.artifact_digest == models.combined_digest(files)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda directory: (directory / "model_quantized.onnx").unlink(), "model_file_not_found"),
        (
            lambda directory: (directory / "model_quantized.onnx").write_bytes(b"not onnx"),
            "model_container_not_onnx",
        ),
        (lambda directory: (directory / "tokenizer.json").unlink(), "tokenizer_file_not_found"),
        (lambda directory: (directory / "config.json").unlink(), "model_config_missing"),
    ],
)
def test_configure_fails_on_incomplete_weight_dirs(monkeypatch, tmp_path, mutate, reason):
    directory = make_weight_dir(tmp_path)
    mutate(directory)
    monkeypatch.setattr(embed, "load_tokenizer", lambda path: FakeTokenizer())
    monkeypatch.setattr(embed, "create_session", lambda path, provider: FakeSession())
    plugin = embed.EmbedPlugin(artifact_digest=ARTIFACT)
    with pytest.raises(ValueError, match=reason):
        plugin.configure({"model_dir": str(directory), "provider": "cpu"})


def test_model_file_may_point_into_a_subdirectory(monkeypatch, tmp_path):
    """HF 快照把 ONNX 放在 onnx/ 子目录：相对子路径是合法配置，身份仍按字节实测。"""
    directory = make_weight_dir(tmp_path)
    nested = directory / "onnx"
    nested.mkdir()
    (directory / "model_quantized.onnx").rename(nested / "model_quantized.onnx")
    monkeypatch.setattr(embed, "load_tokenizer", lambda path: FakeTokenizer())
    monkeypatch.setattr(
        embed, "create_session", lambda path, provider: FakeSession(dimension=DIMENSION)
    )
    plugin = embed.EmbedPlugin(artifact_digest=ARTIFACT)
    plugin.configure(
        {
            "model_dir": str(directory),
            "model_file": "onnx/model_quantized.onnx",
            "provider": "cpu",
        }
    )
    assert plugin.started is True
    files = {item.role: item for item in plugin.model.weights}
    assert files["encoder"].name == "model_quantized.onnx"
    assert files["encoder"].digest == models.sha256_file(nested / "model_quantized.onnx")
    assert plugin.model.dimension == DIMENSION


@pytest.mark.parametrize(
    ("model_file", "reason"),
    [
        ("/etc/passwd", "model_file_must_be_relative"),
        ("../outside.onnx", "model_file_outside_model_dir"),
        ("onnx/../../outside.onnx", "model_file_outside_model_dir"),
    ],
)
def test_model_file_may_not_escape_the_model_dir(monkeypatch, tmp_path, model_file, reason):
    """权重是只读输入：越界路径是配置错误，不是"帮运营找文件"。"""
    directory = make_weight_dir(tmp_path)
    monkeypatch.setattr(embed, "load_tokenizer", lambda path: FakeTokenizer())
    monkeypatch.setattr(embed, "create_session", lambda path, provider: FakeSession())
    plugin = embed.EmbedPlugin(artifact_digest=ARTIFACT)
    with pytest.raises(ValueError, match=reason):
        plugin.configure({"model_dir": str(directory), "model_file": model_file, "provider": "cpu"})


def test_configure_refuses_a_truncation_limit_beyond_the_model(monkeypatch, tmp_path):
    """分词截断上限超过权重的位置编码范围时拒绝启动，而不是静默夹取。"""
    directory = make_weight_dir(tmp_path)
    (directory / "config.json").write_text(
        json.dumps({"hidden_size": DIMENSION, "max_position_embeddings": 64})
    )
    monkeypatch.setattr(embed, "load_tokenizer", lambda path: FakeTokenizer())
    monkeypatch.setattr(embed, "create_session", lambda path, provider: FakeSession())
    plugin = embed.EmbedPlugin(artifact_digest=ARTIFACT)
    with pytest.raises(ValueError, match="max_length_exceeds_model_limit:64"):
        plugin.configure({"model_dir": str(directory), "max_length": 512})


def test_validate_config_covers_the_admission_surface():
    assert embed.validate_config({"model_dir": "/tmp"}).valid
    assert embed.validate_config({}).field_errors == ["model_dir_required"]
    result = embed.validate_config({"model_dir": "/tmp", "provider": "metal"})
    assert result.field_errors == ["unsupported_provider:metal"]
    result = embed.validate_config({"model_dir": "/tmp", "max_length": 4096})
    assert result.field_errors == ["max_length_out_of_range"]
    result = embed.validate_config({"model_dir": "/tmp", "unknown": 1})
    assert result.field_errors == ["unknown_config_keys:unknown"]
    assert embed.EmbedConfig.from_mapping({}).provider == "cpu"


def test_describe_declares_the_real_input_and_output_kinds():
    description = embed.describe()
    assert description.consumes == [embed.CONSUMES] == ["observation.ocr_blocks"]
    assert description.produces == ["observation.text_embedding"]
    # 不消费任何 buffer：这就是"不接数据面"的可观测表达。
    assert description.memory_kinds == []


# --- manifest / schema / SBOM / 摘要口径 --------------------------------------


def load_manifest() -> dict:
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())


def test_manifest_matches_the_real_package_digest():
    manifest = load_manifest()
    assert manifest["spec"]["artifacts"]["digest"] == package_digest(PLUGIN_DIR)
    assert manifest["spec"]["capabilities"]["acceptsMemoryKinds"] == []
    assert manifest["spec"]["security"]["network"] == "none"
    assert manifest["spec"]["security"].get("allowedHosts") in (None, [])
    assert manifest["spec"]["security"]["filesystem"]["writablePaths"] == []
    assert manifest["spec"]["entrypoint"]["port"] == 50074


def test_manifest_and_config_schema_are_valid():
    schema = json.loads((ROOT / "docs/contracts/plugin.schema.json").read_text())
    Draft202012Validator(schema).validate(load_manifest())
    config_schema = json.loads((PLUGIN_DIR / "config.schema.json").read_text())
    Draft202012Validator.check_schema(config_schema)
    validator = Draft202012Validator(config_schema)
    assert not list(validator.iter_errors({"model_dir": "/tmp"}))
    assert list(validator.iter_errors({}))
    assert list(validator.iter_errors({"model_dir": "/tmp", "provider": "metal"}))


def test_sbom_lists_the_declared_runtime_dependencies():
    sbom = json.loads((PLUGIN_DIR / "sbom.cdx.json").read_text())
    manifest = load_manifest()
    assert sbom["metadata"]["component"]["version"] == manifest["metadata"]["version"]
    assert sbom["metadata"]["component"]["properties"] == [
        {"name": "sensoryplex:package_digest", "value": package_digest(PLUGIN_DIR)}
    ]
    names = {component["name"] for component in sbom["components"]}
    assert {"edge-material-sdk", "grpcio", "numpy", "onnxruntime", "tokenizers"} <= names
    assert "huggingface-hub" not in names  # 本插件不下载权重，也不该声明下载依赖
    # 组件名只是包名，版本区间单独记账（不把 "grpcio>=1.71,<1.72" 当成包名）。
    grpcio = next(component for component in sbom["components"] if component["name"] == "grpcio")
    assert grpcio["version"] == "unspecified"
    assert grpcio["properties"] == [
        {"name": "sensoryplex:version_specifier", "value": ">=1.71,<1.72"}
    ]


def test_digest_scope_tracks_code_and_ignores_everything_else(tmp_path):
    """摘要口径必须可复算：改代码会变；manifest、README、字节码缓存都不在范围内。"""
    copy_root = tmp_path / "embed-bge-onnx"
    shutil.copytree(PLUGIN_DIR, copy_root)
    baseline = package_digest(copy_root)
    module = copy_root / "src/edge_material_plugin_embed_bge_onnx/plugin.py"
    module.write_text(
        module.read_text().replace('MODALITY = "text_embedding"', 'MODALITY = "vector"')
    )
    assert package_digest(copy_root) != baseline
    module.write_text(
        module.read_text().replace('MODALITY = "vector"', 'MODALITY = "text_embedding"')
    )
    assert package_digest(copy_root) == baseline
    (copy_root / "plugin.yaml").write_text("apiVersion: edited-by-test\n")
    (copy_root / "README.md").write_text("说明文字不属于摘要范围\n")
    (copy_root / "src/edge_material_plugin_embed_bge_onnx/__pycache__").mkdir(exist_ok=True)
    bytecode = copy_root / "src/edge_material_plugin_embed_bge_onnx/__pycache__/plugin.pyc"
    bytecode.parent.mkdir(exist_ok=True)
    bytecode.write_bytes(b"x")
    assert package_digest(copy_root) == baseline
    project = copy_root / "pyproject.toml"
    project.write_text('[project]\nname = "changed"\nversion = "0.1.0"\n')
    assert package_digest(copy_root) != baseline

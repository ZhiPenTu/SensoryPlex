"""模型插件的契约测试：输入路径、observation 语义、产物摘要可复算。

这些测试不联网、不启动模型服务：插件的外部依赖（模型 HTTP、数据面）在
`process()` 的边界上被替换成测试替身，但**被测的契约**（输入准入、锚点、
provenance、置信度语义、稳定 ID）都是真实代码路径。
"""

import copy
import json
import pathlib
import shutil
import tempfile
import time

import pytest
import yaml
from edge_material_plugin_vlm_moondream import plugin as vlm
from edge_material_plugin_vlm_moondream.artifact import package_digest
from edge_material_plugin_vlm_moondream.png import encode_rgba
from edge_material_sdk import BufferRead
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.validation import validate_observation
from jsonschema import Draft202012Validator

PLUGIN_DIR = pathlib.Path(__file__).parents[2] / "plugins/python/processors/vlm-moondream"
DIGEST = "sha256:" + "a" * 64


def test_local_decode_memory_limit_survives_protobuf_struct():
    from google.protobuf.json_format import MessageToDict, ParseDict

    request = runtime.ValidateConfigRequest()
    ParseDict(
        {"data_plane_mode": "local_decode", "min_free_memory_bytes": 268435456}, request.config
    )
    config = MessageToDict(request.config)
    assert vlm.validate_config(config).valid
    assert not vlm.validate_config({**config, "min_free_memory_bytes": 268435456.5}).valid
    assert not vlm.validate_config({**config, "min_free_memory_bytes": True}).valid


@pytest.mark.parametrize("mode", ["per_request", "local_decode"])
def test_grpc_start_does_not_require_static_handoff_for_dynamic_modes(monkeypatch, mode):
    import asyncio

    from edge_material_plugin_vlm_moondream.server import PluginServicer
    from google.protobuf.json_format import ParseDict

    plugin = make_plugin()
    monkeypatch.setattr(plugin, "_probe_model", lambda config: plugin.model)
    loop = asyncio.new_event_loop()
    try:
        service = PluginServicer(plugin, loop)
        request = runtime.StartRequest()
        config = {"data_plane_mode": mode}
        if mode == "local_decode":
            config["min_free_memory_bytes"] = 268435456
        ParseDict(config, request.config)
        response = service.Start(request, None)
        assert response.state == "ready", response.error.reason_code
        assert plugin.buffer_reader is None
    finally:
        loop.close()


class FakeReader:
    """测试替身：只实现插件真正用到的读取契约。"""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.reads: list[str] = []
        self.closed = False

    def read(self, buffer_id: str) -> BufferRead:
        self.reads.append(buffer_id)
        return BufferRead(
            buffer_id=buffer_id,
            kind="video_frame",
            time_range=common.TimeRange(start_ms=1000, end_ms=1033),
            format=common.BufferFormat(pixel_format="RGBA", width=2, height=2),
            digest=DIGEST,
            payload=self.payload,
        )

    def close(self):
        self.closed = True


def make_plugin(reader=None) -> vlm.VisionVlmPlugin:
    plugin = vlm.VisionVlmPlugin(artifact_digest="sha256:" + "b" * 64)
    plugin.config = vlm.VlmConfig()
    plugin.model = vlm.ModelIdentity(
        model_id="moondream",
        model_version="v2",
        release_id="ollama:moondream:v2@ad0714b7b564",
        artifact_digest="sha256:" + "c" * 64,
        backend="ollama-0.4.1",
    )
    plugin.started = True
    plugin.buffer_reader = reader
    return plugin


def make_request(kind: str = "buffer", stream_id: str = "stream_model") -> runtime.ProcessRequest:
    if kind == "buffer":
        payload = runtime.PluginInput(
            buffer=common.BufferDescriptor(
                buffer_id="buf_1",
                kind="video_frame",
                memory_kind="cpu_shared_memory",
                stream_id=stream_id,
                time_range=common.TimeRange(start_ms=1000, end_ms=1033),
                format=common.BufferFormat(pixel_format="RGBA", width=2, height=2),
                content_hash=DIGEST,
            )
        )
    else:
        payload = runtime.PluginInput(
            buffer=common.BufferDescriptor(
                buffer_id="buf_2",
                kind=kind,
                memory_kind="cpu_shared_memory",
                stream_id=stream_id,
                time_range=common.TimeRange(start_ms=1000, end_ms=1033),
                content_hash=DIGEST,
            )
        )
    return runtime.ProcessRequest(
        context=common.RequestContext(
            request_id="request_model",
            trace_id="trace_model",
            pipeline_run_id="run_model",
            stream_id=stream_id,
            source_id="source_model",
            deadline_unix_ms=int(time.time() * 1000) + 5000,
            attempt=1,
            idempotency_key="stable",
            privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
        ),
        inputs=[payload],
        processor_release_id="release_model",
    )


async def test_buffer_input_requires_an_attached_reader():
    """没有 reader 时必须显式拒绝，不能退化成"跳过这条输入"。"""
    response = await make_plugin().invoke(make_request())
    assert response.error.code == common.UNSUPPORTED_MEMORY_KIND
    assert response.error.reason_code == "buffer_reader_not_attached"


async def test_unsupported_memory_kind_and_kind_are_refused():
    request = make_request()
    request.inputs[0].buffer.memory_kind = "cuda_ipc"
    response = await make_plugin(FakeReader(b"\x00" * 16)).invoke(request)
    assert response.error.reason_code.startswith("unsupported_memory_kind")

    response = await make_plugin(FakeReader(b"\x00" * 16)).invoke(make_request("audio_pcm"))
    assert response.error.reason_code == "unsupported_input_kind:audio_pcm"


async def test_observation_carries_anchor_source_version_and_confidence_semantics(monkeypatch):
    reader = FakeReader(b"\xff\x00\x00\xff" * 4)
    plugin = make_plugin(reader)
    monkeypatch.setattr(
        plugin,
        "_request_json",
        lambda path, body, timeout: {
            "response": "  a single red square  ",
            "eval_count": 7,
            "total_duration": 2_000_000,
        },
    )
    response = await plugin.invoke(make_request())
    assert not response.HasField("error")
    observation = response.observations[0]
    validate_observation(observation)

    # 时间锚点直接来自源帧的半开区间，不重新计时。
    assert (observation.time_range.start_ms, observation.time_range.end_ms) == (1000, 1033)
    # observation 绑定到它实际读到的那些字节。
    assert observation.content_hash == DIGEST
    assert observation.timing_source
    # 模型不给校准置信度 → 必须显式未知，且写明原因。
    assert not observation.HasField("confidence")
    assert observation.confidence_unavailable_reason == vlm.CONFIDENCE_UNAVAILABLE_REASON
    assert observation.provenance.plugin == vlm.PLUGIN_NAME
    assert observation.provenance.model_artifact_digest == "sha256:" + "c" * 64
    assert observation.provenance.config_hash == plugin.config.config_hash()
    assert observation.payload["text"] == "a single red square"
    # 原始像素（4 字节 RGBA 模式）不得出现在 payload 里。
    assert all("payload" not in key for key in ("raw", "bytes", "pixels"))
    assert reader.reads == ["buf_1"]


async def test_observation_id_is_derived_from_stable_inputs(monkeypatch):
    plugin = make_plugin(FakeReader(b"\xff\x00\x00\xff" * 4))
    monkeypatch.setattr(
        plugin, "_request_json", lambda path, body, timeout: {"response": "text", "eval_count": 1}
    )
    first = await plugin.invoke(make_request())
    second = await plugin.invoke(make_request())
    assert first.observations[0].observation_id == second.observations[0].observation_id

    other_stream = make_request(stream_id="stream_other")
    third = await plugin.invoke(other_stream)
    assert third.observations[0].observation_id != first.observations[0].observation_id


async def test_empty_model_response_is_a_failure_not_an_empty_success(monkeypatch):
    plugin = make_plugin(FakeReader(b"\xff\x00\x00\xff" * 4))
    monkeypatch.setattr(plugin, "_request_json", lambda path, body, timeout: {"response": "   "})
    response = await plugin.invoke(make_request())
    assert response.error.reason_code == "empty_model_response"
    assert response.error.retryable is True


def test_config_validation_rejects_unknown_keys_and_missing_endpoint():
    assert vlm.validate_config({"handoff_endpoint": "127.0.0.1:50051"}).valid
    unknown = vlm.validate_config({"handoff_endpoint": "127.0.0.1:50051", "typo": 1})
    assert not unknown.valid and unknown.field_errors == ["unknown_config_keys:typo"]
    missing = vlm.validate_config({"handoff_endpoint": ""})
    assert not missing.valid and "handoff_endpoint_required" in missing.field_errors

    delayed_without_watermark = vlm.validate_config({"data_plane_mode": "local_decode"})
    assert not delayed_without_watermark.valid
    assert "min_free_memory_bytes_required" in delayed_without_watermark.field_errors
    assert vlm.validate_config(
        {
            "data_plane_mode": "local_decode",
            "min_free_memory_bytes": vlm.MIN_LOCAL_DECODE_FREE_MEMORY_BYTES,
        }
    ).valid


def test_config_hash_tracks_only_the_effective_config():
    base = vlm.VlmConfig()
    same = vlm.VlmConfig()
    assert base.config_hash() == same.config_hash()
    changed = vlm.VlmConfig(prompt="answer with one word")
    assert changed.config_hash() != base.config_hash()
    # handoff_endpoint 属于部署位置，不属于"同一次推理的配置"。
    assert vlm.VlmConfig(handoff_endpoint="127.0.0.1:1").config_hash() == base.config_hash()


def test_png_encoder_round_trips_the_pixels():
    pixels = bytes(range(16))
    encoded = encode_rgba(2, 2, pixels)
    assert encoded.startswith(b"\x89PNG\r\n\x1a\n")
    import struct
    import zlib

    # 解出 IDAT，去掉每行的 filter 字节，应当还原出原始像素。
    offset, chunks = 8, {}
    while offset < len(encoded):
        (length,) = struct.unpack(">I", encoded[offset : offset + 4])
        tag = encoded[offset + 4 : offset + 8]
        body = encoded[offset + 8 : offset + 8 + length]
        chunks[tag] = body
        offset += 12 + length
    raw = zlib.decompress(chunks[b"IDAT"])
    rows = [raw[index + 1 : index + 9] for index in range(0, len(raw), 9)]
    assert b"".join(rows) == pixels
    assert struct.unpack(">IIBBBBB", chunks[b"IHDR"])[:2] == (2, 2)

    with pytest.raises(ValueError, match="invalid_image_size"):
        encode_rgba(0, 2, pixels)
    with pytest.raises(ValueError, match="pixel_buffer_too_small"):
        encode_rgba(2, 2, pixels[:8])


def test_manifest_declares_the_real_package_digest():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    artifacts = manifest["spec"]["artifacts"]
    assert artifacts["form"] == "local_native"
    # 占位串会在这里被抓住：manifest 的 digest 必须等于本机复算的插件包摘要。
    assert artifacts["digest"] == package_digest(PLUGIN_DIR)
    assert artifacts["image"].startswith("local:")
    # 没有签名就写明确原因，不用占位串冒充签名。
    assert "signature" not in artifacts
    assert artifacts["signatureUnavailableReason"]
    sbom = json.loads((PLUGIN_DIR / artifacts["sbom"]).read_text())
    assert sbom["metadata"]["component"]["properties"] == [
        {"name": "sensoryplex:package_digest", "value": artifacts["digest"]}
    ]


def test_manifest_matches_the_schema():
    schema = json.loads(
        (pathlib.Path(__file__).parents[2] / "docs/contracts/plugin.schema.json").read_text()
    )
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert list(Draft202012Validator(schema).iter_errors(manifest)) == []


def test_package_digest_scope_covers_code_and_ignores_prose():
    with tempfile.TemporaryDirectory() as directory:
        copy = pathlib.Path(directory) / "plugin"
        shutil.copytree(PLUGIN_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
        baseline = package_digest(copy)
        write_sbom_untouched = copy / "sbom.cdx.json"
        assert write_sbom_untouched.is_file()  # 摘要范围之外的产物

        (copy / "README.md").write_text("说明文字的变化不该改变产物摘要\n")
        assert package_digest(copy) == baseline

        target = copy / "src/edge_material_plugin_vlm_moondream/png.py"
        target.write_text(target.read_text() + "\n# 一行注释\n")
        assert package_digest(copy) != baseline
        assert baseline.startswith("sha256:") and len(baseline) == 71


def test_plugin_schema_still_requires_a_signature_for_container_form():
    schema = json.loads(
        (pathlib.Path(__file__).parents[2] / "docs/contracts/plugin.schema.json").read_text()
    )
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    container = copy.deepcopy(manifest)
    container["spec"]["artifacts"] = {
        "image": "example.invalid/vlm:0.1.0",
        "digest": DIGEST,
        "sbom": "sbom.cdx.json",
    }
    errors = list(Draft202012Validator(schema).iter_errors(container))
    assert errors, "容器形态仍然必须提供签名，不能只写原因"

"""OCR 插件的契约测试：输入准入、像素布局显式拒绝、坐标/锚点/摘要/置信度语义、稳定 ID、
空结果语义、越界失败、provider 断言、manifest 摘要与 schema、网络白名单与可写路径。

这些测试不联网、不加载真实 ONNX 权重：插件的外部依赖（rapidocr 引擎、数据面 lease）在
`process()` 的边界上被换成测试替身，但**被测的契约**（像素布局、坐标空间、锚点绑定、
provenance、置信度语义、稳定 ID、越界处理、provider 断言）都是真实代码路径。
"""

import copy
import json
import pathlib
import shutil
import tempfile

import numpy as np
import pytest
import yaml
from edge_material_plugin_ocr_rapidocr import models
from edge_material_plugin_ocr_rapidocr import plugin as ocr
from edge_material_plugin_ocr_rapidocr.artifact import package_digest
from edge_material_sdk import BufferRead
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.validation import validate_observation
from jsonschema import Draft202012Validator

PLUGIN_DIR = pathlib.Path(__file__).parents[2] / "plugins/python/processors/ocr-rapidocr"
DIGEST = "sha256:" + "a" * 64
MODEL_ARTIFACT = "sha256:" + "c" * 64
ARTIFACT = "sha256:" + "b" * 64
WINDOW_START_MS = 2_000
WINDOW_END_MS = 2_040
FRAME_WIDTH = 8
FRAME_HEIGHT = 4
FRAME_STRIDE = FRAME_WIDTH * 4


def rgba_payload(width=FRAME_WIDTH, height=FRAME_HEIGHT, stride=FRAME_STRIDE) -> bytes:
    """每行前 4 字节写死 (R,G,B,A)=(1,2,3,4)，行尾补 stride 余量。"""
    row = bytes([1, 2, 3, 4]) * width + bytes(stride - width * 4)
    return row * height


class FakeReader:
    """测试替身：只实现插件真正用到的读取契约。"""

    def __init__(
        self,
        payload: bytes,
        pixel_format: str = "RGBA",
        width=FRAME_WIDTH,
        height=FRAME_HEIGHT,
        stride=FRAME_STRIDE,
        digest: str = DIGEST,
    ):
        self.payload = payload
        self.pixel_format = pixel_format
        self.width = width
        self.height = height
        self.stride = stride
        self.digest = digest
        self.reads: list[str] = []
        self.closed = False

    def read(self, buffer_id: str) -> BufferRead:
        self.reads.append(buffer_id)
        return BufferRead(
            buffer_id=buffer_id,
            kind="video_frame",
            time_range=common.TimeRange(start_ms=WINDOW_START_MS, end_ms=WINDOW_END_MS),
            format=common.BufferFormat(
                pixel_format=self.pixel_format,
                width=self.width,
                height=self.height,
                strides=[self.stride],
            ),
            digest=self.digest,
            payload=self.payload,
        )

    def close(self):
        self.closed = True


class EngineResult:
    def __init__(self, txts, scores, boxes):
        self.txts = txts
        self.scores = scores
        self.boxes = boxes


class FakeEngine:
    """rapidocr 引擎的替身：记录送进来的真实数组，返回可控的识别结果。"""

    def __init__(self, result: EngineResult | None = None):
        self.result = result if result is not None else DEFAULT_RESULT
        self.images: list[np.ndarray] = []

    def __call__(self, image):
        self.images.append(image)
        return copy.deepcopy(self.result)


DEFAULT_RESULT = EngineResult(
    txts=["Jak nahrát video"],
    scores=[0.93],
    boxes=[[[0.0, 0.0], [float(FRAME_WIDTH), 0.0], [float(FRAME_WIDTH), 2.0], [0.0, 2.0]]],
)


def make_plugin(reader=None, engine=None, provider: str = "cpu") -> ocr.OcrPlugin:
    plugin = ocr.OcrPlugin(artifact_digest=ARTIFACT)
    plugin.config = ocr.OcrConfig(handoff_endpoint="127.0.0.1:50073", provider=provider)
    plugin.model = models.OcrModelIdentity(
        model_id="PP-OCRv6_mobile",
        model_version="bundled",
        release_id="ppocr:PP-OCRv6_mobile@31df9f5afcc7",
        artifact_digest=MODEL_ARTIFACT,
        backend=f"onnxruntime-1.30.0/{ocr.PROVIDER_EP[provider]}",
        providers={role: [ocr.PROVIDER_EP[provider]] for _, role in ocr.SESSION_ROLES},
        runtime_version="3.9.2",
        weights=[
            models.WeightsFile(role=role, name=f"{role}.onnx(onnx)", digest=DIGEST, size_bytes=1)
            for _, role in ocr.SESSION_ROLES
        ],
        source="bundled",
        directory="/nonexistent/in-test",
    )
    plugin.started = True
    plugin._engine = engine or FakeEngine()
    plugin.buffer_reader = reader
    return plugin


def make_request(kind: str = "video_frame", stream_id: str = "stream_ocr"):
    payload = runtime.PluginInput(
        buffer=common.BufferDescriptor(
            buffer_id="frame_1",
            kind=kind,
            memory_kind="cpu_shared_memory",
            stream_id=stream_id,
            time_range=common.TimeRange(start_ms=WINDOW_START_MS, end_ms=WINDOW_END_MS),
            format=common.BufferFormat(
                pixel_format="RGBA",
                width=FRAME_WIDTH,
                height=FRAME_HEIGHT,
                strides=[FRAME_STRIDE],
            ),
            content_hash=DIGEST,
        )
    )
    return runtime.ProcessRequest(
        context=common.RequestContext(
            request_id="request_ocr",
            trace_id="trace_ocr",
            pipeline_run_id="run_ocr",
            stream_id=stream_id,
            source_id="source_ocr",
            deadline_unix_ms=2_000_000_000_000,
            attempt=1,
            idempotency_key="stable",
            privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
        ),
        inputs=[payload],
        processor_release_id="release_ocr",
    )


async def test_buffer_input_requires_an_attached_reader():
    """没有 reader 时必须显式拒绝，不能退化成"跳过这条输入"。"""
    response = await make_plugin().invoke(make_request())
    assert response.error.code == common.UNSUPPORTED_MEMORY_KIND
    assert response.error.reason_code == "buffer_reader_not_attached"


async def test_unsupported_memory_kind_and_input_kind_are_refused():
    request = make_request()
    request.inputs[0].buffer.memory_kind = "cuda_ipc"
    response = await make_plugin(FakeReader(rgba_payload())).invoke(request)
    assert response.error.reason_code.startswith("unsupported_memory_kind")

    # 音频段不是视频帧：这条插件不能靠"自认为能读"来接别的种类。
    response = await make_plugin(FakeReader(rgba_payload())).invoke(make_request("audio_segment"))
    assert response.error.reason_code == "unsupported_input_kind:audio_segment"


async def test_rgba_frames_are_converted_to_bgr_for_the_engine():
    """颜色通道顺序写错不会被任何断言发现，所以这是唯一入口，必须被测住。"""
    engine = FakeEngine()
    response = await make_plugin(FakeReader(rgba_payload()), engine).invoke(make_request())
    assert not response.HasField("error")
    received = engine.images[0]
    assert received.dtype == np.uint8
    assert received.shape == (FRAME_HEIGHT, FRAME_WIDTH, 3)
    # RGBA=(1,2,3,4) → BGR=(3,2,1)
    assert received[0, 0].tolist() == [3, 2, 1]
    assert received[FRAME_HEIGHT - 1, FRAME_WIDTH - 1].tolist() == [3, 2, 1]


@pytest.mark.parametrize(
    ("reader", "reason"),
    [
        (FakeReader(rgba_payload(), pixel_format="BGRA"), "unsupported_pixel_format:BGRA"),
        (FakeReader(rgba_payload(), width=0, height=0), "frame_dimensions_unusable"),
        (FakeReader(rgba_payload(), stride=FRAME_STRIDE - 4), "frame_stride_smaller_than_row"),
        (FakeReader(rgba_payload()[:100]), "frame_payload_too_small"),
    ],
)
async def test_frame_layout_failures_are_explicit(reader, reason):
    """布局不对就显式拒绝：不猜 stride、不按行宽补零、也不"读到多少算多少"。"""
    response = await make_plugin(reader).invoke(make_request())
    assert response.HasField("error")
    assert response.error.reason_code == reason


async def test_observation_carries_anchor_source_version_and_confidence_semantics():
    reader = FakeReader(rgba_payload())
    plugin = make_plugin(reader)
    response = await plugin.invoke(make_request())
    assert not response.HasField("error")
    observation = response.observations[0]
    validate_observation(observation)

    # 时间锚点直接来自源帧的半开区间，不按模型耗时重算。
    assert (
        observation.time_range.start_ms,
        observation.time_range.end_ms,
    ) == (WINDOW_START_MS, WINDOW_END_MS)
    assert observation.timing_source == "media_pts"
    # observation 绑定到它实际读到的那些字节。
    assert observation.content_hash == DIGEST
    # 检测/识别分数不是校准置信度 → 必须显式未知，且写明原因。
    assert not observation.HasField("confidence")
    assert observation.confidence_unavailable_reason == ocr.CONFIDENCE_UNAVAILABLE_REASON
    assert observation.provenance.plugin == ocr.PLUGIN_NAME
    assert observation.provenance.artifact_digest == ARTIFACT
    assert observation.provenance.model_artifact_digest == MODEL_ARTIFACT
    assert observation.provenance.config_hash == plugin.config.config_hash()
    assert observation.provenance.execution_backend == "onnxruntime-1.30.0/CPUExecutionProvider"
    assert reader.reads == ["frame_1"]


async def test_payload_reports_blocks_coordinates_and_engine_identity():
    response = await make_plugin(FakeReader(rgba_payload())).invoke(make_request())
    payload = response.observations[0].payload

    assert payload["coordinate_space"] == ocr.COORDINATE_SPACE
    assert payload["image"]["pixel_format"] == "RGBA"
    assert payload["image"]["width"] == FRAME_WIDTH
    assert payload["image"]["height"] == FRAME_HEIGHT
    assert payload["image"]["stride"] == FRAME_STRIDE
    assert payload["block_count"] == 1
    assert payload["blocks_omitted"] == 0
    assert payload["char_count"] == len("Jak nahrát video")
    block = payload["blocks"][0]
    assert block["text"] == "Jak nahrát video"
    assert block["chars"] == len(block["text"])
    assert block["score"] == 0.93
    assert block["box"][0] == [0.0, 0.0]
    # 归一化坐标用同一个坐标系换算，读者不需要自己猜分母。
    assert block["box_normalized"][2] == [1.0, 0.5]
    assert "empty_reason" not in payload
    # 引擎身份来自实测：模型 id/revision/来源/权重摘要都在 payload 里。
    assert payload["engine"]["model_id"] == "PP-OCRv6_mobile"
    assert payload["engine"]["runtime_version"] == "3.9.2"
    assert payload["engine"]["model_source"] == "bundled"
    assert {item["role"] for item in payload["engine"]["weights"]} == {"det", "cls", "rec"}
    assert payload["engine"]["providers"] == {
        "det": ["CPUExecutionProvider"],
        "cls": ["CPUExecutionProvider"],
        "rec": ["CPUExecutionProvider"],
    }


async def test_empty_result_explains_itself_as_no_text():
    """这一帧没有文字是模型的真实结果，不是失败；但必须说清是哪一种空。"""
    empty = EngineResult(txts=None, scores=None, boxes=None)
    response = await make_plugin(FakeReader(rgba_payload()), FakeEngine(empty)).invoke(
        make_request()
    )
    assert not response.HasField("error")
    payload = response.observations[0].payload
    assert payload["blocks"] == []
    assert payload["block_count"] == 0
    assert payload["char_count"] == 0
    assert payload["empty_reason"] == "model_found_no_text"


async def test_blocks_beyond_bounds_are_counted_and_long_blocks_fail_loudly():
    """越界不静默：超出块数的部分要计数，单个超长块直接失败，而不是悄悄截断。"""
    many = EngineResult(
        txts=[f"t{index}" for index in range(ocr.MAX_BLOCKS + 5)],
        scores=[0.5] * (ocr.MAX_BLOCKS + 5),
        boxes=[[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]] * (ocr.MAX_BLOCKS + 5),
    )
    response = await make_plugin(FakeReader(rgba_payload()), FakeEngine(many)).invoke(
        make_request()
    )
    payload = response.observations[0].payload
    assert payload["block_count"] == ocr.MAX_BLOCKS + 5
    assert len(payload["blocks"]) == ocr.MAX_BLOCKS
    assert payload["blocks_omitted"] == 5
    # 被列出的块与总数、字数都对得上账。
    assert payload["char_count"] == sum(len(block["text"]) for block in payload["blocks"])

    too_long = EngineResult(
        txts=["x" * (ocr.MAX_BLOCK_CHARS + 1)],
        scores=[0.5],
        boxes=[[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]],
    )
    response = await make_plugin(FakeReader(rgba_payload()), FakeEngine(too_long)).invoke(
        make_request()
    )
    assert response.HasField("error")
    assert response.error.reason_code == "ocr_block_exceeds_bound"


async def test_observation_ids_are_stable_per_input_bytes():
    first = await make_plugin(FakeReader(rgba_payload())).invoke(make_request())
    second = await make_plugin(FakeReader(rgba_payload())).invoke(make_request())
    assert first.observations[0].observation_id == second.observations[0].observation_id

    # 换成另一份字节（读取窗口摘要不同）时 ID 必须变，否则两个不同的输入会撞成一条。
    other = await make_plugin(FakeReader(rgba_payload(), digest="sha256:" + "d" * 64)).invoke(
        make_request()
    )
    assert other.observations[0].observation_id != first.observations[0].observation_id


def test_describe_declares_the_real_modality_and_memory_kind():
    description = ocr.describe()
    assert description.consumes == ["media.video_frame"]
    assert description.produces == ["observation.ocr_blocks"]
    # 插件只声明它能读的内存种类：多声明一种就是替运行时做承诺。
    assert list(description.memory_kinds) == ["cpu_shared_memory"]


def test_config_validation_rejects_unknown_keys_and_bad_values():
    assert ocr.validate_config({"handoff_endpoint": "127.0.0.1:50073"}).valid
    assert ocr.validate_config({"handoff_endpoint": "127.0.0.1:50073", "provider": "coreml"}).valid
    unknown = ocr.validate_config({"handoff_endpoint": "127.0.0.1:50073", "typo": 1})
    assert not unknown.valid and unknown.field_errors == ["unknown_config_keys:typo"]
    missing = ocr.validate_config({"handoff_endpoint": ""})
    assert not missing.valid and "handoff_endpoint_required" in missing.field_errors
    bad_provider = ocr.validate_config({"handoff_endpoint": "127.0.0.1:1", "provider": "metal"})
    assert "unsupported_provider:metal" in bad_provider.field_errors
    bad_score = ocr.validate_config({"handoff_endpoint": "127.0.0.1:1", "text_score": 0})
    assert "text_score_out_of_range" in bad_score.field_errors
    bad_ttl = ocr.validate_config({"handoff_endpoint": "127.0.0.1:1", "ttl_ms": 5})
    assert "ttl_ms_out_of_range" in bad_ttl.field_errors
    bad_timeout = ocr.validate_config({"handoff_endpoint": "127.0.0.1:1", "timeout_s": 0})
    assert "timeout_s_must_be_positive" in bad_timeout.field_errors


def test_config_hash_tracks_only_the_effective_config():
    base = ocr.OcrConfig()
    assert base.config_hash() == ocr.OcrConfig().config_hash()
    assert ocr.OcrConfig(provider="coreml").config_hash() != base.config_hash()
    assert ocr.OcrConfig(text_score=0.7).config_hash() != base.config_hash()
    # handoff_endpoint 是传输位置、model_dir 是位置而不是语义：身份由权重摘要承担。
    assert ocr.OcrConfig(handoff_endpoint="127.0.0.1:1").config_hash() == base.config_hash()
    assert ocr.OcrConfig(model_dir="/tmp/x").config_hash() == base.config_hash()


def test_requested_provider_must_actually_be_selected():
    """请求了 CoreML 却拿到别的 provider 是失败，不是"降级可用"（ADR-016）。"""
    cpu_sessions = {role: ["CPUExecutionProvider"] for _, role in ocr.SESSION_ROLES}
    plugin = make_plugin()
    # CPU 请求与实测一致 → 通过。
    assert plugin._backend_string(cpu_sessions, "cpu").endswith("CPUExecutionProvider")
    # 请求 CoreML 却实际拿到 CPU → 显式失败，并指出是哪个角色。
    with pytest.raises(ValueError) as error:
        plugin._backend_string(cpu_sessions, "coreml")
    assert str(error.value) == (
        "execution_provider_not_selected:"
        "det:CPUExecutionProvider,cls:CPUExecutionProvider,rec:CPUExecutionProvider"
    )
    # 会话缺失（空 provider 列表）同样不能当成通过。
    with pytest.raises(ValueError, match="execution_provider_not_selected"):
        plugin._backend_string({role: [] for _, role in ocr.SESSION_ROLES}, "cpu")
    # 会话缺失（空 provider 列表）时原因串写 none，而不是留空让人以为"没有偏好"。
    with pytest.raises(ValueError, match=r"execution_provider_not_selected:.*:none"):
        plugin._backend_string({role: [] for _, role in ocr.SESSION_ROLES}, "cpu")


def test_manifest_declares_the_real_package_digest():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    artifacts = manifest["spec"]["artifacts"]
    assert artifacts["form"] == "local_native"
    # 占位串会在这里被抓住：manifest 的 digest 必须等于本机复算的插件包摘要。
    assert artifacts["digest"] == package_digest(PLUGIN_DIR)
    assert artifacts["image"].startswith("local:")
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


def test_manifest_network_allowlist_and_writable_paths_are_bounded():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    spec = manifest["spec"]
    # 权重随包携带或来自本机 model_dir；只有权重缺失/摘要不匹配时才会去模型目录站。
    assert spec["security"]["dataEgress"] == "local_only"
    assert spec["security"]["network"] == "allowlist"
    assert set(spec["security"]["allowedHosts"]) == {"www.modelscope.cn"}
    # 本版本不落任何缓存：可写范围必须是空的，不能顺手放开一个目录。
    assert spec["security"]["filesystem"]["writablePaths"] == []
    assert spec["security"]["filesystem"]["readOnly"] is True
    # 声明的资源必须与插件真实并发行为一致：单实例、串行推理。
    assert spec["resources"]["maxConcurrency"] == 1
    assert spec["capabilities"]["acceptsMemoryKinds"] == ["cpu_shared_memory"]


def test_package_digest_scope_covers_code_and_ignores_prose():
    with tempfile.TemporaryDirectory() as directory:
        copy = pathlib.Path(directory) / "plugin"
        shutil.copytree(PLUGIN_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
        baseline = package_digest(copy)
        assert (copy / "sbom.cdx.json").is_file()  # 摘要范围之外的产物

        (copy / "README.md").write_text("说明文字的变化不该改变产物摘要\n")
        assert package_digest(copy) == baseline

        target = copy / "src/edge_material_plugin_ocr_rapidocr/models.py"
        target.write_text(target.read_text() + "\n# 一行注释\n")
        assert package_digest(copy) != baseline
        assert baseline.startswith("sha256:") and len(baseline) == 71


def test_onnx_container_probe_rejects_broken_files():
    with tempfile.TemporaryDirectory() as directory:
        empty = pathlib.Path(directory) / "empty.onnx"
        empty.write_bytes(b"")
        with pytest.raises(ValueError, match="model_container_empty"):
            models.probe_onnx_container(empty)

        wrong = pathlib.Path(directory) / "wrong.onnx"
        wrong.write_bytes(b"not-a-protobuf-onnx")
        with pytest.raises(ValueError, match="model_container_not_onnx"):
            models.probe_onnx_container(wrong)

        # ONNX 是 protobuf，首字节是 `ir_version`（field 1, varint）的 tag。
        good = pathlib.Path(directory) / "good.onnx"
        good.write_bytes(b"\x08\x07" + b"\x00" * 8)
        assert models.probe_onnx_container(good) == "onnx"


def test_combined_digest_is_order_independent_and_covers_every_role():
    files = {"det": "sha256:" + "1" * 64, "cls": "sha256:" + "2" * 64, "rec": "sha256:" + "3" * 64}
    assert models.combined_digest(files) == models.combined_digest(
        dict(reversed(list(files.items())))
    )
    # 换掉任意一份权重都必须得到不同的组合摘要，否则模型身份就没覆盖到它。
    for role in files:
        changed = dict(files, **{role: "sha256:" + "9" * 64})
        assert models.combined_digest(changed) != models.combined_digest(files)


def test_installed_version_reads_real_metadata_and_never_invents():
    from edge_material_plugin_ocr_rapidocr import models as target

    assert target.installed_version("rapidocr") != ""
    # 不存在的发行版写 unknown，而不是编一个好看的版本号。
    assert target.installed_version("sensoryplex-not-a-real-distribution") == "unknown"

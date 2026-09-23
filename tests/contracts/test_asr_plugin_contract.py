"""ASR 插件的契约测试：输入准入、锚点、provenance、置信度语义、稳定 ID、音频布局显式拒绝。

这些测试不联网、不加载真实权重：插件的外部依赖（mlx-whisper 后端、数据面 lease）在
`process()` 的边界上被换成测试替身，但**被测的契约**（输入准入、样本布局、锚点绑定、
provenance、置信度语义、稳定 ID、段相对时间换算）都是真实代码路径。
"""

import copy
import json
import math
import pathlib
import shutil
import struct
import tempfile
import time

import numpy as np
import pytest
import yaml
from edge_material_plugin_asr_whisper_mlx import audio
from edge_material_plugin_asr_whisper_mlx import plugin as asr
from edge_material_plugin_asr_whisper_mlx.artifact import package_digest
from edge_material_sdk import BufferRead
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime
from edge_material_sdk.validation import validate_observation
from jsonschema import Draft202012Validator

PLUGIN_DIR = pathlib.Path(__file__).parents[2] / "plugins/python/processors/asr-whisper-mlx"
DIGEST = "sha256:" + "a" * 64
MODEL_ARTIFACT = "sha256:" + "c" * 64
ARTIFACT = "sha256:" + "b" * 64
WINDOW_START_MS = 1_000
WINDOW_END_MS = 6_000


def f32le(*frames: tuple[float, ...]) -> bytes:
    """按 interleaved F32LE 造载荷：测试数据也必须走与被测代码同一条解释路径。"""
    return b"".join(struct.pack("<f", value) for frame in frames for value in frame)


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
            kind="audio_segment",
            time_range=common.TimeRange(start_ms=WINDOW_START_MS, end_ms=WINDOW_END_MS),
            format=common.BufferFormat(sample_rate=48_000, channels=2, sample_format="F32LE"),
            digest=DIGEST,
            payload=self.payload,
        )

    def close(self):
        self.closed = True


class FakeBackend:
    """mlx-whisper 的替身：记录真实调用参数，转写结果是可控的。"""

    def __init__(self, result: dict | None = None):
        self.result = result if result is not None else DEFAULT_RESULT
        self.calls: list[dict] = []

    def transcribe(self, waveform, **kwargs):
        self.calls.append(
            {
                "samples": int(waveform.shape[0]),
                "dtype": str(waveform.dtype),
                **{key: value for key, value in kwargs.items() if key != "path_or_hf_repo"},
            }
        )
        return copy.deepcopy(self.result)


DEFAULT_RESULT = {
    "text": "  hello from a segment  ",
    "language": "en",
    "segments": [
        {
            "start": 0.5,
            "end": 1.25,
            "text": " hello from a segment ",
            "avg_logprob": -0.31,
            "no_speech_prob": 0.01,
            "compression_ratio": 0.9,
            "temperature": 0.0,
        }
    ],
}


def make_plugin(reader=None, backend=None) -> asr.WhisperAsrPlugin:
    plugin = asr.WhisperAsrPlugin(artifact_digest=ARTIFACT)
    plugin.config = asr.AsrConfig(handoff_endpoint="127.0.0.1:50072")
    plugin.model = asr.ModelIdentity(
        model_id="mlx-community/whisper-tiny-mlx",
        model_version="a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
        release_id="mlx-whisper:mlx-community/whisper-tiny-mlx@951ed3fc1203",
        artifact_digest=MODEL_ARTIFACT,
        backend="mlx-0.32.2",
        weights_file="weights.safetensors",
        weights_container="safetensors",
        source="hub",
        directory="/nonexistent/in-test",
    )
    plugin.started = True
    plugin._backend = backend or FakeBackend()
    plugin.buffer_reader = reader
    return plugin


def make_request(kind: str = "audio_segment", stream_id: str = "stream_asr"):
    payload = runtime.PluginInput(
        buffer=common.BufferDescriptor(
            buffer_id="seg_1",
            kind=kind,
            memory_kind="cpu_shared_memory",
            stream_id=stream_id,
            time_range=common.TimeRange(start_ms=WINDOW_START_MS, end_ms=WINDOW_END_MS),
            format=common.BufferFormat(sample_rate=48_000, channels=2, sample_format="F32LE"),
            content_hash=DIGEST,
        )
    )
    return runtime.ProcessRequest(
        context=common.RequestContext(
            request_id="request_asr",
            trace_id="trace_asr",
            pipeline_run_id="run_asr",
            stream_id=stream_id,
            source_id="source_asr",
            deadline_unix_ms=int(time.time() * 1000) + 5000,
            attempt=1,
            idempotency_key="stable",
            privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
        ),
        inputs=[payload],
        processor_release_id="release_asr",
    )


def stereo_payload(frames: int = 240) -> bytes:
    """240 帧 @48 kHz 立体声 = 5 ms 音频，足够触发重采样但不产生大对象。"""
    return f32le(*[(0.5, -0.5) for _ in range(frames)])


async def test_buffer_input_requires_an_attached_reader():
    """没有 reader 时必须显式拒绝，不能退化成"跳过这条输入"。"""
    response = await make_plugin().invoke(make_request())
    assert response.error.code == common.UNSUPPORTED_MEMORY_KIND
    assert response.error.reason_code == "buffer_reader_not_attached"


async def test_unsupported_memory_kind_and_input_kind_are_refused():
    request = make_request()
    request.inputs[0].buffer.memory_kind = "cuda_ipc"
    response = await make_plugin(FakeReader(stereo_payload())).invoke(request)
    assert response.error.reason_code.startswith("unsupported_memory_kind")

    # 视频帧不是音频段：这条插件不能靠"自认为能读"来接别的种类。
    response = await make_plugin(FakeReader(stereo_payload())).invoke(make_request("video_frame"))
    assert response.error.reason_code == "unsupported_input_kind:video_frame"


async def test_observation_carries_anchor_source_version_and_confidence_semantics():
    reader = FakeReader(stereo_payload())
    backend = FakeBackend()
    plugin = make_plugin(reader, backend)
    response = await plugin.invoke(make_request())
    assert not response.HasField("error")
    observation = response.observations[0]
    validate_observation(observation)

    # 时间锚点直接来自源音频段的半开区间，不按模型耗时重算。
    assert (
        observation.time_range.start_ms,
        observation.time_range.end_ms,
    ) == (WINDOW_START_MS, WINDOW_END_MS)
    assert observation.timing_source == "media_pts"
    # observation 绑定到它实际读到的那些字节。
    assert observation.content_hash == DIGEST
    # 解码诊断不是校准置信度 → 必须显式未知，且写明原因。
    assert not observation.HasField("confidence")
    assert observation.confidence_unavailable_reason == asr.CONFIDENCE_UNAVAILABLE_REASON
    assert observation.provenance.plugin == asr.PLUGIN_NAME
    assert observation.provenance.artifact_digest == ARTIFACT
    assert observation.provenance.model_artifact_digest == MODEL_ARTIFACT
    assert observation.provenance.config_hash == plugin.config.config_hash()
    assert reader.reads == ["seg_1"]


async def test_payload_reports_input_facts_and_the_real_transcribe_call():
    backend = FakeBackend()
    response = await make_plugin(FakeReader(stereo_payload(240)), backend).invoke(make_request())
    payload = response.observations[0].payload

    assert payload["text"] == "hello from a segment"
    assert payload["language"] == "en"
    assert payload["input"]["sample_format"] == "F32LE"
    assert payload["input"]["input_sample_rate"] == 48_000
    assert payload["input"]["input_channels"] == 2
    assert payload["input"]["input_samples"] == 240
    assert payload["input"]["whisper_sample_rate"] == 16_000
    assert payload["input"]["whisper_samples"] == math.ceil(240 / 3)
    assert payload["segment_timing"] == "media_pts_window_relative_plus_window_start"
    assert payload["inference"]["weights_file"] == "weights.safetensors"
    assert payload["inference"]["weights_container"] == "safetensors"
    assert "empty_transcript_reason" not in payload

    # 后端的调用参数也是契约的一部分：16 kHz 单声道、安静模式、语言/任务原样透传。
    call = backend.calls[0]
    assert call["samples"] == math.ceil(240 / 3)
    assert call["dtype"] == "float32"
    assert call["language"] is None
    assert call["task"] == "transcribe"
    assert call["verbose"] is None


async def test_out_of_window_sub_segments_are_flagged_not_clamped_or_dropped():
    """Whisper 幻觉时会给越窗时间戳（实测 5 秒窗口上的 `[940, 29880]`）。

    契约要求的是**不静默**：原值保留、显式标记、计数对得上；既不夹取成合法区间，
    也不悄悄丢掉这段文本——夹取会让一个越界的时序看起来像测得值。
    """
    hallucinated = FakeBackend(
        {
            "text": "Rik for at man prist",
            "language": "no",
            "segments": [{"start": 0.94, "end": 29.88, "text": "Rik for at man prist"}],
        }
    )
    response = await make_plugin(FakeReader(stereo_payload()), hallucinated).invoke(make_request())
    payload = response.observations[0].payload
    segment = payload["segments"][0]
    assert (segment["start_ms"], segment["end_ms"]) == (
        WINDOW_START_MS + 940,
        WINDOW_START_MS + 29880,
    )
    assert segment["timing_outside_window"] is True
    assert payload["segments_outside_window"] == 1
    # 文本没有被丢掉，也没有因为越窗被改写成空。
    assert payload["text"] == "Rik for at man prist"
    # 观察本身的锚点仍然是源段区间：越窗的是**子段时序**，不是这段字节的来源。
    assert (
        response.observations[0].time_range.start_ms,
        response.observations[0].time_range.end_ms,
    ) == (WINDOW_START_MS, WINDOW_END_MS)


async def test_segment_times_are_window_relative_plus_window_start():
    response = await make_plugin(FakeReader(stereo_payload())).invoke(make_request())
    segment = response.observations[0].payload["segments"][0]
    # 模型给的 0.5 s / 1.25 s 是窗口内相对时间；写进时间轴时必须加上窗口起点。
    assert (segment["start_ms"], segment["end_ms"]) == (
        WINDOW_START_MS + 500,
        WINDOW_START_MS + 1250,
    )
    assert segment["avg_logprob"] == pytest.approx(-0.31)
    assert segment["compression_ratio"] == pytest.approx(0.9)
    # 窗口内的子段必须**不**被标记为越窗，计数为 0：标记不能是"一律为真"。
    assert segment["timing_outside_window"] is False
    assert response.observations[0].payload["segments_outside_window"] == 0


async def test_observation_id_is_derived_from_stable_inputs():
    first = await make_plugin(FakeReader(stereo_payload())).invoke(make_request())
    second = await make_plugin(FakeReader(stereo_payload())).invoke(make_request())
    assert first.observations[0].observation_id == second.observations[0].observation_id

    other_stream = await make_plugin(FakeReader(stereo_payload())).invoke(
        make_request(stream_id="stream_other")
    )
    assert other_stream.observations[0].observation_id != first.observations[0].observation_id


async def test_an_empty_transcript_is_reported_with_its_own_reason():
    """静音段上"空转写"是模型的真实结果，不是失败；但必须说清是哪一种空。"""
    silent = FakeBackend({"text": "  ", "language": "en", "segments": []})
    response = await make_plugin(FakeReader(stereo_payload()), silent).invoke(make_request())
    assert not response.HasField("error")
    payload = response.observations[0].payload
    assert payload["text"] == ""
    assert payload["empty_transcript_reason"] == "model_returned_no_segments"

    segmentless = FakeBackend(
        {"text": "", "language": "en", "segments": [{"start": 0.0, "end": 1.0, "text": " "}]}
    )
    response = await make_plugin(FakeReader(stereo_payload()), segmentless).invoke(make_request())
    payload = response.observations[0].payload
    # 分段存在但没有文本，是另一种空：不能和"模型没给段"混成一个原因。
    assert payload["empty_transcript_reason"] == "model_returned_empty_text"


async def test_an_oversized_transcript_fails_instead_of_being_truncated():
    long_text = FakeBackend(
        {
            "text": "x" * (asr.MAX_TRANSCRIPT_CHARS + 1),
            "language": "en",
            "segments": [],
        }
    )
    response = await make_plugin(FakeReader(stereo_payload()), long_text).invoke(make_request())
    assert response.error.reason_code == "transcript_exceeds_bound"
    assert response.error.retryable is True


async def test_a_backend_failure_never_leaks_its_internals():
    class BrokenBackend(FakeBackend):
        def transcribe(self, waveform, **kwargs):
            raise RuntimeError("/Users/someone/secret/weights.safetensors shape (1,80,3000)")

    response = await make_plugin(FakeReader(stereo_payload()), BrokenBackend()).invoke(
        make_request()
    )
    assert response.error.reason_code == "mlx_whisper_transcribe_failed"
    assert "secret" not in response.error.reason_code


async def test_an_unknown_sample_layout_is_refused_before_transcribing():
    """布局未知时不能按 4 字节/样本去猜：那会静默改变模型看到的内容。"""
    reader = FakeReader(stereo_payload())
    reader.read = lambda buffer_id: BufferRead(
        buffer_id=buffer_id,
        kind="audio_segment",
        time_range=common.TimeRange(start_ms=WINDOW_START_MS, end_ms=WINDOW_END_MS),
        format=common.BufferFormat(sample_rate=48_000, channels=2, sample_format=""),
        digest=DIGEST,
        payload=stereo_payload(),
    )
    backend = FakeBackend()
    response = await make_plugin(reader, backend).invoke(make_request())
    assert response.error.code == common.INVALID_INPUT
    assert response.error.reason_code == "unsupported_sample_format:unset"
    assert backend.calls == []


def test_frames_from_f32le_refuses_every_ambiguous_payload():
    with pytest.raises(audio.AudioError, match="unsupported_sample_format:S16LE"):
        audio.frames_from_f32le(b"\x00\x00", "S16LE", 1)
    with pytest.raises(audio.AudioError, match="unsupported_sample_format:unset"):
        audio.frames_from_f32le(b"\x00\x00", "", 1)
    with pytest.raises(audio.AudioError, match="audio_channel_count_unknown"):
        audio.frames_from_f32le(b"\x00\x00", "F32LE", 0)
    with pytest.raises(audio.AudioError, match="empty_audio_payload"):
        audio.frames_from_f32le(b"", "F32LE", 1)
    with pytest.raises(audio.AudioError, match="audio_payload_not_frame_aligned"):
        audio.frames_from_f32le(b"\x00" * 10, "F32LE", 2)


def test_downmix_and_resample_are_deterministic_and_checkable():
    frames = audio.frames_from_f32le(f32le((1.0, 0.0), (0.0, 1.0)), "F32LE", 2)
    assert frames.shape == (2, 2)
    # 多声道按算术平均下混：确定性的取值，而不是"取第一路"。
    assert list(audio.to_mono(frames)) == [0.5, 0.5]

    tone = np.sin(np.linspace(0, 2 * math.pi, 4_800, dtype=np.float64))
    converted = audio.resample(tone.astype(np.float32), 48_000)
    assert converted.dtype == np.float32
    assert converted.shape[0] == 1_600
    # 已经是目标采样率时不重采样，直接返回同一段波形。
    assert audio.resample(converted, 16_000).shape[0] == 1_600
    with pytest.raises(audio.AudioError, match="audio_sample_rate_unknown"):
        audio.resample(converted, 0)


def test_config_validation_rejects_unknown_keys_and_missing_endpoint():
    assert asr.validate_config({"handoff_endpoint": "127.0.0.1:50072"}).valid
    unknown = asr.validate_config({"handoff_endpoint": "127.0.0.1:50072", "typo": 1})
    assert not unknown.valid and unknown.field_errors == ["unknown_config_keys:typo"]
    missing = asr.validate_config({"handoff_endpoint": ""})
    assert not missing.valid and "handoff_endpoint_required" in missing.field_errors
    bad_task = asr.validate_config({"handoff_endpoint": "127.0.0.1:1", "task": "summarize"})
    assert "unsupported_task:summarize" in bad_task.field_errors
    bad_language = asr.validate_config({"handoff_endpoint": "127.0.0.1:1", "language": "english"})
    assert "language_must_be_a_code" in bad_language.field_errors
    assert not asr.validate_config({"handoff_endpoint": "127.0.0.1:1", "ttl_ms": 5}).valid


def test_config_hash_tracks_only_the_effective_config():
    base = asr.AsrConfig()
    assert base.config_hash() == asr.AsrConfig().config_hash()
    assert asr.AsrConfig(language="en").config_hash() != base.config_hash()
    assert asr.AsrConfig(task="translate").config_hash() != base.config_hash()
    # handoff_endpoint 是传输位置、model_dir 是位置而不是语义：身份由权重摘要承担。
    assert asr.AsrConfig(handoff_endpoint="127.0.0.1:1").config_hash() == base.config_hash()
    assert asr.AsrConfig(model_dir="/tmp/x").config_hash() == base.config_hash()


def test_describe_declares_the_real_modality_and_memory_kind():
    description = asr.describe()
    assert description.consumes == ["media.audio_segment"]
    assert description.produces == ["observation.asr_segment"]
    # 插件只声明它能读的内存种类：多声明一种就是替运行时做承诺。
    assert list(description.memory_kinds) == ["cpu_shared_memory"]


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
    # 模型权重来自 Hugging Face；除此之外不允许任何 egress。
    assert spec["security"]["dataEgress"] == "local_only"
    assert spec["security"]["network"] == "allowlist"
    assert set(spec["security"]["allowedHosts"]) == {
        "huggingface.co",
        "cdn-lfs.huggingface.co",
        "cdn-lfs-us-1.hf.co",
    }
    # 可写范围只能是权重缓存目录，不能是整个 HOME。
    assert spec["security"]["filesystem"]["writablePaths"] == ["~/.cache/huggingface"]
    # 只读挂载：模型权重是唯一需要写盘的东西，插件本身不该写别的路径。
    assert spec["security"]["filesystem"]["readOnly"] is True


def test_package_digest_scope_covers_code_and_ignores_prose():
    with tempfile.TemporaryDirectory() as directory:
        copy = pathlib.Path(directory) / "plugin"
        shutil.copytree(PLUGIN_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
        baseline = package_digest(copy)
        assert (copy / "sbom.cdx.json").is_file()  # 摘要范围之外的产物

        (copy / "README.md").write_text("说明文字的变化不该改变产物摘要\n")
        assert package_digest(copy) == baseline

        target = copy / "src/edge_material_plugin_asr_whisper_mlx/audio.py"
        target.write_text(target.read_text() + "\n# 一行注释\n")
        assert package_digest(copy) != baseline
        assert baseline.startswith("sha256:") and len(baseline) == 71

"""ASR 转写插件：读 Runtime 数据面里的真实音频段，用本机 MLX Whisper 权重产出带锚点的转写。

它消费的是**字节**，不是文件名：每段音频都通过 `LeaseBufferReader` 按 lease 读取、
校验窗口摘要后才送进模型。权重来自本机（HF 缓存或显式目录）、推理在本机执行，媒体不出网。

刻意保持的语义：

- 音频字节只出现在"送进模型"这一条路径上：不进日志、不进控制消息、不进返回的 payload；
- 模型不给校准置信度，因此 `confidence` 留空并写明确原因，绝不填一个看起来合理的数字；
  解码诊断（`avg_logprob` / `no_speech_prob` / `compression_ratio`）原样带出，且**不**当作
  置信度使用——它们是解码器的中间量，不是校准概率；
- 时间锚点直接取 descriptor 的半开区间 `[start_ms, end_ms)`，不重新计时；模型返回的相对时间
  只用于把窗口切成子段（`window_start + relative`），绝不改变窗口本身；
- 模型身份来自**实际要被加载的那个权重文件**的字节摘要，不是配置里写的版本号；
- 输出 ID 由稳定输入派生（stream/source/buffer/摘要/版本/配置），重放同一段得到同一个 ID；
- 非音频段输入、未知样本布局、未知权重要么显式拒绝，要么显式失败，绝不静默跳过或猜。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import pathlib
import time
import zipfile
from dataclasses import dataclass, field

import grpc
from edge_material_sdk import BufferReadError, PluginError, ProcessorPlugin
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime

from . import audio

PLUGIN_NAME = "org.sensoryplex.asr-whisper-mlx"
PLUGIN_VERSION = "0.1.0"
MODALITY = "asr_segment"
CONSUMES = "media.audio_segment"
INPUT_KIND = "audio_segment"
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
# 模型不提供校准置信度：这是模型的属性，不是缺失的数据，必须显式写出来。
CONFIDENCE_UNAVAILABLE_REASON = "model_does_not_report_calibrated_confidence"
DEFAULT_TIMEOUT_S = 300.0
# 输出有上限：一段 30 秒以内的音频不可能合理地产生更多子段/更长的文本。
MAX_LISTED_SEGMENTS = 64
MAX_TRANSCRIPT_CHARS = 8_000
# 与 mlx_whisper.load_model 的取值顺序一致：先 safetensors，再 npz。
WEIGHT_FILES = ("weights.safetensors", "weights.npz")
# load_model 会先摘掉这两个键再构造 ModelDimensions，校验时同样排除。
MODEL_CONFIG_KEYS_IGNORED = ("model_type", "quantization")
HASH_CHUNK_BYTES = 1 << 20

CONFIG_KEYS = (
    "handoff_endpoint",
    "model",
    "model_revision",
    "model_dir",
    "language",
    "task",
    "word_timestamps",
    "timeout_s",
    "ttl_ms",
)
TASKS = ("transcribe", "translate")


@dataclass
class AsrConfig:
    handoff_endpoint: str = ""
    model: str = DEFAULT_MODEL
    model_revision: str = ""
    model_dir: str = ""
    language: str | None = None
    task: str = "transcribe"
    word_timestamps: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S
    ttl_ms: int = 30_000

    @classmethod
    def from_mapping(cls, config: dict) -> AsrConfig:
        unknown = sorted(set(config) - set(CONFIG_KEYS))
        if unknown:
            raise ValueError(f"unknown_config_keys:{','.join(unknown)}")
        fields = {key: config[key] for key in CONFIG_KEYS if key in config}
        return cls(**{"model": DEFAULT_MODEL, **fields})

    def effective(self) -> dict:
        """语义配置：`model_dir` 是位置而不是语义（身份由权重摘要承担），
        `handoff_endpoint` 是传输位置，两者都不进配置摘要。"""
        return {
            "model": self.model,
            "model_revision": self.model_revision,
            "language": self.language,
            "task": self.task,
            "word_timestamps": bool(self.word_timestamps),
            "timeout_s": float(self.timeout_s),
            "ttl_ms": int(self.ttl_ms),
        }

    def config_hash(self) -> str:
        """配置的规范摘要：同一次配置永远得到同一个值，且可被别人复算。"""
        blob = json.dumps(self.effective(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class ModelIdentity:
    """模型身份来自**即将加载的权重文件**本身（真实字节摘要），不是配置里写的版本号。"""

    model_id: str
    model_version: str
    release_id: str
    artifact_digest: str
    backend: str
    weights_file: str
    weights_container: str
    source: str
    directory: str = field(default="", repr=False)


def describe() -> runtime.PluginDescription:
    return runtime.PluginDescription(
        name=PLUGIN_NAME,
        version=PLUGIN_VERSION,
        protocol="v1",
        consumes=[CONSUMES],
        produces=[f"observation.{MODALITY}"],
        memory_kinds=["cpu_shared_memory"],
    )


def validate_config(config: dict) -> runtime.ValidationResult:
    errors: list[str] = []
    try:
        parsed = AsrConfig.from_mapping(dict(config))
    except (ValueError, TypeError) as error:
        return runtime.ValidationResult(valid=False, field_errors=[str(error)])
    if not parsed.handoff_endpoint:
        errors.append("handoff_endpoint_required")
    if not parsed.model and not parsed.model_dir:
        errors.append("model_or_model_dir_required")
    if parsed.task not in TASKS:
        errors.append(f"unsupported_task:{parsed.task}")
    if parsed.language is not None and not (2 <= len(parsed.language) <= 3):
        errors.append("language_must_be_a_code")
    if not 50 <= int(parsed.ttl_ms) <= 60_000:
        errors.append("ttl_ms_out_of_range")
    if parsed.timeout_s <= 0:
        errors.append("timeout_s_must_be_positive")
    return runtime.ValidationResult(valid=not errors, field_errors=errors)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def probe_weight_container(path: pathlib.Path) -> str:
    """真实读一次权重容器头：损坏或截断的权重必须在 Start 就被拒，而不是等到第一次推理。"""
    if path.suffix == ".safetensors":
        with path.open("rb") as handle:
            raw_length = handle.read(8)
            if len(raw_length) != 8:
                raise ValueError("model_weights_container_unreadable")
            header_length = int.from_bytes(raw_length, "little")
            if not 0 < header_length <= 64 * 1024 * 1024:
                raise ValueError("model_weights_header_length_invalid")
            header = handle.read(header_length)
        try:
            entries = json.loads(header)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("model_weights_header_not_json") from None
        if not isinstance(entries, dict) or not entries:
            raise ValueError("model_weights_header_empty")
        return "safetensors"
    if path.suffix == ".npz":
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError):
            raise ValueError("model_weights_container_unreadable") from None
        if not names:
            raise ValueError("model_weights_header_empty")
        return "npz"
    raise ValueError("model_weights_container_unsupported")


class WhisperAsrPlugin(ProcessorPlugin):
    def __init__(self, artifact_digest: str = "", **kwargs):
        super().__init__(max_concurrency=1, max_batch_size=1, **kwargs)
        # artifact_digest 是插件包内容的规范摘要，由 tools/plugin_artifact.py 生成并校验，
        # 不是占位符：没有它就拒绝启动（见 ADR-012）。
        self.artifact_digest = artifact_digest
        self.config = AsrConfig()
        self.model: ModelIdentity | None = None
        self.started = False
        self._backend = None
        self._dimension_fields: set[str] = set()

    # --- 生命周期 -------------------------------------------------------------
    def describe(self) -> runtime.PluginDescription:
        return describe()

    def configure(self, config: dict) -> None:
        if not self.artifact_digest:
            raise ValueError("artifact_digest_required")
        parsed = AsrConfig.from_mapping(dict(config))
        if not parsed.handoff_endpoint:
            raise ValueError("handoff_endpoint_required")
        self._load_backend()
        self.model = self._probe_model(parsed)
        self.config = parsed
        self.started = True

    def _load_backend(self) -> None:
        """后端在 Start 时就真实导入：没装 mlx-whisper 不是等到推理才失败。"""
        try:
            import mlx_whisper
            from mlx_whisper.whisper import ModelDimensions
        except ImportError:
            raise ValueError("backend_not_available:mlx-whisper") from None
        self._backend = mlx_whisper
        self._dimension_fields = {item.name for item in dataclasses.fields(ModelDimensions)}

    def _probe_model(self, config: AsrConfig) -> ModelIdentity:
        """解析权重、读一次容器头、算真实摘要；任何一步失败都拒绝启动。"""
        directory, source = self._resolve_directory(config)
        weights = next(
            (directory / name for name in WEIGHT_FILES if (directory / name).is_file()), None
        )
        if weights is None:
            raise ValueError("model_artifact_digest_unavailable")
        container = probe_weight_container(weights)
        self._check_model_config(directory)
        digest = sha256_file(weights)
        name = config.model or directory.name
        model_id, _, tag = name.partition(":")
        revision = self._revision(directory, config, source)
        return ModelIdentity(
            model_id=model_id,
            model_version=tag or revision,
            release_id=f"mlx-whisper:{name}@{digest.removeprefix('sha256:')[:12]}",
            artifact_digest=digest,
            backend=self._mlx_backend(),
            weights_file=weights.name,
            weights_container=container,
            source=source,
            directory=str(directory),
        )

    def _resolve_directory(self, config: AsrConfig) -> tuple[pathlib.Path, str]:
        if config.model_dir:
            directory = pathlib.Path(config.model_dir).expanduser()
            if not directory.is_dir():
                # 原因串里不带路径：路径是运营配置，不该出现在控制面字符串里。
                raise ValueError("model_dir_not_found")
            return directory, "local_dir"
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            raise ValueError("backend_not_available:huggingface_hub") from None
        try:
            resolved = snapshot_download(
                repo_id=config.model, revision=config.model_revision or None
            )
        except (OSError, ValueError, RuntimeError):
            raise ValueError("model_weights_unreachable") from None
        return pathlib.Path(resolved), "hub"

    def _check_model_config(self, directory: pathlib.Path) -> None:
        """`config.json` 必须能被 `ModelDimensions(**config)` 接受：多一个键少一个键都装不起来。"""
        try:
            raw = json.loads((directory / "config.json").read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("model_config_unreadable") from None
        if not isinstance(raw, dict):
            raise ValueError("model_config_unreadable")
        remaining = set(raw) - set(MODEL_CONFIG_KEYS_IGNORED)
        if remaining != self._dimension_fields:
            raise ValueError("model_config_not_loadable")

    @staticmethod
    def _revision(directory: pathlib.Path, config: AsrConfig, source: str) -> str:
        if source == "local_dir":
            return config.model_revision or "local"
        # HF 缓存的快照目录名就是 revision（commit sha）。
        candidate = directory.name
        if candidate and candidate not in {"snapshots", "hub"}:
            return candidate
        return config.model_revision or "unknown"

    @staticmethod
    def _mlx_backend() -> str:
        try:
            import mlx.core as mx
        except ImportError:  # pragma: no cover - mlx 与 mlx-whisper 同装
            return "mlx-unknown"
        return f"mlx-{getattr(mx, '__version__', 'unknown')}"

    def close(self) -> None:
        if self.buffer_reader is not None:
            self.buffer_reader.close()
            self.buffer_reader = None

    # --- 处理 -----------------------------------------------------------------
    async def process(self, request, cancel_token):
        if not self.started or self.model is None:
            raise PluginError(common.UNSUPPORTED_CAPABILITY, "plugin_not_started")
        observations = []
        for item in request.inputs:
            descriptor = item.buffer
            if descriptor.kind != INPUT_KIND:
                raise PluginError(common.INVALID_INPUT, f"unsupported_input_kind:{descriptor.kind}")
            cancel_token.raise_if_cancelled()
            read = await asyncio.to_thread(self._read_segment, descriptor)
            cancel_token.raise_if_cancelled()
            transcript = await asyncio.to_thread(self._transcribe, read)
            observations.append(self._observation(request.context, read, transcript))
        return runtime.ProcessResponse(observations=observations)

    def _read_segment(self, descriptor):
        """按 lease 读取这一段音频；任何失败都翻译成契约错误码，不吞掉、不降级。"""
        try:
            return self.buffer_reader.read(descriptor.buffer_id)
        except BufferReadError as error:
            raise PluginError(error.code, error.reason_code, error.retryable) from None
        except grpc.RpcError as error:
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE,
                f"data_plane_rpc_failed:{error.code().name}",
                True,
            ) from None

    def _transcribe(self, read) -> dict:
        fmt = read.format
        try:
            waveform, stats = audio.prepare(
                read.payload, fmt.sample_format, fmt.sample_rate, fmt.channels
            )
        except audio.AudioError as error:
            raise PluginError(common.INVALID_INPUT, error.reason_code) from None
        started = time.monotonic()
        try:
            result = self._backend.transcribe(
                waveform,
                path_or_hf_repo=self.model.directory,
                language=self.config.language,
                task=self.config.task,
                word_timestamps=bool(self.config.word_timestamps),
                # verbose=None 是唯一安静的取值：既不打正文，也不打进度条。
                verbose=None,
            )
        except (OSError, RuntimeError, ValueError):
            # 后端内部细节（路径、张量形状）不进控制面；原因串是稳定的。
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE, "mlx_whisper_transcribe_failed", True
            ) from None
        text = str(result.get("text", "")).strip()
        if len(text) > MAX_TRANSCRIPT_CHARS:
            # 一段 30 秒以内的音频不可能合理地产生这么长的文本：当作模型侧失败上报，
            # 而不是静默截断成一个"看起来正常"的转写。
            raise PluginError(common.TRANSIENT_BACKEND_FAILURE, "transcript_exceeds_bound", True)
        segments = list(result.get("segments") or [])
        listed = segments[:MAX_LISTED_SEGMENTS]
        return {
            "text": text,
            "language": str(result.get("language") or ""),
            "segments": listed,
            "segments_omitted": max(0, len(segments) - len(listed)),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "input": stats,
        }

    # --- 输出 -----------------------------------------------------------------
    def _observation(self, ctx, read, transcript: dict) -> material.Observation:
        source_item_id = read.describe_source_item()
        seed = "|".join(
            (
                ctx.stream_id,
                ctx.source_id,
                source_item_id,
                MODALITY,
                PLUGIN_VERSION,
                self.model.release_id,
                self.config.config_hash(),
            )
        )
        observation_id = "obs_" + hashlib.sha256(seed.encode()).hexdigest()[:32]
        observation = material.Observation(
            observation_id=observation_id,
            modality=MODALITY,
            stream_id=ctx.stream_id,
            source_id=ctx.source_id,
            source_item_id=source_item_id,
            # 锚点就是这一段音频自己的半开区间，不按模型耗时或墙钟重算。
            time_range=read.time_range,
            confidence_unavailable_reason=CONFIDENCE_UNAVAILABLE_REASON,
            quality_state="final",
            content_hash=read.digest,
            timing_source="media_pts",
            created_at_unix_ms=int(time.time() * 1000),
            provenance=material.Provenance(
                plugin=PLUGIN_NAME,
                plugin_version=PLUGIN_VERSION,
                artifact_digest=self.artifact_digest,
                model_release_id=self.model.release_id,
                model_id=self.model.model_id,
                model_version=self.model.model_version,
                config_hash=self.config.config_hash(),
                execution_backend=self.model.backend,
                model_artifact_digest=self.model.artifact_digest,
            ),
        )
        observation.payload.update(self._payload(read, transcript))
        return observation

    def _payload(self, read, transcript: dict) -> dict:
        window_start_ms = int(read.time_range.start_ms)
        window_end_ms = int(read.time_range.end_ms)
        segments = [
            self._segment_payload(window_start_ms, window_end_ms, segment)
            for segment in transcript["segments"]
        ]
        payload = {
            "text": transcript["text"],
            "language": transcript["language"],
            "segments": segments,
            # 模型可以在幻觉时给出**越窗**的时间戳：实测 5 秒窗口上出现过 [940, 29880]。
            # 这是模型的输出事实，所以既不夹取也不丢弃——保留原值并显式计数，
            # 让消费者能据此丢掉这段的时序，而不是拿到一个看起来合法的区间。
            "segments_outside_window": sum(
                1 for segment in segments if segment["timing_outside_window"]
            ),
            "segments_omitted": transcript["segments_omitted"],
            "input": dict(transcript["input"], window_ms=window_end_ms - window_start_ms),
            "inference": {
                "model": self.config.model,
                "weights_file": self.model.weights_file,
                "weights_container": self.model.weights_container,
                "model_source": self.model.source,
                "task": self.config.task,
                "duration_ms": transcript["duration_ms"],
            },
            # 子段相对时间如何换算成时间轴时间，必须写在结果里，而不是留给读者猜。
            "segment_timing": "media_pts_window_relative_plus_window_start",
        }
        if not transcript["text"]:
            # 空转写是模型的真实结果，不是失败；但必须说清是哪一种空。
            payload["empty_transcript_reason"] = (
                "model_returned_no_segments"
                if not transcript["segments"]
                else "model_returned_empty_text"
            )
        return payload

    @staticmethod
    def _segment_payload(window_start_ms: int, window_end_ms: int, segment: dict) -> dict:
        """模型给的相对时间只用来切分子段；缺哪个字段就写 null，不填 0 冒充测得值。"""

        def optional(name: str):
            value = segment.get(name)
            return None if value is None else float(value)

        start_ms = window_start_ms + int(round(float(segment.get("start", 0.0)) * 1000))
        end_ms = window_start_ms + int(round(float(segment.get("end", 0.0)) * 1000))
        return {
            "start_ms": start_ms,
            "end_ms": end_ms,
            # 越窗不是"算错了"，是模型报告的时间可能超出它实际拿到的音频：标记而不是夹取。
            "timing_outside_window": start_ms < window_start_ms or end_ms > window_end_ms,
            "text": str(segment.get("text", "")).strip(),
            # 解码诊断：原样带出，明确不是校准置信度。
            "avg_logprob": optional("avg_logprob"),
            "no_speech_prob": optional("no_speech_prob"),
            "compression_ratio": optional("compression_ratio"),
            "temperature": optional("temperature"),
        }

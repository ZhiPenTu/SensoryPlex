"""BGE 文本向量插件：把上游观测里的真实文字编码成**版本化维度**的归一化向量。

它消费的不是字节，而是**上游已经产出的事实**：`observation.ocr_blocks` 里那些带坐标的文字块。
因此本插件不接数据面、不读文件、也不自己再去识别一遍——文字从哪来、覆盖哪个时间窗，
都由上游观测决定并被原样带出。这也意味着一个显式约束：

**给本插件喂 buffer（原始帧）是错误用法，会以 `buffer_reader_not_attached` 明确拒绝**，
不会退化成"自己找文件读"。挂载数据面的插件才允许碰字节（见 ADR-010）。

刻意保持的语义（与 ADR-012 / ADR-014 / ADR-016 同一条规则）：

- 模型身份来自**实际被加载的三个文件**的字节摘要（编码器权重 + 分词器 + 模型结构配置），
  不是配置里写的版本号；第三方可以用同样的文件独立复算；
- **维度是身份的一部分**：`dimension` 取自 `config.json` 的 `hidden_size`，并在 Start 时用一次
  真实前向**实测**输出维度与它对齐；对不上就拒绝启动（ADR-017 的"同一 embedding 维度须版本化"）；
- 池化与归一化方式写进结果（BGE 的标准用法是 CLS 池化 + L2 归一化），不让读者猜；
- `content_hash` 是**实际被编码的那段文本**的摘要（可被任何持有该文本的人复算），
  上游观测的身份另写 `input.*`，两者不混为一谈；
- 相似度**不是校准置信度**，因此 `confidence` 缺省并写明确原因；
- 时间锚点继承上游观测的半开区间，本插件不重新计时；
- 分词器/权重/后端任一不可用都在 Start 失败，不静默退回别的模型或 CPU（见 ADR-016）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import time
from dataclasses import dataclass

import numpy as np
from edge_material_sdk import PluginError, ProcessorPlugin
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime

from . import models
from . import text as text_module

PLUGIN_NAME = "org.sensoryplex.embed-bge-onnx"
PLUGIN_VERSION = "0.1.0"
MODALITY = "text_embedding"
# 能力串是契约里的名字：消费上游的 ocr_blocks 事实，产出 text_embedding 事实。
CONSUMES = "observation.ocr_blocks"
PRODUCES = "observation.text_embedding"
POOLING = "cls"
NORMALIZE = "l2"
CONFIDENCE_UNAVAILABLE_REASON = "embedding_similarity_is_not_calibrated_confidence"
DIMENSION_SOURCE = "config.json:hidden_size+probe_forward"
# 规模上限：超过就不是"一个观测的文字"，显式失败而不是截断。
MAX_DIMENSION = 4096
# 本切片交付的模型（bge-small-zh-v1.5）位置编码上限是 512；config.schema.json 同口径。
MAX_LENGTH = 512
PROVIDERS = ("cpu", "coreml")
PROVIDER_EP = {"cpu": "CPUExecutionProvider", "coreml": "CoreMLExecutionProvider"}
# Start 时的真实前向探针：它证明"会话能跑"，并实测出向量维度。
PROBE_TEXT = "端侧向量探针 probe"
MIN_MAX_LENGTH = 8

CONFIG_KEYS = (
    "model_dir",
    "model_file",
    "model_id",
    "model_revision",
    "provider",
    "max_length",
    "ttl_ms",
    "timeout_s",
)
DEFAULT_MODEL_ID = "bge-small-zh-v1.5"


@dataclass
class EmbedConfig:
    model_dir: str = ""
    model_file: str = models.DEFAULT_MODEL_FILE
    model_id: str = DEFAULT_MODEL_ID
    model_revision: str = ""
    provider: str = "cpu"
    max_length: int = 512
    ttl_ms: int = 30_000
    timeout_s: float = 120.0

    @classmethod
    def from_mapping(cls, config: dict) -> EmbedConfig:
        unknown = sorted(set(config) - set(CONFIG_KEYS))
        if unknown:
            raise ValueError(f"unknown_config_keys:{','.join(unknown)}")
        return cls(**{key: config[key] for key in CONFIG_KEYS if key in config})

    def effective(self) -> dict:
        """语义配置：`model_dir` 是位置（身份由文件摘要承担），维度上限与池化写进 config_hash。"""
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_file": self.model_file,
            "max_length": int(self.max_length),
            "pooling": POOLING,
            "normalize": NORMALIZE,
            "timeout_s": float(self.timeout_s),
            "ttl_ms": int(self.ttl_ms),
        }

    def config_hash(self) -> str:
        blob = json.dumps(self.effective(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def describe() -> runtime.PluginDescription:
    return runtime.PluginDescription(
        name=PLUGIN_NAME,
        version=PLUGIN_VERSION,
        protocol="v1",
        consumes=[CONSUMES],
        produces=[PRODUCES],
        # 本插件不消费任何 buffer：它要的是上游事实，不是字节。空列表就是它的真实能力。
        memory_kinds=[],
    )


def validate_config(config: dict) -> runtime.ValidationResult:
    errors: list[str] = []
    try:
        parsed = EmbedConfig.from_mapping(dict(config))
    except (ValueError, TypeError) as error:
        return runtime.ValidationResult(valid=False, field_errors=[str(error)])
    if not parsed.model_dir:
        errors.append("model_dir_required")
    if not parsed.model_file:
        errors.append("model_file_required")
    if parsed.provider not in PROVIDERS:
        errors.append(f"unsupported_provider:{parsed.provider}")
    if not MIN_MAX_LENGTH <= int(parsed.max_length) <= MAX_LENGTH:
        errors.append("max_length_out_of_range")
    if float(parsed.timeout_s) <= 0:
        errors.append("timeout_s_must_be_positive")
    return runtime.ValidationResult(valid=not errors, field_errors=errors)


def load_tokenizer(path: pathlib.Path):
    """真实加载分词器：缺 `tokenizers` 或文件不是合法分词器都在这里失败。"""
    try:
        import tokenizers
    except ImportError:  # pragma: no cover - 依赖装了就一定在
        raise ValueError("backend_not_available:tokenizers") from None
    try:
        return tokenizers.Tokenizer.from_file(str(path))
    except Exception:  # noqa: BLE001 - 库内部细节不进控制面，原因串保持稳定
        raise ValueError("tokenizer_load_failed") from None


def create_session(path: pathlib.Path, provider: str):
    """真实创建推理会话：请求 CoreML 就只有 CoreML 会话能满足请求，我们不做静默降级。"""
    try:
        import onnxruntime
    except ImportError:  # pragma: no cover - 依赖装了就一定在
        raise ValueError("backend_not_available:onnxruntime") from None
    # 首选执行后端必须是请求的那个，CPU 只作为后备；请求 CPU 时不出重复项。
    providers = [PROVIDER_EP[provider]]
    if PROVIDER_EP[provider] != PROVIDER_EP["cpu"]:
        providers.append(PROVIDER_EP["cpu"])
    options = onnxruntime.SessionOptions()
    options.log_severity_level = 3
    try:
        return onnxruntime.InferenceSession(str(path), sess_options=options, providers=providers)
    except Exception:  # noqa: BLE001
        raise ValueError("onnx_session_init_failed") from None


@dataclass(frozen=True)
class Encoded:
    vector: np.ndarray
    token_count: int


class BgeEncoder:
    """编码器：分词 → 前向 → CLS 池化 → L2 归一化，全部对着实际会话做。"""

    def __init__(self, session, tokenizer, *, max_length: int, dimension: int):
        self.session = session
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.dimension = dimension
        tokenizer.enable_truncation(max_length=max_length)

    def encode(self, value: str) -> Encoded:
        encoding = self.tokenizer.encode(value)
        ids = np.asarray([encoding.ids], dtype=np.int64)
        feed = self._feed(ids)
        outputs = self.session.run(None, feed)
        sequence = self._sequence_output(outputs)
        # BGE 的标准用法：CLS 池化（取序列第一位），再 L2 归一化。
        vector = np.asarray(sequence[0, 0, :], dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm <= 0:
            # 零向量或 NaN 不是"相似度为 0 的结果"，是编码失败：显式拒绝。
            raise ValueError("embedding_norm_unusable")
        return Encoded(vector=(vector / norm).astype(np.float32), token_count=int(ids.shape[1]))

    def _feed(self, ids: np.ndarray) -> dict[str, np.ndarray]:
        ones = np.ones_like(ids)
        zeros = np.zeros_like(ids)
        feed: dict[str, np.ndarray] = {}
        for entry in self.session.get_inputs():
            name = entry.name
            if "mask" in name:
                feed[name] = ones
            elif "token_type" in name:
                feed[name] = zeros
            else:
                feed[name] = ids
        return feed

    def _sequence_output(self, outputs) -> np.ndarray:
        """挑出隐藏状态：只认"最后一维等于本模型维度"的那个张量，其余一律不猜。"""
        for output in outputs:
            candidate = np.asarray(output)
            if candidate.ndim == 3 and candidate.shape[-1] == self.dimension:
                return candidate
        raise ValueError("model_output_shape_unexpected")


class EmbedPlugin(ProcessorPlugin):
    def __init__(self, artifact_digest: str = "", **kwargs):
        super().__init__(max_concurrency=1, max_batch_size=8, **kwargs)
        self.artifact_digest = artifact_digest
        self.config = EmbedConfig()
        self.model: models.EmbedModelIdentity | None = None
        self.encoder: BgeEncoder | None = None
        self.started = False

    # --- 生命周期 -------------------------------------------------------------
    def describe(self) -> runtime.PluginDescription:
        return describe()

    def configure(self, config: dict) -> None:
        if not self.artifact_digest:
            raise ValueError("artifact_digest_required")
        parsed = EmbedConfig.from_mapping(dict(config))
        if not parsed.model_dir:
            # 权重不随包携带，也不在 Start 里偷偷下载：目录必须由运营显式提供。
            raise ValueError("model_dir_required")
        if parsed.provider not in PROVIDERS:
            raise ValueError(f"unsupported_provider:{parsed.provider}")
        directory = pathlib.Path(parsed.model_dir).expanduser()
        if not directory.is_dir():
            # 原因串里不带路径：路径是运营配置，不该出现在控制面字符串里。
            raise ValueError("model_dir_not_found")
        version = models.installed_version("onnxruntime")
        encoder, model_config = self._load_engine(directory, parsed)
        weights = [
            self._probe_weights_file(
                models.resolve_model_file(directory, parsed.model_file), "encoder"
            ),
            self._probe_weights_file(directory / models.TOKENIZER_FILE, "tokenizer"),
            self._probe_weights_file(directory / models.MODEL_CONFIG_FILE, "model_config"),
        ]
        session = encoder.session
        providers = list(session.get_providers())
        backend = self._backend_string(session, parsed.provider, version)
        measured = self._probe_dimension(encoder, model_config["dimension"])
        self.encoder = encoder
        self.model = models.build_identity(
            model_id=parsed.model_id,
            revision=parsed.model_revision or "local_dir",
            source="local_dir",
            directory=directory,
            weights=weights,
            providers=providers,
            backend=backend,
            runtime_version=version,
            dimension=measured,
            max_length=int(parsed.max_length),
        )
        self.config = parsed
        self.started = True

    @staticmethod
    def _load_engine(directory: pathlib.Path, config: EmbedConfig):
        """按真实文件建引擎：模型容器、分词器、会话、维度上限都在这里被验证。"""
        model_path = models.resolve_model_file(directory, config.model_file)
        if not model_path.is_file():
            raise ValueError("model_file_not_found")
        models.probe_onnx_container(model_path)
        model_config = models.read_model_config(directory / models.MODEL_CONFIG_FILE)
        if int(config.max_length) > int(model_config["max_position_embeddings"]):
            # 截断上限超过模型的位置编码范围：这是配置与权重不符，拒绝启动而不是静默夹取。
            raise ValueError(
                f"max_length_exceeds_model_limit:{model_config['max_position_embeddings']}"
            )
        tokenizer_path = directory / models.TOKENIZER_FILE
        if not tokenizer_path.is_file():
            raise ValueError("tokenizer_file_not_found")
        tokenizer = load_tokenizer(tokenizer_path)
        session = create_session(model_path, config.provider)
        dimension = int(model_config["dimension"])
        if dimension > MAX_DIMENSION:
            raise ValueError("model_dimension_exceeds_bound")
        encoder = BgeEncoder(
            session, tokenizer, max_length=int(config.max_length), dimension=dimension
        )
        return encoder, model_config

    @staticmethod
    def _probe_weights_file(path: pathlib.Path, role: str) -> models.WeightsFile:
        if not path.is_file():
            raise ValueError("model_artifact_digest_unavailable")
        size = path.stat().st_size
        if size == 0:
            raise ValueError("model_container_empty")
        if role == "encoder":
            models.probe_onnx_container(path)
        return models.WeightsFile(
            role=role, name=path.name, digest=models.sha256_file(path), size_bytes=size
        )

    @staticmethod
    def _backend_string(session, requested: str, runtime_version: str) -> str:
        # 请求哪个 provider 由调用方显式传入：不从可变状态反推（见 ADR-016 的缺陷记录）。
        providers = list(session.get_providers())
        requested_ep = PROVIDER_EP[requested]
        if not providers or providers[0] != requested_ep:
            actual = providers[0] if providers else "none"
            raise ValueError(f"execution_provider_not_selected:{actual}")
        return f"onnxruntime-{runtime_version}/{providers[0]}"

    @staticmethod
    def _probe_dimension(encoder: BgeEncoder, declared: int) -> int:
        """真实跑一次前向：会话能跑，且输出维度与 `config.json` 声明的一致。"""
        try:
            probe = encoder.encode(PROBE_TEXT)
        except Exception:  # noqa: BLE001 - 探针失败只暴露稳定的原因串
            raise ValueError("model_probe_forward_failed") from None
        measured = int(probe.vector.shape[0])
        if measured != declared:
            raise ValueError(f"model_dimension_mismatch:{declared}!={measured}")
        return measured

    def close(self) -> None:
        self.encoder = None
        self.started = False

    # --- 处理 -----------------------------------------------------------------
    async def process(self, request, cancel_token):
        if not self.started or self.model is None or self.encoder is None:
            raise PluginError(common.UNSUPPORTED_CAPABILITY, "plugin_not_started")
        observations = []
        for item in request.inputs:
            kind = item.WhichOneof("value")
            if kind != "observation":
                # 原始帧在这里被明确拒绝，而不是被"顺手"读掉。
                raise PluginError(
                    common.UNSUPPORTED_MEMORY_KIND, f"unsupported_input_kind:{kind or 'unset'}"
                )
            upstream = item.observation
            cancel_token.raise_if_cancelled()
            source = text_module.collect_text(upstream)
            encoded = await asyncio.to_thread(self._encode, source.text)
            cancel_token.raise_if_cancelled()
            observations.append(self._observation(request.context, upstream, source, encoded))
        return runtime.ProcessResponse(observations=observations)

    def _encode(self, value: str) -> tuple[Encoded, float]:
        started = time.monotonic()
        try:
            encoded = self.encoder.encode(value)
        except PluginError:
            raise
        except Exception:  # noqa: BLE001 - 会话/库内部细节不进控制面
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE, "embedding_inference_failed", True
            ) from None
        return encoded, round((time.monotonic() - started) * 1000, 3)

    # --- 输出 -----------------------------------------------------------------
    def _observation(
        self, ctx, upstream, source: text_module.TextSource, encoded
    ) -> material.Observation:
        key, duration_ms = encoded
        seed = "|".join(
            (
                ctx.stream_id,
                ctx.source_id,
                upstream.observation_id,
                MODALITY,
                PLUGIN_VERSION,
                self.model.release_id,
                self.config.config_hash(),
                source.text_sha256,
            )
        )
        observation_id = "obs_" + hashlib.sha256(seed.encode()).hexdigest()[:32]
        observation = material.Observation(
            observation_id=observation_id,
            modality=MODALITY,
            stream_id=ctx.stream_id,
            source_id=ctx.source_id,
            source_item_id=upstream.source_item_id,
            # 锚点继承上游观测的半开区间：本插件不重新计时，也不按推理耗时换算。
            time_range=upstream.time_range,
            confidence_unavailable_reason=CONFIDENCE_UNAVAILABLE_REASON,
            # 上游是部分结果时，由它派生的向量也是部分结果；状态照实继承，不美化。
            quality_state=upstream.quality_state,
            content_hash=source.text_sha256,
            timing_source=upstream.timing_source,
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
        observation.payload.update(
            self._payload(observation_id, upstream, source, key, duration_ms)
        )
        return observation

    def _payload(
        self,
        observation_id: str,
        upstream,
        source: text_module.TextSource,
        key: Encoded,
        duration_ms: float,
    ) -> dict:
        vector = [float(value) for value in key.vector]
        return {
            # 向量库侧的记录名：本切片没有向量库，`storage` 与 `vector_ref` 把这件事说清楚。
            "embedding_id": observation_id,
            "storage": "inline_payload",
            "vector_ref": None,
            "vector_index_key": self.vector_index_key(),
            "dimension": int(self.model.dimension),
            "dimension_source": DIMENSION_SOURCE,
            "pooling": POOLING,
            "normalize": NORMALIZE,
            "vector": vector,
            # 摘要按 float32 小端字节算，任何拿到这份 payload 的人都能独立复算。
            "vector_sha256": vector_digest(key.vector),
            "norm": float(np.linalg.norm(key.vector)),
            "token_count": int(key.token_count),
            **source.payload(),
            "input": {
                "observation_id": upstream.observation_id,
                "modality": upstream.modality,
                "content_hash": upstream.content_hash,
                "stream_id": upstream.stream_id,
                "source_id": upstream.source_id,
                "source_item_id": upstream.source_item_id,
                "quality_state": upstream.quality_state,
                "time_range": {
                    "start_ms": upstream.time_range.start_ms,
                    "end_ms": upstream.time_range.end_ms,
                },
                "model_release_id": upstream.provenance.model_release_id,
            },
            "engine": {
                "model_id": self.model.model_id,
                "model_revision": self.model.model_version,
                "model_source": self.model.source,
                "runtime_version": self.model.runtime_version,
                "weights": self.model.weights_payload(),
                "providers": self.model.providers,
                "max_length": self.model.max_length,
                "duration_ms": duration_ms,
            },
        }

    def vector_index_key(self) -> str:
        """向量库 collection 名：模型 + 维度 + 版本，由实测值派生（不写死维度）。"""
        slug = "".join(
            character if character.isalnum() else "_" for character in self.model.model_id
        ).strip("_")
        return f"material_text_{slug}_d{self.model.dimension}_v1"


def vector_digest(vector: np.ndarray) -> str:
    """向量摘要：float32 小端字节的 SHA-256，与 JSON 里的十进制表示无关。"""
    return "sha256:" + hashlib.sha256(np.asarray(vector, dtype="<f4").tobytes()).hexdigest()

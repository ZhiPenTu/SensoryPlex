"""VLM 场景描述插件：读 Runtime 数据面里的真实视频帧，产出带锚点与来源的 observation。

它消费的是**字节**，不是文件名：每一帧都通过 `LeaseBufferReader` 按 lease 读取、
校验窗口摘要后才送进模型。模型本身跑在本机 ollama 上（端侧推理，不出网）。

刻意保持的语义：
- 帧字节只出现在"送进模型"这一条路径上，不进日志、不进控制消息、不进返回的 payload；
- 模型不给校准置信度，因此 `confidence` 留空并写明确原因，绝不填一个看起来合理的数字；
- 时间锚点直接取 descriptor 的半开区间 `[start_ms, end_ms)`，不重新计时；
- 输出 ID 由稳定输入派生（stream/source/buffer/摘要/版本/配置），重放同一帧得到同一个 ID；
- 非视频输入显式拒绝，而不是被静默跳过。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import grpc
from edge_material_sdk import BufferReadError, PluginError, ProcessorPlugin
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime

from .png import encode_rgba

PLUGIN_NAME = "org.sensoryplex.vlm-moondream"
PLUGIN_VERSION = "0.1.0"
MODALITY = "vision.scene_description"
CONSUMES = "media.video_frame"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_MODEL = "moondream:v2"
DEFAULT_PROMPT = "Describe what is visible in this image in one sentence."
# 模型不提供校准置信度：这是模型的属性，不是缺失的数据，必须显式写出来。
CONFIDENCE_UNAVAILABLE_REASON = "model_does_not_report_calibrated_confidence"
DEFAULT_TIMEOUT_S = 180.0

CONFIG_KEYS = ("endpoint", "model", "prompt", "handoff_endpoint", "timeout_s", "ttl_ms")


@dataclass
class VlmConfig:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    prompt: str = DEFAULT_PROMPT
    handoff_endpoint: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    ttl_ms: int = 30_000

    @classmethod
    def from_mapping(cls, config: dict) -> VlmConfig:
        unknown = sorted(set(config) - set(CONFIG_KEYS))
        if unknown:
            raise ValueError(f"unknown_config_keys:{','.join(unknown)}")
        fields = {key: config[key] for key in CONFIG_KEYS if key in config}
        return cls(**{**{"endpoint": DEFAULT_ENDPOINT}, **fields})

    def effective(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "prompt": self.prompt,
            "timeout_s": float(self.timeout_s),
            "ttl_ms": int(self.ttl_ms),
        }

    def config_hash(self) -> str:
        """配置的规范摘要：同一次配置永远得到同一个值，且可被别人复算。"""
        blob = json.dumps(self.effective(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class ModelIdentity:
    """模型身份来自模型服务本身（真实摘要），不是我猜的版本号。"""

    model_id: str
    model_version: str
    release_id: str
    artifact_digest: str
    backend: str = field(default="ollama")


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
        parsed = VlmConfig.from_mapping(dict(config))
    except (ValueError, TypeError) as error:
        return runtime.ValidationResult(valid=False, field_errors=[str(error)])
    if not parsed.endpoint.startswith(("http://127.0.0.1", "http://localhost", "https://")):
        errors.append("endpoint_must_be_explicit")
    if not parsed.handoff_endpoint:
        errors.append("handoff_endpoint_required")
    if not 50 <= int(parsed.ttl_ms) <= 60_000:
        errors.append("ttl_ms_out_of_range")
    if parsed.timeout_s <= 0:
        errors.append("timeout_s_must_be_positive")
    if not parsed.prompt.strip() or not parsed.model.strip():
        errors.append("model_and_prompt_required")
    return runtime.ValidationResult(valid=not errors, field_errors=errors)


class VisionVlmPlugin(ProcessorPlugin):
    def __init__(self, artifact_digest: str = "", **kwargs):
        super().__init__(max_concurrency=1, max_batch_size=1, **kwargs)
        # artifact_digest 是插件包内容的规范摘要，由 tools/plugin_artifact.py 生成并校验，
        # 不是占位符：没有它就拒绝启动（见 ADR-012）。
        self.artifact_digest = artifact_digest
        self.config = VlmConfig()
        self.model: ModelIdentity | None = None
        self.started = False

    # --- 生命周期 -------------------------------------------------------------
    def describe(self) -> runtime.PluginDescription:
        return describe()

    def configure(self, config: dict) -> None:
        if not self.artifact_digest:
            raise ValueError("artifact_digest_required")
        parsed = VlmConfig.from_mapping(dict(config))
        if not parsed.handoff_endpoint:
            raise ValueError("handoff_endpoint_required")
        self.config = parsed
        self.model = self._probe_model(parsed)
        self.started = True

    def _probe_model(self, config: VlmConfig) -> ModelIdentity:
        """从模型服务读取真实身份；读不到就显式失败，不编造版本或摘要。"""
        payload = self._request_json("/api/tags", None, config.timeout_s)
        entry = next(
            (item for item in payload.get("models", []) if item.get("name") == config.model), None
        )
        if entry is None:
            raise ValueError(f"model_not_available:{config.model}")
        digest = str(entry.get("digest", ""))
        if len(digest) != 64:
            raise ValueError("model_artifact_digest_unavailable")
        name, _, tag = config.model.partition(":")
        return ModelIdentity(
            model_id=name,
            model_version=tag or "unversioned",
            release_id=f"ollama:{config.model}@{digest[:12]}",
            artifact_digest="sha256:" + digest,
            backend=f"ollama-{self._ollama_version(config.timeout_s)}",
        )

    def _ollama_version(self, timeout_s: float) -> str:
        try:
            payload = self._request_json("/api/version", None, timeout_s)
            return str(payload.get("version", "unknown"))
        except (PluginError, OSError, ValueError):
            return "unknown"

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
            if descriptor.kind != "video_frame":
                raise PluginError(common.INVALID_INPUT, f"unsupported_input_kind:{descriptor.kind}")
            cancel_token.raise_if_cancelled()
            read = await asyncio.to_thread(self._read_frame, descriptor)
            cancel_token.raise_if_cancelled()
            text, telemetry = await asyncio.to_thread(self._describe, read)
            observations.append(self._observation(request.context, read, text, telemetry))
        return runtime.ProcessResponse(observations=observations)

    def _read_frame(self, descriptor):
        """按 lease 读取这一帧；任何失败都翻译成契约错误码，不吞掉、不降级。"""
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

    def _describe(self, read) -> tuple[str, dict]:
        fmt = read.format
        if fmt.pixel_format != "RGBA":
            raise PluginError(
                common.UNSUPPORTED_MEMORY_KIND, f"unsupported_pixel_format:{fmt.pixel_format}"
            )
        stride = int(fmt.strides[0]) if fmt.strides else 0
        try:
            image = encode_rgba(fmt.width, fmt.height, read.payload, stride)
        except ValueError as error:
            raise PluginError(common.INVALID_INPUT, f"frame_encode_failed:{error}") from None
        payload = self._request_json(
            "/api/generate",
            {
                "model": self.config.model,
                "prompt": self.config.prompt,
                "images": [base64.b64encode(image).decode("ascii")],
                "stream": False,
            },
            self.config.timeout_s,
        )
        text = str(payload.get("response", "")).strip()
        if not text:
            # 空结果是模型侧的真实结果，按失败上报，不伪装成"没有可见内容"。
            raise PluginError(common.TRANSIENT_BACKEND_FAILURE, "empty_model_response", True)
        telemetry = {
            "model": self.config.model,
            "eval_count": int(payload.get("eval_count", 0)),
            "total_duration_ms": round(int(payload.get("total_duration", 0)) / 1e6, 3),
        }
        return text, telemetry

    def _request_json(self, path: str, body: dict | None, timeout_s: float) -> dict:
        """`body is None` 表示 GET。方法写错会得到 404，所以这里不做"两个都试"的兜底。"""
        request = urllib.request.Request(
            self.config.endpoint + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE, f"model_endpoint_http_{error.code}", True
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # 模型服务不可达：显式传播，不返回空描述。
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE, "model_endpoint_unreachable", True
            ) from None
        except json.JSONDecodeError:
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE, "model_response_not_json", True
            ) from None

    # --- 输出 -----------------------------------------------------------------
    def _observation(self, ctx, read, text: str, telemetry: dict) -> material.Observation:
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
        observation.payload.update(
            {
                "text": text,
                "frame": {"width": read.format.width, "height": read.format.height},
                "prompt": self.config.prompt,
                "inference": telemetry,
            }
        )
        return observation

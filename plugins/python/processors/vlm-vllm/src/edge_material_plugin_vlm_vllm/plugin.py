"""VLM 远程场景描述插件：通过兼容 OpenAI 的 API 远程调用 vLLM 视觉模型。

遵循与本地 VLM 插件相同的平台契约：
- 帧字节只出现在"送进模型"这一条路径上，不进日志、不进控制消息、不进返回的 payload；
- 远程模型通常不给出校准置信度，因此 `confidence` 留空并写明确原因；
- 时间锚点直接取 descriptor 的半开区间 `[start_ms, end_ms)`；
- 输出 ID 由稳定输入派生（stream/source/buffer/摘要/版本/配置），重放同一帧得到同一个 ID；
- 远程 API 存在并发限制，通过内部信号量严格限流并排队等待；遇到 429 转换为 retryable 错误。
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
from edge_material_sdk import BufferReadError, PluginError, ProcessorPlugin, read_descriptor
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime

from .png import encode_rgba

PLUGIN_NAME = "org.sensoryplex.vlm-vllm"
PLUGIN_VERSION = "0.1.0"
MODALITY = "vision.scene_description"
CONSUMES = "media.video_frame"
DEFAULT_PROMPT = "Describe what is visible in this image in one sentence."
CONFIDENCE_UNAVAILABLE_REASON = "model_does_not_report_calibrated_confidence"
DEFAULT_TIMEOUT_S = 180.0
MIN_LOCAL_DECODE_FREE_MEMORY_BYTES = 256 * 1024 * 1024

CONFIG_KEYS = (
    "base_url",
    "api_key",
    "max_concurrency",
    "model",
    "prompt",
    "data_plane_mode",
    "handoff_endpoint",
    "timeout_s",
    "ttl_ms",
    "min_free_memory_bytes",
)


def normalize_base_url(raw: str) -> str:
    """规范化 vLLM / OpenAI 兼容基准地址。

    兼容用户输入的多种常见形式：
    - http://192.168.1.100:8000 -> http://192.168.1.100:8000/v1
    - http://192.168.1.100:8000/v1 -> http://192.168.1.100:8000/v1
    - http://192.168.1.100:8000/v1/ -> http://192.168.1.100:8000/v1
    - http://192.168.1.100:8000/v1/chat/completions -> http://192.168.1.100:8000/v1
    """
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("base_url_must_start_with_http_or_https")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")].rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


@dataclass
class VllmConfig:
    base_url: str = ""
    api_key: str = ""
    max_concurrency: int = 2
    model: str = ""
    prompt: str = DEFAULT_PROMPT
    data_plane_mode: str = "static"
    handoff_endpoint: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    ttl_ms: int = 30_000
    min_free_memory_bytes: int = 0

    @classmethod
    def from_mapping(cls, config: dict) -> VllmConfig:
        unknown = sorted(set(config) - set(CONFIG_KEYS))
        if unknown:
            raise ValueError(f"unknown_config_keys:{','.join(unknown)}")
        fields = {key: config[key] for key in CONFIG_KEYS if key in config}
        if "base_url" in fields:
            fields["base_url"] = normalize_base_url(str(fields["base_url"]))
        if "max_concurrency" in fields:
            concurrency = fields["max_concurrency"]
            if isinstance(concurrency, float) and concurrency.is_integer():
                fields["max_concurrency"] = int(concurrency)
        memory = fields.get("min_free_memory_bytes")
        if isinstance(memory, float) and memory.is_integer():
            fields["min_free_memory_bytes"] = int(memory)
        return cls(**fields)

    def effective(self) -> dict:
        # api_key 以哈希摘要计入配置一致性判定，绝不明文暴露在 effective 字典中
        key_hash = (
            "sha256:" + hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()
            if self.api_key
            else ""
        )
        return {
            "base_url": self.base_url,
            "api_key_hash": key_hash,
            "max_concurrency": int(self.max_concurrency),
            "model": self.model,
            "prompt": self.prompt,
            "data_plane_mode": self.data_plane_mode,
            "timeout_s": float(self.timeout_s),
            "ttl_ms": int(self.ttl_ms),
        }

    def config_hash(self) -> str:
        """配置的规范摘要：同一次配置永远得到同一个值，且不泄露明文密钥。"""
        blob = json.dumps(self.effective(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class ModelIdentity:
    """模型身份来自远程服务真实返回（/v1/models），不是人工编造。"""

    model_id: str
    model_version: str
    release_id: str
    artifact_digest: str
    backend: str = field(default="vllm-remote-openai")


def describe(artifact_digest: str = "") -> runtime.PluginDescription:
    return runtime.PluginDescription(
        name=PLUGIN_NAME,
        version=PLUGIN_VERSION,
        protocol="v1",
        consumes=[CONSUMES],
        produces=[f"observation.{MODALITY}"],
        memory_kinds=["cpu_shared_memory"],
        artifact_digest=artifact_digest,
    )


def validate_config(config: dict) -> runtime.ValidationResult:
    errors: list[str] = []
    try:
        parsed = VllmConfig.from_mapping(dict(config))
    except (ValueError, TypeError) as error:
        return runtime.ValidationResult(valid=False, field_errors=[str(error)])
    if not parsed.base_url:
        errors.append("base_url_required")
    if not 1 <= int(parsed.max_concurrency) <= 64:
        errors.append("max_concurrency_out_of_range")
    if parsed.data_plane_mode not in {"static", "per_request", "local_decode"}:
        errors.append("invalid_data_plane_mode")
    if parsed.data_plane_mode == "static" and not parsed.handoff_endpoint:
        errors.append("handoff_endpoint_required")
    if parsed.data_plane_mode == "local_decode" and (
        isinstance(parsed.min_free_memory_bytes, bool)
        or not isinstance(parsed.min_free_memory_bytes, int)
        or parsed.min_free_memory_bytes < MIN_LOCAL_DECODE_FREE_MEMORY_BYTES
    ):
        errors.append("min_free_memory_bytes_required")
    if not 50 <= int(parsed.ttl_ms) <= 60_000:
        errors.append("ttl_ms_out_of_range")
    if parsed.timeout_s <= 0:
        errors.append("timeout_s_must_be_positive")
    if not parsed.prompt.strip():
        errors.append("prompt_required")
    return runtime.ValidationResult(valid=not errors, field_errors=errors)


class VisionVllmPlugin(ProcessorPlugin):
    def __init__(self, artifact_digest: str = "", **kwargs):
        super().__init__(max_concurrency=64, max_batch_size=1, **kwargs)
        self.artifact_digest = artifact_digest
        self.config = VllmConfig()
        self.model: ModelIdentity | None = None
        self.started = False
        self._semaphore = asyncio.Semaphore(2)

    # --- 生命周期 -------------------------------------------------------------
    def describe(self) -> runtime.PluginDescription:
        return describe(self.artifact_digest)

    def configure(self, config: dict) -> None:
        if not self.artifact_digest:
            raise ValueError("artifact_digest_required")
        parsed = VllmConfig.from_mapping(dict(config))
        if not parsed.base_url:
            raise ValueError("base_url_required")
        if not 1 <= int(parsed.max_concurrency) <= 64:
            raise ValueError("max_concurrency_out_of_range")
        if parsed.data_plane_mode not in {"static", "per_request", "local_decode"}:
            raise ValueError("invalid_data_plane_mode")
        if parsed.data_plane_mode == "static" and not parsed.handoff_endpoint:
            raise ValueError("handoff_endpoint_required")
        if parsed.data_plane_mode == "local_decode" and (
            isinstance(parsed.min_free_memory_bytes, bool)
            or not isinstance(parsed.min_free_memory_bytes, int)
            or parsed.min_free_memory_bytes < MIN_LOCAL_DECODE_FREE_MEMORY_BYTES
        ):
            raise ValueError("min_free_memory_bytes_required")
        self.config = parsed
        self._semaphore = asyncio.Semaphore(parsed.max_concurrency)
        self.model = self._probe_model(parsed)
        self.started = True

    def _probe_model(self, config: VllmConfig) -> ModelIdentity:
        """从远程 vLLM /v1/models 接口探测模型真实身份。

        当 config.model 为空时，自动探测远端加载的第一个模型；
        若显式提供了 model，则校验该模型存在于远端列表中。
        """
        payload = self._request_json("/models", None, config.timeout_s)
        models = payload.get("data", [])
        if not isinstance(models, list) or not models:
            raise ValueError("no_models_available_on_vllm")

        entry = None
        if config.model.strip():
            target = config.model.strip()
            entry = next((item for item in models if str(item.get("id")) == target), None)
            if entry is None:
                raise ValueError(f"model_not_available:{target}")
        else:
            entry = models[0]
            config.model = str(entry.get("id", ""))

        model_id = str(entry.get("id", "unknown_model"))
        created = str(entry.get("created", int(time.time())))
        artifact_digest = "sha256:" + hashlib.sha256(f"{model_id}@{created}".encode()).hexdigest()
        release_id = f"vllm:{model_id}@{created[:10]}"

        return ModelIdentity(
            model_id=model_id,
            model_version=str(entry.get("version", "vllm-openai")),
            release_id=release_id,
            artifact_digest=artifact_digest,
            backend="vllm-remote-openai",
        )

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
            text, telemetry = await self._describe(read)
            observations.append(self._observation(request.context, read, text, telemetry))
        return runtime.ProcessResponse(observations=observations)

    def _read_frame(self, descriptor):
        """按 lease 读取这一帧。"""
        try:
            return read_descriptor(
                self.buffer_reader,
                descriptor,
                ttl_ms=int(self.config.ttl_ms),
                timeout_s=float(self.config.timeout_s),
            )
        except BufferReadError as error:
            raise PluginError(error.code, error.reason_code, error.retryable) from None
        except grpc.RpcError as error:
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE,
                f"data_plane_rpc_failed:{error.code().name}",
                True,
            ) from None

    async def _describe(self, read) -> tuple[str, dict]:
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
        return await self._describe_png(image)

    async def _describe_png(self, image: bytes) -> tuple[str, dict]:
        """将 PNG 帧通过信号量并发门禁发送给远程 vLLM。"""
        async with self._semaphore:
            return await asyncio.to_thread(self._call_chat_completions, image)

    def _call_chat_completions(self, image: bytes) -> tuple[str, dict]:
        """同步发起 OpenAI Vision 格式调用。"""
        b64_image = base64.b64encode(image).decode("ascii")
        body = {
            "model": self.model.model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64_image}",
                            },
                        },
                        {
                            "type": "text",
                            "text": self.config.prompt,
                        },
                    ],
                }
            ],
            "max_tokens": 256,
            "temperature": 0.2,
        }
        start_t = time.perf_counter()
        payload = self._request_json("/chat/completions", body, self.config.timeout_s)
        duration_ms = round((time.perf_counter() - start_t) * 1000, 3)

        choices = payload.get("choices", [])
        if not choices:
            raise PluginError(common.TRANSIENT_BACKEND_FAILURE, "empty_model_response", True)

        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            text = "".join(parts).strip()
        else:
            text = str(content).strip()

        if not text:
            raise PluginError(common.TRANSIENT_BACKEND_FAILURE, "empty_model_response", True)

        usage = payload.get("usage", {})
        telemetry = {
            "model": self.model.model_id,
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
            "total_duration_ms": duration_ms,
        }
        return text, telemetry

    def describe_decoded_anchor(
        self,
        *,
        stream_id: str,
        source_id: str,
        source_item_id: str,
        start_ms: int,
        end_ms: int,
        png: bytes,
        task_config_hash: str,
    ) -> material.Observation:
        """为 WorkQueue / 慢路径构造 Observation。"""
        if not self.started or self.model is None:
            raise PluginError(common.UNSUPPORTED_CAPABILITY, "plugin_not_started")
        if start_ms < 0 or end_ms <= start_ms or not png:
            raise PluginError(common.INVALID_INPUT, "on_demand_anchor_invalid")

        text, telemetry = self._call_chat_completions(png)
        image_digest = "sha256:" + hashlib.sha256(png).hexdigest()
        seed = "|".join(
            (
                stream_id,
                source_id,
                source_item_id,
                MODALITY,
                PLUGIN_VERSION,
                self.model.release_id,
                task_config_hash,
            )
        )
        observation = material.Observation(
            observation_id="obs_" + hashlib.sha256(seed.encode()).hexdigest()[:32],
            modality=MODALITY,
            stream_id=stream_id,
            source_id=source_id,
            source_item_id=source_item_id,
            time_range=common.TimeRange(start_ms=start_ms, end_ms=end_ms),
            confidence_unavailable_reason=CONFIDENCE_UNAVAILABLE_REASON,
            quality_state="final",
            content_hash=image_digest,
            timing_source="media_pts",
            created_at_unix_ms=int(time.time() * 1000),
            provenance=material.Provenance(
                plugin=PLUGIN_NAME,
                plugin_version=PLUGIN_VERSION,
                artifact_digest=self.artifact_digest,
                model_release_id=self.model.release_id,
                model_id=self.model.model_id,
                model_version=self.model.model_version,
                config_hash=task_config_hash,
                execution_backend=self.model.backend,
                model_artifact_digest=self.model.artifact_digest,
            ),
        )
        observation.payload.update(
            {
                "text": text,
                "frame": {"decoded_on_demand": True},
                "prompt": self.config.prompt,
                "inference": telemetry,
            }
        )
        return observation

    def _request_json(self, path: str, body: dict | None, timeout_s: float) -> dict:
        url = self.config.base_url + path
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        request = urllib.request.Request(
            url,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise PluginError(
                    common.RESOURCE_EXHAUSTED, "remote_api_rate_limited", retryable=True
                ) from None
            if error.code == 503:
                raise PluginError(
                    common.TRANSIENT_BACKEND_FAILURE,
                    "remote_api_service_unavailable",
                    retryable=True,
                ) from None
            if error.code in (401, 403):
                raise PluginError(
                    common.PERMISSION_DENIED, "remote_api_authentication_failed", retryable=False
                ) from None
            if error.code in (400, 422):
                raise PluginError(
                    common.INVALID_INPUT,
                    f"remote_api_invalid_request_{error.code}",
                    retryable=False,
                ) from None
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE, f"model_endpoint_http_{error.code}", True
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
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

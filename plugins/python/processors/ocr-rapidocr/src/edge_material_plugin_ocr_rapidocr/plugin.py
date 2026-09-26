"""OCR 插件：读 Runtime 数据面里的真实视频帧，用本机 PP-OCR 权重产出带坐标与来源的文字块。

它消费的是**字节**，不是文件名：每帧都通过 `LeaseBufferReader` 按 lease 读取、校验窗口摘要后
才送进模型。权重来自本机（随包携带的模型文件或显式目录），推理在本机执行，媒体不出网。

刻意保持的语义（与 ADR-012 / ADR-014 同一条规则）：

- 帧字节只出现在"送进模型"这一条路径上：不进日志、不进控制消息、不进返回的 payload；
- 模型身份来自**实际被加载的那几个 ONNX 文件**的字节摘要（检测/方向/识别三份都要），
  不是配置里写的版本号；第三方可以用同样的文件独立复算；
- 检测与识别的分数是模型自己的输出，**不是校准置信度**，因此 `confidence` 留空并写明确原因，
  分数原样放在每个块里，不被当作置信度使用；
- 时间锚点直接取 descriptor 的半开区间 `[start_ms, end_ms)`，不重新计时；
- 坐标写在**帧像素坐标系**（左上原点）里，并同时给出归一化坐标——坐标含义写在结果里，
  而不是留给读者猜；
- "模型没有找到文字"与"处理失败"必须能分开：前者是 `blocks=[]` + `empty_reason`，后者是错误码；
- 请求了 CoreML 却拿不到 CoreML 会话时**显式失败**，不静默退回 CPU（见 ADR-016）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import time
from dataclasses import dataclass

import grpc
import numpy as np
from edge_material_sdk import BufferReadError, PluginError, ProcessorPlugin, read_descriptor
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as runtime

from . import models

PLUGIN_NAME = "org.sensoryplex.ocr-rapidocr"
PLUGIN_VERSION = "0.1.1"
MODALITY = "ocr_blocks"
CONSUMES = "media.video_frame"
INPUT_KIND = "video_frame"
PIXEL_FORMAT = "RGBA"
CONFIDENCE_UNAVAILABLE_REASON = "detector_and_recognizer_scores_are_not_calibrated_confidence"
# 坐标含义必须显式写出：像素是帧的左上原点坐标系，不是显示坐标系，也不是源编码坐标系。
COORDINATE_SPACE = "frame_pixels_top_left"
# 输出有上限：单帧不可能合理地产生更多文字块，单块也不可能更长（越界即失败，不静默截断）。
MAX_BLOCKS = 512
MAX_BLOCK_CHARS = 512
PROVIDERS = ("cpu", "coreml")
PROVIDER_EP = {"cpu": "CPUExecutionProvider", "coreml": "CoreMLExecutionProvider"}
# 三个角色必须都有会话：少一个就说明引擎没有完整加载组合模型。
SESSION_ROLES = (("text_det", "det"), ("text_cls", "cls"), ("text_rec", "rec"))

CONFIG_KEYS = (
    "data_plane_mode",
    "handoff_endpoint",
    "provider",
    "model_dir",
    "model_id",
    "model_revision",
    "text_score",
    "ttl_ms",
    "timeout_s",
)
DEFAULT_MODEL_ID = "PP-OCRv6_mobile"


@dataclass
class OcrConfig:
    data_plane_mode: str = "static"
    handoff_endpoint: str = ""
    provider: str = "cpu"
    model_dir: str = ""
    model_id: str = DEFAULT_MODEL_ID
    model_revision: str = ""
    text_score: float = 0.5
    ttl_ms: int = 30_000
    timeout_s: float = 120.0

    @classmethod
    def from_mapping(cls, config: dict) -> OcrConfig:
        unknown = sorted(set(config) - set(CONFIG_KEYS))
        if unknown:
            raise ValueError(f"unknown_config_keys:{','.join(unknown)}")
        return cls(**{key: config[key] for key in CONFIG_KEYS if key in config})

    def effective(self) -> dict:
        """语义配置：`model_dir` 是位置（身份由权重摘要承担），`handoff_endpoint` 是传输位置。"""
        return {
            "data_plane_mode": self.data_plane_mode,
            "provider": self.provider,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "text_score": float(self.text_score),
            "timeout_s": float(self.timeout_s),
            "ttl_ms": int(self.ttl_ms),
        }

    def config_hash(self) -> str:
        blob = json.dumps(self.effective(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


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
        parsed = OcrConfig.from_mapping(dict(config))
    except (ValueError, TypeError) as error:
        return runtime.ValidationResult(valid=False, field_errors=[str(error)])
    if parsed.data_plane_mode not in {"static", "per_request"}:
        errors.append("invalid_data_plane_mode")
    if parsed.data_plane_mode == "static" and not parsed.handoff_endpoint:
        errors.append("handoff_endpoint_required")
    if parsed.provider not in PROVIDERS:
        errors.append(f"unsupported_provider:{parsed.provider}")
    if not 0.0 < float(parsed.text_score) <= 1.0:
        errors.append("text_score_out_of_range")
    if not 50 <= int(parsed.ttl_ms) <= 60_000:
        errors.append("ttl_ms_out_of_range")
    if parsed.timeout_s <= 0:
        errors.append("timeout_s_must_be_positive")
    return runtime.ValidationResult(valid=not errors, field_errors=errors)


class OcrPlugin(ProcessorPlugin):
    def __init__(self, artifact_digest: str = "", **kwargs):
        super().__init__(max_concurrency=1, max_batch_size=4, **kwargs)
        self.artifact_digest = artifact_digest
        self.config = OcrConfig()
        self.model: models.OcrModelIdentity | None = None
        self.started = False
        self._engine = None
        self._rapidocr_version = "unknown"

    # --- 生命周期 -------------------------------------------------------------
    def describe(self) -> runtime.PluginDescription:
        return describe(self.artifact_digest)

    def configure(self, config: dict) -> None:
        if not self.artifact_digest:
            raise ValueError("artifact_digest_required")
        parsed = OcrConfig.from_mapping(dict(config))
        if parsed.data_plane_mode not in {"static", "per_request"}:
            raise ValueError("invalid_data_plane_mode")
        if parsed.data_plane_mode == "static" and not parsed.handoff_endpoint:
            raise ValueError("handoff_endpoint_required")
        if parsed.provider not in PROVIDERS:
            raise ValueError(f"unsupported_provider:{parsed.provider}")
        self._engine = self._build_engine(parsed)
        self.model = self._probe_model(parsed)
        self.config = parsed
        self.started = True

    def _build_engine(self, config: OcrConfig):
        """后端在 Start 时就真实构造会话：缺 onnxruntime、模型装不起来都在这里失败。"""
        try:
            # 只探测这一条 import：包在但入口不在也算"后端不可用"，不静默降级。
            from rapidocr import RapidOCR
        except ImportError:
            raise ValueError("backend_not_available:rapidocr") from None
        params: dict = {
            "Global.text_score": float(config.text_score),
            "Global.log_level": "error",
        }
        if config.model_dir:
            directory = pathlib.Path(config.model_dir).expanduser()
            if not directory.is_dir():
                # 原因串里不带路径：路径是运营配置，不该出现在控制面字符串里。
                raise ValueError("model_dir_not_found")
            params["Global.model_root_dir"] = str(directory)
        if config.provider == "coreml":
            params["EngineConfig.onnxruntime.use_coreml"] = True
        try:
            engine = RapidOCR(params=params)
        except Exception:  # noqa: BLE001 - 引擎内部细节不进控制面，原因串是稳定的
            raise ValueError("ocr_engine_init_failed") from None
        self._rapidocr_version = models.installed_version("rapidocr")
        return engine

    def _session_summary(
        self, requested: str
    ) -> tuple[dict[str, list[str]], list[models.WeightsFile], str, pathlib.Path]:
        """读一次真实会话：每个角色的 provider 与**实际加载的权重文件路径**。"""
        providers: dict[str, list[str]] = {}
        weights: list[models.WeightsFile] = []
        directory: pathlib.Path | None = None
        for attribute, role in SESSION_ROLES:
            holder = getattr(self._engine, attribute, None)
            inner = getattr(holder, "session", None)
            session = getattr(inner, "session", None)
            if session is None:
                raise ValueError(f"ocr_session_missing:{role}")
            providers[role] = list(session.get_providers())
            path = getattr(session, "_model_path", None)
            if not path:
                # 读不到真实路径就没有真实身份：拒绝启动，而不是"猜一个名字"。
                raise ValueError("model_artifact_digest_unavailable")
            weights.append(self._probe_weights_file(pathlib.Path(path), role))
            if role == "det":
                directory = pathlib.Path(path).parent
        if directory is None:  # pragma: no cover - det 必在 SESSION_ROLES 中
            raise ValueError("model_artifact_digest_unavailable")
        return providers, weights, self._backend_string(providers, requested), directory

    @staticmethod
    def _probe_weights_file(path: pathlib.Path, role: str) -> models.WeightsFile:
        if not path.is_file():
            raise ValueError("model_artifact_digest_unavailable")
        container = models.probe_onnx_container(path)
        return models.WeightsFile(
            role=role,
            name=f"{path.name}({container})",
            digest=models.sha256_file(path),
            size_bytes=path.stat().st_size,
        )

    def _backend_string(self, providers: dict[str, list[str]], requested: str) -> str:
        # 请求哪个 provider 由调用方显式传入：`self.config` 要到 Start 末尾才落值，
        # 从它反推会把"请求了 CoreML"悄悄退化成"默认 CPU 通过"。
        requested_ep = PROVIDER_EP[requested]
        role_primary = [item[0] for item in providers.values() if item]
        mismatched = [
            role for role, item in providers.items() if not item or item[0] != requested_ep
        ]
        if mismatched:
            # 请求了 CoreML 却拿到别的 provider：这是失败，不是"降级可用"。
            actual = ",".join(f"{role}:{(providers[role] or ['none'])[0]}" for role in mismatched)
            raise ValueError(f"execution_provider_not_selected:{actual}")
        if len(set(role_primary)) == 1:
            return f"onnxruntime-{self._onnxruntime_version()}/{role_primary[0]}"
        return f"onnxruntime-{self._onnxruntime_version()}/mixed:{','.join(role_primary)}"

    @staticmethod
    def _onnxruntime_version() -> str:
        return models.installed_version("onnxruntime")

    def _probe_model(self, config: OcrConfig) -> models.OcrModelIdentity:
        providers, weights, backend, loaded_directory = self._session_summary(config.provider)
        # 目录只作说明用：身份由上面那几个**实际加载的文件**的摘要承担。
        directory = (
            pathlib.Path(config.model_dir).expanduser() if config.model_dir else loaded_directory
        )
        source = "local_dir" if config.model_dir else "bundled"
        return models.build_identity(
            model_id=config.model_id,
            revision=config.model_revision or source,
            source=source,
            directory=directory,
            weights=weights,
            providers=providers,
            backend=backend,
            runtime_version=self._rapidocr_version,
        )

    def close(self) -> None:
        if self.buffer_reader is not None:
            self.buffer_reader.close()
            self.buffer_reader = None
        self._engine = None

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
            read = await asyncio.to_thread(self._read_frame, descriptor)
            image = self._to_bgr(read)
            cancel_token.raise_if_cancelled()
            result = await asyncio.to_thread(self._recognize, image)
            observations.append(self._observation(request.context, read, image, result))
        return runtime.ProcessResponse(observations=observations)

    def _read_frame(self, descriptor):
        """按 lease 读取这一帧；任何失败都翻译成契约错误码，不吞掉、不降级。"""
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

    @staticmethod
    def _to_bgr(read) -> np.ndarray:
        """RGBA8 → BGR（引擎的输入约定）。布局不对就显式拒绝，不猜 stride、不做填充。"""
        fmt = read.format
        if fmt.pixel_format != PIXEL_FORMAT:
            raise PluginError(
                common.UNSUPPORTED_MEMORY_KIND, f"unsupported_pixel_format:{fmt.pixel_format}"
            )
        width, height = int(fmt.width), int(fmt.height)
        if width <= 0 or height <= 0:
            raise PluginError(common.INVALID_INPUT, "frame_dimensions_unusable")
        stride = int(fmt.strides[0]) if fmt.strides else width * 4
        if stride < width * 4:
            raise PluginError(common.INVALID_INPUT, "frame_stride_smaller_than_row")
        needed = stride * (height - 1) + width * 4
        if len(read.payload) < needed:
            raise PluginError(common.INVALID_INPUT, "frame_payload_too_small")
        raw = np.frombuffer(read.payload[: stride * height], dtype=np.uint8).reshape(height, stride)
        rgba = raw[:, : width * 4].reshape(height, width, 4)
        # BGR：引擎按 OpenCV 约定收图；颜色通道顺序写错不会被任何断言发现，所以这里是唯一入口。
        return np.ascontiguousarray(rgba[:, :, 2::-1])

    def _recognize(self, image: np.ndarray) -> dict:
        started = time.monotonic()
        try:
            result = self._engine(image)
        except Exception:  # noqa: BLE001 - 引擎内部细节（张量形状、路径）不进控制面
            raise PluginError(
                common.TRANSIENT_BACKEND_FAILURE, "ocr_inference_failed", True
            ) from None
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        texts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        boxes = getattr(result, "boxes", None)
        count = 0 if texts is None else len(texts)
        listed = min(count, MAX_BLOCKS)
        blocks = []
        for index in range(listed):
            text = str(texts[index])
            if len(text) > MAX_BLOCK_CHARS:
                # 单个文字块长到这种程度不是"识别得多"，是模型侧异常：显式失败，不静默截断。
                raise PluginError(common.TRANSIENT_BACKEND_FAILURE, "ocr_block_exceeds_bound", True)
            blocks.append(
                {
                    "text": text,
                    "score": float(scores[index]) if scores is not None else None,
                    "box": np.asarray(boxes[index]).round(3).tolist()
                    if boxes is not None
                    else None,
                }
            )
        return {
            "blocks": blocks,
            "block_count": count,
            "blocks_omitted": max(0, count - listed),
            "duration_ms": duration_ms,
        }

    # --- 输出 -----------------------------------------------------------------
    def _observation(self, ctx, read, image: np.ndarray, result: dict) -> material.Observation:
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
            # 锚点就是这一帧自己的半开区间，不按模型耗时或墙钟重算。
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
        observation.payload.update(self._payload(read, image, result))
        return observation

    def _payload(self, read, image: np.ndarray, result: dict) -> dict:
        height, width = int(image.shape[0]), int(image.shape[1])
        blocks = [self._block_payload(width, height, block) for block in result["blocks"]]
        payload = {
            "blocks": blocks,
            "block_count": result["block_count"],
            "blocks_omitted": result["blocks_omitted"],
            "char_count": sum(len(block["text"]) for block in blocks),
            "image": {
                "width": width,
                "height": height,
                "pixel_format": PIXEL_FORMAT,
                "stride": int(read.format.strides[0]) if read.format.strides else width * 4,
            },
            "coordinate_space": COORDINATE_SPACE,
            "engine": {
                "model_id": self.model.model_id,
                "model_revision": self.model.model_version,
                "model_source": self.model.source,
                "runtime_version": self.model.runtime_version,
                "weights": self.model.weights_payload(),
                "providers": self.model.providers,
                "text_score": float(self.config.text_score),
                "duration_ms": result["duration_ms"],
            },
        }
        if not blocks:
            # 空结果是模型的真实结果（这一帧确实没有文字），不是失败；但必须说清是哪一种空。
            payload["empty_reason"] = "model_found_no_text"
        return payload

    @staticmethod
    def _block_payload(width: int, height: int, block: dict) -> dict:
        box = block.get("box")
        entry = {
            "text": block["text"],
            "chars": len(block["text"]),
            # 模型自己的检测/识别分数：原样带出，明确不是校准置信度。
            "score": block["score"],
            "box": box,
            "box_normalized": (
                [
                    [round(float(point[0]) / width, 6), round(float(point[1]) / height, 6)]
                    for point in box
                ]
                if box
                else None
            ),
        }
        return entry

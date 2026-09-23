"""进程内 worker 的构建块；进程隔离由运行时负责。

Plugins implement deterministic processing. Durable idempotency, lease allocation,
gRPC lifecycle serving and restart recovery are worker responsibilities.

输入有两条合法路径，且只有两条：
- `observation`：上游插件产出的事实单元，`invoke()` 直接校验契约；
- `buffer`：Runtime 数据面里的真实字节，插件**必须**通过注入的 `buffer_reader`
  （见 `edge_material_sdk.buffer_reader.LeaseBufferReader`）按 lease 读取。
  没有注入 reader 时 buffer 输入以 `buffer_reader_not_attached` 明确拒绝，
  不会退化成"读本地文件"或"跳过这条输入"。
"""

import asyncio
import time
from abc import ABC, abstractmethod

from .buffer_reader import CPU_SHARED_MEMORY
from .generated.common.v1 import common_pb2 as common
from .generated.runtime.v1.runtime_pb2 import ProcessRequest, ProcessResponse
from .validation import validate_digest, validate_observation, validate_range


class PluginError(Exception):
    def __init__(self, code: int, reason_code: str, retryable: bool = False):
        super().__init__(reason_code)
        self.code, self.reason_code, self.retryable = code, reason_code, retryable


class CancelToken:
    def __init__(self):
        self._event = asyncio.Event()

    def cancel(self):
        self._event.set()

    def raise_if_cancelled(self):
        if self._event.is_set():
            raise asyncio.CancelledError


class ProcessorPlugin(ABC):
    def __init__(self, max_concurrency: int = 1, max_batch_size: int = 8, buffer_reader=None):
        if max_concurrency < 1 or max_batch_size < 1:
            raise ValueError("resource_limits_must_be_positive")
        self.max_concurrency, self.max_batch_size = max_concurrency, max_batch_size
        # 只有注入了真实 reader 的插件才接受 buffer 输入。默认 None 时保持显式拒绝。
        self.buffer_reader = buffer_reader
        self._in_flight = 0

    @abstractmethod
    def describe(self): ...

    @abstractmethod
    async def process(
        self, request: ProcessRequest, cancel_token: CancelToken
    ) -> ProcessResponse: ...

    def _validate_buffer_input(self, descriptor, ctx) -> None:
        """buffer 输入的结构准入。字节本身由插件在 `process()` 里按 lease 读取。"""
        if self.buffer_reader is None:
            raise PluginError(common.UNSUPPORTED_MEMORY_KIND, "buffer_reader_not_attached")
        if descriptor.memory_kind != CPU_SHARED_MEMORY:
            raise PluginError(
                common.UNSUPPORTED_MEMORY_KIND,
                f"unsupported_memory_kind:{descriptor.memory_kind or 'unset'}",
            )
        if not descriptor.buffer_id or not descriptor.kind:
            raise PluginError(common.INVALID_INPUT, "incomplete_buffer_descriptor")
        if descriptor.stream_id != ctx.stream_id:
            raise PluginError(common.INVALID_INPUT, "input_context_mismatch")
        try:
            validate_digest(descriptor.content_hash)
            validate_range(descriptor.time_range)
        except ValueError:
            raise PluginError(common.INVALID_INPUT, "contract_validation_failed") from None

    async def invoke(self, request: ProcessRequest, token: CancelToken | None = None):
        token = token or CancelToken()
        ctx = request.context
        try:
            token.raise_if_cancelled()
            if not all(
                (
                    ctx.request_id,
                    ctx.trace_id,
                    ctx.pipeline_run_id,
                    ctx.stream_id,
                    ctx.source_id,
                    ctx.idempotency_key,
                    ctx.attempt,
                    request.processor_release_id,
                )
            ):
                raise PluginError(common.INVALID_INPUT, "missing_request_context")
            if ctx.privacy_policy.data_egress != "local_only":
                raise PluginError(common.DATA_POLICY_DENIED, "local_policy_required")
            remaining = ctx.deadline_unix_ms / 1000 - time.time()
            if remaining <= 0:
                raise PluginError(common.DEADLINE_EXCEEDED, "deadline_expired", True)
            if not 0 < len(request.inputs) <= self.max_batch_size:
                raise PluginError(common.INVALID_INPUT, "invalid_batch_size")
            for item in request.inputs:
                kind = item.WhichOneof("value")
                if kind == "observation":
                    validate_observation(item.observation)
                    if (item.observation.stream_id, item.observation.source_id) != (
                        ctx.stream_id,
                        ctx.source_id,
                    ):
                        raise PluginError(common.INVALID_INPUT, "input_context_mismatch")
                elif kind == "buffer":
                    self._validate_buffer_input(item.buffer, ctx)
                else:
                    raise PluginError(common.INVALID_INPUT, "empty_plugin_input")
            if self._in_flight >= self.max_concurrency:
                raise PluginError(common.RESOURCE_EXHAUSTED, "concurrency_limit", True)
            self._in_flight += 1
            try:
                async with asyncio.timeout(remaining):
                    response = await self.process(request, token)
                    token.raise_if_cancelled()
                    if not response.observations and not response.HasField("error"):
                        raise PluginError(common.INTERNAL_PLUGIN_ERROR, "empty_plugin_result")
                    if response.observations and response.HasField("error"):
                        raise PluginError(common.INTERNAL_PLUGIN_ERROR, "ambiguous_plugin_result")
                    for observation in response.observations:
                        try:
                            validate_observation(observation)
                        except ValueError:
                            raise PluginError(
                                common.INTERNAL_PLUGIN_ERROR, "invalid_output_contract"
                            ) from None
                        if (observation.stream_id, observation.source_id) != (
                            ctx.stream_id,
                            ctx.source_id,
                        ):
                            raise PluginError(
                                common.INTERNAL_PLUGIN_ERROR, "output_context_mismatch"
                            )
                    return response
            finally:
                self._in_flight -= 1
        except TimeoutError:
            token.cancel()
            return _failure(common.DEADLINE_EXCEEDED, "processing_timeout", True)
        except PluginError as error:
            return _failure(error.code, error.reason_code, error.retryable)
        except ValueError:
            return _failure(common.INVALID_INPUT, "contract_validation_failed", False)
        except asyncio.CancelledError:
            token.cancel()
            raise
        except Exception:
            # 堆栈跟踪 / 媒体 URL / 密钥绝不出现在插件边界上。
            return _failure(common.INTERNAL_PLUGIN_ERROR, "plugin_execution_failed", False)


def _failure(code, reason, retryable):
    return ProcessResponse(
        error=common.ProcessingError(code=code, reason_code=reason, retryable=retryable)
    )

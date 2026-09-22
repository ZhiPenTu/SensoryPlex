"""In-process worker building blocks; process isolation belongs to the runtime.

Plugins implement deterministic processing. Durable idempotency, lease allocation,
gRPC lifecycle serving and restart recovery are worker responsibilities.
"""

import asyncio
import time
from abc import ABC, abstractmethod

from .generated.common.v1 import common_pb2 as common
from .generated.runtime.v1.runtime_pb2 import ProcessRequest, ProcessResponse
from .validation import validate_observation


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
    def __init__(self, max_concurrency: int = 1, max_batch_size: int = 8):
        if max_concurrency < 1 or max_batch_size < 1:
            raise ValueError("resource_limits_must_be_positive")
        self.max_concurrency, self.max_batch_size = max_concurrency, max_batch_size
        self._in_flight = 0

    @abstractmethod
    def describe(self): ...

    @abstractmethod
    async def process(
        self, request: ProcessRequest, cancel_token: CancelToken
    ) -> ProcessResponse: ...

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
                if item.WhichOneof("value") != "observation":
                    raise PluginError(common.UNSUPPORTED_MEMORY_KIND, "buffer_reader_not_attached")
                validate_observation(item.observation)
                if (item.observation.stream_id, item.observation.source_id) != (
                    ctx.stream_id,
                    ctx.source_id,
                ):
                    raise PluginError(common.INVALID_INPUT, "input_context_mismatch")
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
            # Stack traces / media URLs / secrets never cross a plugin boundary.
            return _failure(common.INTERNAL_PLUGIN_ERROR, "plugin_execution_failed", False)


def _failure(code, reason, retryable):
    return ProcessResponse(
        error=common.ProcessingError(code=code, reason_code=reason, retryable=retryable)
    )

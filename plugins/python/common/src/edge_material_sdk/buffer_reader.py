"""按 lease 读取 Runtime 数据面里的 buffer 字节。

插件不拥有 buffer：它只能通过 Runtime 授予的 lease 打开一个读取窗口，读完后显式释放。
这里刻意不提供任何旁路（不猜路径、不直接读文件、不缓存段内容）：

- 段名只在 `Acquire` 的应答里出现；它按运行随机派生，不可能从报告或 arena handle 推导；
- 读取窗口必须落在该 buffer 自己的区间内；越界由服务端拒绝，客户端不夹取；
- 读到的字节立刻按 lease 摘要校验，校验失败就是失败，不静默兜底；
- 任何路径（包括校验失败、取消、超时）都必须释放 lease，否则 buffer 一直占着保留表。

同 UID 进程之间**没有**逐 buffer 隔离：映射整段后相邻字节同样可见（ADR-010）。
本模块只保证"按 lease 读对了窗口"，不声称内存隔离。
"""

from __future__ import annotations

import hashlib
import mmap
import os
from dataclasses import dataclass

import grpc

from .generated.common.v1 import common_pb2 as common
from .generated.media.v1 import handoff_pb2, handoff_pb2_grpc

try:  # pragma: no cover - 非 POSIX 平台没有共享内存数据面
    from _posixshmem import shm_open
except ImportError as error:  # pragma: no cover
    shm_open = None
    _SHM_IMPORT_ERROR = error
else:
    _SHM_IMPORT_ERROR = None

CPU_SHARED_MEMORY = "cpu_shared_memory"
DEFAULT_TTL_MS = 30_000


class BufferReadError(Exception):
    """显式失败：带 Runtime 错误码与稳定原因串，不用日志代替契约。"""

    def __init__(self, code: int, reason_code: str, retryable: bool = False):
        super().__init__(reason_code)
        self.code, self.reason_code, self.retryable = code, reason_code, retryable


@dataclass(frozen=True)
class BufferRead:
    """一条被授权窗口的读取结果：字节 + 该窗口的实测摘要 + 它自己的时间锚点。"""

    buffer_id: str
    kind: str
    time_range: common.TimeRange
    format: common.BufferFormat
    digest: str
    payload: bytes

    def describe_source_item(self) -> str:
        """稳定输入派生稳定 ID：同一 buffer 的同一窗口永远得到同一个标识。"""
        return f"{self.buffer_id}@{self.digest.removeprefix('sha256:')[:16]}"


def digest_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class LeaseBufferReader:
    """gRPC `BufferHandoffService` 的只读客户端：Acquire → mmap → 校验 → Release。"""

    def __init__(self, endpoint: str, ttl_ms: int = DEFAULT_TTL_MS, timeout_s: float = 30.0):
        if not 50 <= ttl_ms <= 60_000:
            raise ValueError("ttl_ms_out_of_range")
        if _SHM_IMPORT_ERROR is not None:  # pragma: no cover - 非 POSIX 平台
            raise BufferReadError(
                common.UNSUPPORTED_MEMORY_KIND,
                f"shared_memory_unavailable:{_SHM_IMPORT_ERROR}",
            )
        self.endpoint = endpoint
        self.ttl_ms = ttl_ms
        self.timeout_s = timeout_s
        self._channel = grpc.insecure_channel(endpoint)
        self._stub = handoff_pb2_grpc.BufferHandoffServiceStub(self._channel)

    # --- 控制面 ---------------------------------------------------------------
    def listing(self) -> handoff_pb2.ListRetainedResponse:
        return self._stub.List(handoff_pb2.ListRetainedRequest(), timeout=self.timeout_s)

    def release(self, lease_id: str):
        return self._stub.Release(
            handoff_pb2.ReleaseBufferRequest(lease_id=lease_id), timeout=self.timeout_s
        )

    def close(self) -> None:
        self._channel.close()

    # --- 数据面 ---------------------------------------------------------------
    def read(self, buffer_id: str, offset: int = 0, length: int = 0) -> BufferRead:
        """领取一个读取窗口并读回字节；失败抛 `BufferReadError`。"""
        response = self._stub.Acquire(
            handoff_pb2.AcquireBufferRequest(
                buffer_id=buffer_id, offset_bytes=offset, length_bytes=length, ttl_ms=self.ttl_ms
            ),
            timeout=self.timeout_s,
        )
        if not response.granted:
            raise BufferReadError(response.error.code, response.error.reason_code)
        descriptor = response.buffer
        try:
            payload = self._map_window(response.segment_name, descriptor.locator)
        finally:
            # 授予之后无论读到什么都必须归还，否则保留表会被这一条占住直到 TTL 过期。
            released = self.release(descriptor.lease.lease_id)
            if not released.released:
                raise BufferReadError(
                    released.error.code or common.INTERNAL_PLUGIN_ERROR,
                    released.error.reason_code or "lease_release_rejected",
                )
        digest = digest_of(payload)
        if digest != descriptor.content_hash:
            raise BufferReadError(
                common.TRANSIENT_BACKEND_FAILURE, "window_digest_mismatch", retryable=False
            )
        return BufferRead(
            buffer_id=descriptor.buffer_id,
            kind=descriptor.kind,
            time_range=descriptor.time_range,
            format=descriptor.format,
            digest=digest,
            payload=payload,
        )

    def discard(self, buffer_id: str) -> None:
        """领取一条 buffer 后立刻归还，不映射、也不读字节。

        这是"已投递、不消费"的显式确认：数据面要求消费者对每条保留都给出归宿
        （`released + expired + retained == retained_total`），因此调度方不能把
        "我不需要这条"实现成"装作没看见"。它不产生任何 observation。
        """
        response = self._stub.Acquire(
            handoff_pb2.AcquireBufferRequest(
                buffer_id=buffer_id, offset_bytes=0, length_bytes=1, ttl_ms=self.ttl_ms
            ),
            timeout=self.timeout_s,
        )
        if not response.granted:
            raise BufferReadError(response.error.code, response.error.reason_code)
        released = self.release(response.buffer.lease.lease_id)
        if not released.released:
            raise BufferReadError(
                released.error.code or common.INTERNAL_PLUGIN_ERROR,
                released.error.reason_code or "lease_release_rejected",
            )

    def _map_window(self, segment_name: str, locator: common.BufferLocator) -> bytes:
        if not segment_name:
            raise BufferReadError(common.TRANSIENT_BACKEND_FAILURE, "empty_segment_name")
        try:
            fd = shm_open(segment_name, os.O_RDONLY, 0o600)
        except OSError as error:
            # 生产者已退出或段名失效：这是可重试的传输层失败，不是"没有数据"。
            raise BufferReadError(
                common.TRANSIENT_BACKEND_FAILURE, f"segment_unavailable:{error.errno}", True
            ) from error
        try:
            size = os.fstat(fd).st_size
            mapping = mmap.mmap(fd, size, prot=mmap.PROT_READ)
            try:
                end = locator.offset + locator.length
                if locator.length == 0 or end > len(mapping):
                    raise BufferReadError(
                        common.INVALID_INPUT, "segment_shorter_than_window", retryable=False
                    )
                return bytes(mapping[locator.offset : end])
            finally:
                mapping.close()
        finally:
            os.close(fd)

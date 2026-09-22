from edge_material_sdk.generated.common.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RetainedBuffer(_message.Message):
    __slots__ = ("buffer_id", "kind", "stream_id", "time_range", "format", "offset_bytes", "length_bytes", "content_hash", "lease_id")
    BUFFER_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_RANGE_FIELD_NUMBER: _ClassVar[int]
    FORMAT_FIELD_NUMBER: _ClassVar[int]
    OFFSET_BYTES_FIELD_NUMBER: _ClassVar[int]
    LENGTH_BYTES_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    buffer_id: str
    kind: str
    stream_id: str
    time_range: _common_pb2.TimeRange
    format: _common_pb2.BufferFormat
    offset_bytes: int
    length_bytes: int
    content_hash: str
    lease_id: str
    def __init__(self, buffer_id: _Optional[str] = ..., kind: _Optional[str] = ..., stream_id: _Optional[str] = ..., time_range: _Optional[_Union[_common_pb2.TimeRange, _Mapping]] = ..., format: _Optional[_Union[_common_pb2.BufferFormat, _Mapping]] = ..., offset_bytes: _Optional[int] = ..., length_bytes: _Optional[int] = ..., content_hash: _Optional[str] = ..., lease_id: _Optional[str] = ...) -> None: ...

class HandoffStats(_message.Message):
    __slots__ = ("retained_limit", "retained", "leased", "retained_total", "released_total", "expired_total", "retain_rejections", "request_rejections", "rejection_reasons", "arena_capacity_bytes", "arena_used_bytes", "arena_peak_bytes", "arena_live_slabs", "offered_total")
    class RejectionReasonsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    RETAINED_LIMIT_FIELD_NUMBER: _ClassVar[int]
    RETAINED_FIELD_NUMBER: _ClassVar[int]
    LEASED_FIELD_NUMBER: _ClassVar[int]
    RETAINED_TOTAL_FIELD_NUMBER: _ClassVar[int]
    RELEASED_TOTAL_FIELD_NUMBER: _ClassVar[int]
    EXPIRED_TOTAL_FIELD_NUMBER: _ClassVar[int]
    RETAIN_REJECTIONS_FIELD_NUMBER: _ClassVar[int]
    REQUEST_REJECTIONS_FIELD_NUMBER: _ClassVar[int]
    REJECTION_REASONS_FIELD_NUMBER: _ClassVar[int]
    ARENA_CAPACITY_BYTES_FIELD_NUMBER: _ClassVar[int]
    ARENA_USED_BYTES_FIELD_NUMBER: _ClassVar[int]
    ARENA_PEAK_BYTES_FIELD_NUMBER: _ClassVar[int]
    ARENA_LIVE_SLABS_FIELD_NUMBER: _ClassVar[int]
    OFFERED_TOTAL_FIELD_NUMBER: _ClassVar[int]
    retained_limit: int
    retained: int
    leased: int
    retained_total: int
    released_total: int
    expired_total: int
    retain_rejections: int
    request_rejections: int
    rejection_reasons: _containers.ScalarMap[str, int]
    arena_capacity_bytes: int
    arena_used_bytes: int
    arena_peak_bytes: int
    arena_live_slabs: int
    offered_total: int
    def __init__(self, retained_limit: _Optional[int] = ..., retained: _Optional[int] = ..., leased: _Optional[int] = ..., retained_total: _Optional[int] = ..., released_total: _Optional[int] = ..., expired_total: _Optional[int] = ..., retain_rejections: _Optional[int] = ..., request_rejections: _Optional[int] = ..., rejection_reasons: _Optional[_Mapping[str, int]] = ..., arena_capacity_bytes: _Optional[int] = ..., arena_used_bytes: _Optional[int] = ..., arena_peak_bytes: _Optional[int] = ..., arena_live_slabs: _Optional[int] = ..., offered_total: _Optional[int] = ...) -> None: ...

class ListRetainedRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListRetainedResponse(_message.Message):
    __slots__ = ("buffers", "stats", "segment_name")
    BUFFERS_FIELD_NUMBER: _ClassVar[int]
    STATS_FIELD_NUMBER: _ClassVar[int]
    SEGMENT_NAME_FIELD_NUMBER: _ClassVar[int]
    buffers: _containers.RepeatedCompositeFieldContainer[RetainedBuffer]
    stats: HandoffStats
    segment_name: str
    def __init__(self, buffers: _Optional[_Iterable[_Union[RetainedBuffer, _Mapping]]] = ..., stats: _Optional[_Union[HandoffStats, _Mapping]] = ..., segment_name: _Optional[str] = ...) -> None: ...

class HandoffStatsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HandoffStatsResponse(_message.Message):
    __slots__ = ("stats", "segment_name")
    STATS_FIELD_NUMBER: _ClassVar[int]
    SEGMENT_NAME_FIELD_NUMBER: _ClassVar[int]
    stats: HandoffStats
    segment_name: str
    def __init__(self, stats: _Optional[_Union[HandoffStats, _Mapping]] = ..., segment_name: _Optional[str] = ...) -> None: ...

class AcquireBufferRequest(_message.Message):
    __slots__ = ("buffer_id", "offset_bytes", "length_bytes", "ttl_ms")
    BUFFER_ID_FIELD_NUMBER: _ClassVar[int]
    OFFSET_BYTES_FIELD_NUMBER: _ClassVar[int]
    LENGTH_BYTES_FIELD_NUMBER: _ClassVar[int]
    TTL_MS_FIELD_NUMBER: _ClassVar[int]
    buffer_id: str
    offset_bytes: int
    length_bytes: int
    ttl_ms: int
    def __init__(self, buffer_id: _Optional[str] = ..., offset_bytes: _Optional[int] = ..., length_bytes: _Optional[int] = ..., ttl_ms: _Optional[int] = ...) -> None: ...

class AcquireBufferResponse(_message.Message):
    __slots__ = ("granted", "buffer", "segment_name", "arena_capacity_bytes", "error")
    GRANTED_FIELD_NUMBER: _ClassVar[int]
    BUFFER_FIELD_NUMBER: _ClassVar[int]
    SEGMENT_NAME_FIELD_NUMBER: _ClassVar[int]
    ARENA_CAPACITY_BYTES_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    granted: bool
    buffer: _common_pb2.BufferDescriptor
    segment_name: str
    arena_capacity_bytes: int
    error: _common_pb2.ProcessingError
    def __init__(self, granted: bool = ..., buffer: _Optional[_Union[_common_pb2.BufferDescriptor, _Mapping]] = ..., segment_name: _Optional[str] = ..., arena_capacity_bytes: _Optional[int] = ..., error: _Optional[_Union[_common_pb2.ProcessingError, _Mapping]] = ...) -> None: ...

class ReleaseBufferRequest(_message.Message):
    __slots__ = ("lease_id",)
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    lease_id: str
    def __init__(self, lease_id: _Optional[str] = ...) -> None: ...

class ReleaseBufferResponse(_message.Message):
    __slots__ = ("released", "buffer_id", "error")
    RELEASED_FIELD_NUMBER: _ClassVar[int]
    BUFFER_ID_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    released: bool
    buffer_id: str
    error: _common_pb2.ProcessingError
    def __init__(self, released: bool = ..., buffer_id: _Optional[str] = ..., error: _Optional[_Union[_common_pb2.ProcessingError, _Mapping]] = ...) -> None: ...

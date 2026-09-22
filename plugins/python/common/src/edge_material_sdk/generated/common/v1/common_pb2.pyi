from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ErrorCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ERROR_CODE_UNSPECIFIED: _ClassVar[ErrorCode]
    INVALID_INPUT: _ClassVar[ErrorCode]
    UNSUPPORTED_CAPABILITY: _ClassVar[ErrorCode]
    UNSUPPORTED_MEMORY_KIND: _ClassVar[ErrorCode]
    DEADLINE_EXCEEDED: _ClassVar[ErrorCode]
    RESOURCE_EXHAUSTED: _ClassVar[ErrorCode]
    TRANSIENT_BACKEND_FAILURE: _ClassVar[ErrorCode]
    DATA_POLICY_DENIED: _ClassVar[ErrorCode]
    INTERNAL_PLUGIN_ERROR: _ClassVar[ErrorCode]
ERROR_CODE_UNSPECIFIED: ErrorCode
INVALID_INPUT: ErrorCode
UNSUPPORTED_CAPABILITY: ErrorCode
UNSUPPORTED_MEMORY_KIND: ErrorCode
DEADLINE_EXCEEDED: ErrorCode
RESOURCE_EXHAUSTED: ErrorCode
TRANSIENT_BACKEND_FAILURE: ErrorCode
DATA_POLICY_DENIED: ErrorCode
INTERNAL_PLUGIN_ERROR: ErrorCode

class TimeRange(_message.Message):
    __slots__ = ("start_ms", "end_ms")
    START_MS_FIELD_NUMBER: _ClassVar[int]
    END_MS_FIELD_NUMBER: _ClassVar[int]
    start_ms: int
    end_ms: int
    def __init__(self, start_ms: _Optional[int] = ..., end_ms: _Optional[int] = ...) -> None: ...

class RequestContext(_message.Message):
    __slots__ = ("request_id", "trace_id", "pipeline_run_id", "stream_id", "source_id", "deadline_unix_ms", "attempt", "idempotency_key", "privacy_policy")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_RUN_ID_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    PRIVACY_POLICY_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    trace_id: str
    pipeline_run_id: str
    stream_id: str
    source_id: str
    deadline_unix_ms: int
    attempt: int
    idempotency_key: str
    privacy_policy: PrivacyPolicy
    def __init__(self, request_id: _Optional[str] = ..., trace_id: _Optional[str] = ..., pipeline_run_id: _Optional[str] = ..., stream_id: _Optional[str] = ..., source_id: _Optional[str] = ..., deadline_unix_ms: _Optional[int] = ..., attempt: _Optional[int] = ..., idempotency_key: _Optional[str] = ..., privacy_policy: _Optional[_Union[PrivacyPolicy, _Mapping]] = ...) -> None: ...

class PrivacyPolicy(_message.Message):
    __slots__ = ("data_egress", "retain_derived_payload")
    DATA_EGRESS_FIELD_NUMBER: _ClassVar[int]
    RETAIN_DERIVED_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    data_egress: str
    retain_derived_payload: bool
    def __init__(self, data_egress: _Optional[str] = ..., retain_derived_payload: bool = ...) -> None: ...

class ProcessingError(_message.Message):
    __slots__ = ("code", "reason_code", "retryable", "retry_after_ms")
    CODE_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    RETRY_AFTER_MS_FIELD_NUMBER: _ClassVar[int]
    code: ErrorCode
    reason_code: str
    retryable: bool
    retry_after_ms: int
    def __init__(self, code: _Optional[_Union[ErrorCode, str]] = ..., reason_code: _Optional[str] = ..., retryable: bool = ..., retry_after_ms: _Optional[int] = ...) -> None: ...

class BufferDescriptor(_message.Message):
    __slots__ = ("buffer_id", "kind", "memory_kind", "locator", "format", "stream_id", "time_range", "lease", "content_hash")
    BUFFER_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MEMORY_KIND_FIELD_NUMBER: _ClassVar[int]
    LOCATOR_FIELD_NUMBER: _ClassVar[int]
    FORMAT_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_RANGE_FIELD_NUMBER: _ClassVar[int]
    LEASE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    buffer_id: str
    kind: str
    memory_kind: str
    locator: BufferLocator
    format: BufferFormat
    stream_id: str
    time_range: TimeRange
    lease: BufferLease
    content_hash: str
    def __init__(self, buffer_id: _Optional[str] = ..., kind: _Optional[str] = ..., memory_kind: _Optional[str] = ..., locator: _Optional[_Union[BufferLocator, _Mapping]] = ..., format: _Optional[_Union[BufferFormat, _Mapping]] = ..., stream_id: _Optional[str] = ..., time_range: _Optional[_Union[TimeRange, _Mapping]] = ..., lease: _Optional[_Union[BufferLease, _Mapping]] = ..., content_hash: _Optional[str] = ...) -> None: ...

class BufferLocator(_message.Message):
    __slots__ = ("handle", "offset", "length")
    HANDLE_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    LENGTH_FIELD_NUMBER: _ClassVar[int]
    handle: str
    offset: int
    length: int
    def __init__(self, handle: _Optional[str] = ..., offset: _Optional[int] = ..., length: _Optional[int] = ...) -> None: ...

class BufferFormat(_message.Message):
    __slots__ = ("pixel_format", "width", "height", "strides", "sample_rate", "channels")
    PIXEL_FORMAT_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    STRIDES_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    pixel_format: str
    width: int
    height: int
    strides: _containers.RepeatedScalarFieldContainer[int]
    sample_rate: int
    channels: int
    def __init__(self, pixel_format: _Optional[str] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., strides: _Optional[_Iterable[int]] = ..., sample_rate: _Optional[int] = ..., channels: _Optional[int] = ...) -> None: ...

class BufferLease(_message.Message):
    __slots__ = ("lease_id", "expires_at_unix_ms", "read_only")
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    READ_ONLY_FIELD_NUMBER: _ClassVar[int]
    lease_id: str
    expires_at_unix_ms: int
    read_only: bool
    def __init__(self, lease_id: _Optional[str] = ..., expires_at_unix_ms: _Optional[int] = ..., read_only: bool = ...) -> None: ...

class EventEnvelope(_message.Message):
    __slots__ = ("event_id", "event_type", "stream_id", "trace_id", "payload_ref", "created_at_unix_ms", "schema_version")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_REF_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    event_type: str
    stream_id: str
    trace_id: str
    payload_ref: str
    created_at_unix_ms: int
    schema_version: int
    def __init__(self, event_id: _Optional[str] = ..., event_type: _Optional[str] = ..., stream_id: _Optional[str] = ..., trace_id: _Optional[str] = ..., payload_ref: _Optional[str] = ..., created_at_unix_ms: _Optional[int] = ..., schema_version: _Optional[int] = ...) -> None: ...

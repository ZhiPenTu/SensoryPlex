from edge_material_sdk.generated.media.v1 import media_pb2 as _media_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class StreamStall(_message.Message):
    __slots__ = ("started_ms", "ended_ms", "gap_ms", "pts_jump_ms", "reason")
    STARTED_MS_FIELD_NUMBER: _ClassVar[int]
    ENDED_MS_FIELD_NUMBER: _ClassVar[int]
    GAP_MS_FIELD_NUMBER: _ClassVar[int]
    PTS_JUMP_MS_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    started_ms: int
    ended_ms: int
    gap_ms: int
    pts_jump_ms: int
    reason: str
    def __init__(self, started_ms: _Optional[int] = ..., ended_ms: _Optional[int] = ..., gap_ms: _Optional[int] = ..., pts_jump_ms: _Optional[int] = ..., reason: _Optional[str] = ...) -> None: ...

class LiveStreamStats(_message.Message):
    __slots__ = ("uri_secret_ref", "requested_duration_ms", "elapsed_ms", "samples", "stalls", "stalled_ms", "max_stall_ms", "pts_gap_total_ms", "recovered", "ended_by_deadline", "reconnect_owner", "stall_events")
    URI_SECRET_REF_FIELD_NUMBER: _ClassVar[int]
    REQUESTED_DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MS_FIELD_NUMBER: _ClassVar[int]
    SAMPLES_FIELD_NUMBER: _ClassVar[int]
    STALLS_FIELD_NUMBER: _ClassVar[int]
    STALLED_MS_FIELD_NUMBER: _ClassVar[int]
    MAX_STALL_MS_FIELD_NUMBER: _ClassVar[int]
    PTS_GAP_TOTAL_MS_FIELD_NUMBER: _ClassVar[int]
    RECOVERED_FIELD_NUMBER: _ClassVar[int]
    ENDED_BY_DEADLINE_FIELD_NUMBER: _ClassVar[int]
    RECONNECT_OWNER_FIELD_NUMBER: _ClassVar[int]
    STALL_EVENTS_FIELD_NUMBER: _ClassVar[int]
    uri_secret_ref: str
    requested_duration_ms: int
    elapsed_ms: int
    samples: int
    stalls: int
    stalled_ms: int
    max_stall_ms: int
    pts_gap_total_ms: int
    recovered: bool
    ended_by_deadline: bool
    reconnect_owner: str
    stall_events: _containers.RepeatedCompositeFieldContainer[StreamStall]
    def __init__(self, uri_secret_ref: _Optional[str] = ..., requested_duration_ms: _Optional[int] = ..., elapsed_ms: _Optional[int] = ..., samples: _Optional[int] = ..., stalls: _Optional[int] = ..., stalled_ms: _Optional[int] = ..., max_stall_ms: _Optional[int] = ..., pts_gap_total_ms: _Optional[int] = ..., recovered: bool = ..., ended_by_deadline: bool = ..., reconnect_owner: _Optional[str] = ..., stall_events: _Optional[_Iterable[_Union[StreamStall, _Mapping]]] = ...) -> None: ...

class LiveIngestReport(_message.Message):
    __slots__ = ("source", "platform", "stream", "decoded", "blockers", "golden_path_verified", "handoff_state", "media_queue")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    DECODED_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    GOLDEN_PATH_VERIFIED_FIELD_NUMBER: _ClassVar[int]
    HANDOFF_STATE_FIELD_NUMBER: _ClassVar[int]
    MEDIA_QUEUE_FIELD_NUMBER: _ClassVar[int]
    source: _media_pb2.MediaSourceDescription
    platform: str
    stream: LiveStreamStats
    decoded: _media_pb2.DecodedDataPlane
    blockers: _containers.RepeatedScalarFieldContainer[str]
    golden_path_verified: bool
    handoff_state: str
    media_queue: _media_pb2.MediaQueueAdmission
    def __init__(self, source: _Optional[_Union[_media_pb2.MediaSourceDescription, _Mapping]] = ..., platform: _Optional[str] = ..., stream: _Optional[_Union[LiveStreamStats, _Mapping]] = ..., decoded: _Optional[_Union[_media_pb2.DecodedDataPlane, _Mapping]] = ..., blockers: _Optional[_Iterable[str]] = ..., golden_path_verified: bool = ..., handoff_state: _Optional[str] = ..., media_queue: _Optional[_Union[_media_pb2.MediaQueueAdmission, _Mapping]] = ...) -> None: ...

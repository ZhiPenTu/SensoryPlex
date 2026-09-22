from edge_material_sdk.generated.common.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class MediaSourceKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MEDIA_SOURCE_KIND_UNSPECIFIED: _ClassVar[MediaSourceKind]
    MEDIA_SOURCE_KIND_FILE: _ClassVar[MediaSourceKind]
    MEDIA_SOURCE_KIND_SRT: _ClassVar[MediaSourceKind]
MEDIA_SOURCE_KIND_UNSPECIFIED: MediaSourceKind
MEDIA_SOURCE_KIND_FILE: MediaSourceKind
MEDIA_SOURCE_KIND_SRT: MediaSourceKind

class MediaSourceRef(_message.Message):
    __slots__ = ("stream_id", "source_id", "kind", "uri_secret_ref", "content_hash")
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    URI_SECRET_REF_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    stream_id: str
    source_id: str
    kind: MediaSourceKind
    uri_secret_ref: str
    content_hash: str
    def __init__(self, stream_id: _Optional[str] = ..., source_id: _Optional[str] = ..., kind: _Optional[_Union[MediaSourceKind, str]] = ..., uri_secret_ref: _Optional[str] = ..., content_hash: _Optional[str] = ...) -> None: ...

class MediaTrack(_message.Message):
    __slots__ = ("track_kind", "codec", "width", "height", "sample_rate", "channels", "average_frame_rate", "timing_known")
    TRACK_KIND_FIELD_NUMBER: _ClassVar[int]
    CODEC_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    AVERAGE_FRAME_RATE_FIELD_NUMBER: _ClassVar[int]
    TIMING_KNOWN_FIELD_NUMBER: _ClassVar[int]
    track_kind: str
    codec: str
    width: int
    height: int
    sample_rate: int
    channels: int
    average_frame_rate: float
    timing_known: bool
    def __init__(self, track_kind: _Optional[str] = ..., codec: _Optional[str] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., sample_rate: _Optional[int] = ..., channels: _Optional[int] = ..., average_frame_rate: _Optional[float] = ..., timing_known: bool = ...) -> None: ...

class MediaSourceDescription(_message.Message):
    __slots__ = ("source", "tracks", "duration_ms", "probe_tool")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TRACKS_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    PROBE_TOOL_FIELD_NUMBER: _ClassVar[int]
    source: MediaSourceRef
    tracks: _containers.RepeatedCompositeFieldContainer[MediaTrack]
    duration_ms: int
    probe_tool: str
    def __init__(self, source: _Optional[_Union[MediaSourceRef, _Mapping]] = ..., tracks: _Optional[_Iterable[_Union[MediaTrack, _Mapping]]] = ..., duration_ms: _Optional[int] = ..., probe_tool: _Optional[str] = ...) -> None: ...

class TimelineAnchor(_message.Message):
    __slots__ = ("anchor_id", "track_kind", "time_range", "pts_ms", "keyframe")
    ANCHOR_ID_FIELD_NUMBER: _ClassVar[int]
    TRACK_KIND_FIELD_NUMBER: _ClassVar[int]
    TIME_RANGE_FIELD_NUMBER: _ClassVar[int]
    PTS_MS_FIELD_NUMBER: _ClassVar[int]
    KEYFRAME_FIELD_NUMBER: _ClassVar[int]
    anchor_id: str
    track_kind: str
    time_range: _common_pb2.TimeRange
    pts_ms: int
    keyframe: bool
    def __init__(self, anchor_id: _Optional[str] = ..., track_kind: _Optional[str] = ..., time_range: _Optional[_Union[_common_pb2.TimeRange, _Mapping]] = ..., pts_ms: _Optional[int] = ..., keyframe: bool = ...) -> None: ...

class ReplayReport(_message.Message):
    __slots__ = ("source", "anchors", "decoded_items", "emitted_anchors", "dropped_items", "out_of_order_items", "gap_items", "platform", "blockers", "drop_reasons", "golden_path_verified")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    ANCHORS_FIELD_NUMBER: _ClassVar[int]
    DECODED_ITEMS_FIELD_NUMBER: _ClassVar[int]
    EMITTED_ANCHORS_FIELD_NUMBER: _ClassVar[int]
    DROPPED_ITEMS_FIELD_NUMBER: _ClassVar[int]
    OUT_OF_ORDER_ITEMS_FIELD_NUMBER: _ClassVar[int]
    GAP_ITEMS_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    DROP_REASONS_FIELD_NUMBER: _ClassVar[int]
    GOLDEN_PATH_VERIFIED_FIELD_NUMBER: _ClassVar[int]
    source: MediaSourceDescription
    anchors: _containers.RepeatedCompositeFieldContainer[TimelineAnchor]
    decoded_items: int
    emitted_anchors: int
    dropped_items: int
    out_of_order_items: int
    gap_items: int
    platform: str
    blockers: _containers.RepeatedScalarFieldContainer[str]
    drop_reasons: _containers.RepeatedScalarFieldContainer[str]
    golden_path_verified: bool
    def __init__(self, source: _Optional[_Union[MediaSourceDescription, _Mapping]] = ..., anchors: _Optional[_Iterable[_Union[TimelineAnchor, _Mapping]]] = ..., decoded_items: _Optional[int] = ..., emitted_anchors: _Optional[int] = ..., dropped_items: _Optional[int] = ..., out_of_order_items: _Optional[int] = ..., gap_items: _Optional[int] = ..., platform: _Optional[str] = ..., blockers: _Optional[_Iterable[str]] = ..., drop_reasons: _Optional[_Iterable[str]] = ..., golden_path_verified: bool = ...) -> None: ...

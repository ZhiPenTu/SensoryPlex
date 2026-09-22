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

class DecodedTrackStat(_message.Message):
    __slots__ = ("track_kind", "samples", "bytes", "first_pts_ms", "last_end_ms", "width", "height", "pixel_format", "sample_rate", "channels", "audio_format", "dropped_samples", "drop_reasons", "timeline_offset_ms", "overlapping_samples")
    TRACK_KIND_FIELD_NUMBER: _ClassVar[int]
    SAMPLES_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    FIRST_PTS_MS_FIELD_NUMBER: _ClassVar[int]
    LAST_END_MS_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    PIXEL_FORMAT_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    AUDIO_FORMAT_FIELD_NUMBER: _ClassVar[int]
    DROPPED_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    DROP_REASONS_FIELD_NUMBER: _ClassVar[int]
    TIMELINE_OFFSET_MS_FIELD_NUMBER: _ClassVar[int]
    OVERLAPPING_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    track_kind: str
    samples: int
    bytes: int
    first_pts_ms: int
    last_end_ms: int
    width: int
    height: int
    pixel_format: str
    sample_rate: int
    channels: int
    audio_format: str
    dropped_samples: int
    drop_reasons: _containers.RepeatedScalarFieldContainer[str]
    timeline_offset_ms: int
    overlapping_samples: int
    def __init__(self, track_kind: _Optional[str] = ..., samples: _Optional[int] = ..., bytes: _Optional[int] = ..., first_pts_ms: _Optional[int] = ..., last_end_ms: _Optional[int] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., pixel_format: _Optional[str] = ..., sample_rate: _Optional[int] = ..., channels: _Optional[int] = ..., audio_format: _Optional[str] = ..., dropped_samples: _Optional[int] = ..., drop_reasons: _Optional[_Iterable[str]] = ..., timeline_offset_ms: _Optional[int] = ..., overlapping_samples: _Optional[int] = ...) -> None: ...

class AudioSegment(_message.Message):
    __slots__ = ("segment_id", "time_range", "sample_rate", "channels", "bytes", "partial")
    SEGMENT_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_RANGE_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_FIELD_NUMBER: _ClassVar[int]
    segment_id: str
    time_range: _common_pb2.TimeRange
    sample_rate: int
    channels: int
    bytes: int
    partial: bool
    def __init__(self, segment_id: _Optional[str] = ..., time_range: _Optional[_Union[_common_pb2.TimeRange, _Mapping]] = ..., sample_rate: _Optional[int] = ..., channels: _Optional[int] = ..., bytes: _Optional[int] = ..., partial: bool = ...) -> None: ...

class AudioSegmentReport(_message.Message):
    __slots__ = ("segment_ms", "segments", "partial_segments", "bytes", "listed", "listed_limit", "dropped_samples", "discontinuities", "drop_reasons")
    SEGMENT_MS_FIELD_NUMBER: _ClassVar[int]
    SEGMENTS_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_SEGMENTS_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    LISTED_FIELD_NUMBER: _ClassVar[int]
    LISTED_LIMIT_FIELD_NUMBER: _ClassVar[int]
    DROPPED_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    DISCONTINUITIES_FIELD_NUMBER: _ClassVar[int]
    DROP_REASONS_FIELD_NUMBER: _ClassVar[int]
    segment_ms: int
    segments: int
    partial_segments: int
    bytes: int
    listed: _containers.RepeatedCompositeFieldContainer[AudioSegment]
    listed_limit: int
    dropped_samples: int
    discontinuities: int
    drop_reasons: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, segment_ms: _Optional[int] = ..., segments: _Optional[int] = ..., partial_segments: _Optional[int] = ..., bytes: _Optional[int] = ..., listed: _Optional[_Iterable[_Union[AudioSegment, _Mapping]]] = ..., listed_limit: _Optional[int] = ..., dropped_samples: _Optional[int] = ..., discontinuities: _Optional[int] = ..., drop_reasons: _Optional[_Iterable[str]] = ...) -> None: ...

class DecodedDataPlane(_message.Message):
    __slots__ = ("arena_id", "arena_capacity_bytes", "arena_peak_bytes", "decoded_bytes", "tracks", "descriptors_built", "descriptors_validated", "descriptor_failures", "failure_reasons", "leases_issued", "leases_released", "evidence_descriptors", "audio_segments")
    ARENA_ID_FIELD_NUMBER: _ClassVar[int]
    ARENA_CAPACITY_BYTES_FIELD_NUMBER: _ClassVar[int]
    ARENA_PEAK_BYTES_FIELD_NUMBER: _ClassVar[int]
    DECODED_BYTES_FIELD_NUMBER: _ClassVar[int]
    TRACKS_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTORS_BUILT_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTORS_VALIDATED_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTOR_FAILURES_FIELD_NUMBER: _ClassVar[int]
    FAILURE_REASONS_FIELD_NUMBER: _ClassVar[int]
    LEASES_ISSUED_FIELD_NUMBER: _ClassVar[int]
    LEASES_RELEASED_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_DESCRIPTORS_FIELD_NUMBER: _ClassVar[int]
    AUDIO_SEGMENTS_FIELD_NUMBER: _ClassVar[int]
    arena_id: str
    arena_capacity_bytes: int
    arena_peak_bytes: int
    decoded_bytes: int
    tracks: _containers.RepeatedCompositeFieldContainer[DecodedTrackStat]
    descriptors_built: int
    descriptors_validated: int
    descriptor_failures: int
    failure_reasons: _containers.RepeatedScalarFieldContainer[str]
    leases_issued: int
    leases_released: int
    evidence_descriptors: _containers.RepeatedCompositeFieldContainer[_common_pb2.BufferDescriptor]
    audio_segments: AudioSegmentReport
    def __init__(self, arena_id: _Optional[str] = ..., arena_capacity_bytes: _Optional[int] = ..., arena_peak_bytes: _Optional[int] = ..., decoded_bytes: _Optional[int] = ..., tracks: _Optional[_Iterable[_Union[DecodedTrackStat, _Mapping]]] = ..., descriptors_built: _Optional[int] = ..., descriptors_validated: _Optional[int] = ..., descriptor_failures: _Optional[int] = ..., failure_reasons: _Optional[_Iterable[str]] = ..., leases_issued: _Optional[int] = ..., leases_released: _Optional[int] = ..., evidence_descriptors: _Optional[_Iterable[_Union[_common_pb2.BufferDescriptor, _Mapping]]] = ..., audio_segments: _Optional[_Union[AudioSegmentReport, _Mapping]] = ...) -> None: ...

class ReplayReport(_message.Message):
    __slots__ = ("source", "anchors", "decoded_items", "emitted_anchors", "dropped_items", "out_of_order_items", "gap_items", "platform", "blockers", "drop_reasons", "golden_path_verified", "decoded")
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
    DECODED_FIELD_NUMBER: _ClassVar[int]
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
    decoded: DecodedDataPlane
    def __init__(self, source: _Optional[_Union[MediaSourceDescription, _Mapping]] = ..., anchors: _Optional[_Iterable[_Union[TimelineAnchor, _Mapping]]] = ..., decoded_items: _Optional[int] = ..., emitted_anchors: _Optional[int] = ..., dropped_items: _Optional[int] = ..., out_of_order_items: _Optional[int] = ..., gap_items: _Optional[int] = ..., platform: _Optional[str] = ..., blockers: _Optional[_Iterable[str]] = ..., drop_reasons: _Optional[_Iterable[str]] = ..., golden_path_verified: bool = ..., decoded: _Optional[_Union[DecodedDataPlane, _Mapping]] = ...) -> None: ...

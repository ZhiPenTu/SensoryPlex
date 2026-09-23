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

class FrameRateMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FRAME_RATE_MODE_UNKNOWN: _ClassVar[FrameRateMode]
    FRAME_RATE_MODE_CONSTANT: _ClassVar[FrameRateMode]
    FRAME_RATE_MODE_VARIABLE: _ClassVar[FrameRateMode]
MEDIA_SOURCE_KIND_UNSPECIFIED: MediaSourceKind
MEDIA_SOURCE_KIND_FILE: MediaSourceKind
MEDIA_SOURCE_KIND_SRT: MediaSourceKind
FRAME_RATE_MODE_UNKNOWN: FrameRateMode
FRAME_RATE_MODE_CONSTANT: FrameRateMode
FRAME_RATE_MODE_VARIABLE: FrameRateMode

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
    __slots__ = ("track_kind", "samples", "bytes", "first_pts_ms", "last_end_ms", "width", "height", "pixel_format", "sample_rate", "channels", "audio_format", "dropped_samples", "drop_reasons", "timeline_offset_ms", "overlapping_samples", "duration_derived_samples", "decoder_element", "source_codec", "source_bit_depth", "source_chroma_format", "colorimetry", "applied_rotation_deg", "frame_rate_mode", "declared_frame_rate_num", "declared_frame_rate_den")
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
    DURATION_DERIVED_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    DECODER_ELEMENT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_CODEC_FIELD_NUMBER: _ClassVar[int]
    SOURCE_BIT_DEPTH_FIELD_NUMBER: _ClassVar[int]
    SOURCE_CHROMA_FORMAT_FIELD_NUMBER: _ClassVar[int]
    COLORIMETRY_FIELD_NUMBER: _ClassVar[int]
    APPLIED_ROTATION_DEG_FIELD_NUMBER: _ClassVar[int]
    FRAME_RATE_MODE_FIELD_NUMBER: _ClassVar[int]
    DECLARED_FRAME_RATE_NUM_FIELD_NUMBER: _ClassVar[int]
    DECLARED_FRAME_RATE_DEN_FIELD_NUMBER: _ClassVar[int]
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
    duration_derived_samples: int
    decoder_element: str
    source_codec: str
    source_bit_depth: int
    source_chroma_format: str
    colorimetry: str
    applied_rotation_deg: int
    frame_rate_mode: FrameRateMode
    declared_frame_rate_num: int
    declared_frame_rate_den: int
    def __init__(self, track_kind: _Optional[str] = ..., samples: _Optional[int] = ..., bytes: _Optional[int] = ..., first_pts_ms: _Optional[int] = ..., last_end_ms: _Optional[int] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., pixel_format: _Optional[str] = ..., sample_rate: _Optional[int] = ..., channels: _Optional[int] = ..., audio_format: _Optional[str] = ..., dropped_samples: _Optional[int] = ..., drop_reasons: _Optional[_Iterable[str]] = ..., timeline_offset_ms: _Optional[int] = ..., overlapping_samples: _Optional[int] = ..., duration_derived_samples: _Optional[int] = ..., decoder_element: _Optional[str] = ..., source_codec: _Optional[str] = ..., source_bit_depth: _Optional[int] = ..., source_chroma_format: _Optional[str] = ..., colorimetry: _Optional[str] = ..., applied_rotation_deg: _Optional[int] = ..., frame_rate_mode: _Optional[_Union[FrameRateMode, str]] = ..., declared_frame_rate_num: _Optional[int] = ..., declared_frame_rate_den: _Optional[int] = ...) -> None: ...

class RejectedTrack(_message.Message):
    __slots__ = ("track_kind", "code", "detail", "container", "decoder_element")
    TRACK_KIND_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    DECODER_ELEMENT_FIELD_NUMBER: _ClassVar[int]
    track_kind: str
    code: str
    detail: str
    container: str
    decoder_element: str
    def __init__(self, track_kind: _Optional[str] = ..., code: _Optional[str] = ..., detail: _Optional[str] = ..., container: _Optional[str] = ..., decoder_element: _Optional[str] = ...) -> None: ...

class AudioSegment(_message.Message):
    __slots__ = ("segment_id", "time_range", "sample_rate", "channels", "bytes", "partial", "sample_format")
    SEGMENT_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_RANGE_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_FORMAT_FIELD_NUMBER: _ClassVar[int]
    segment_id: str
    time_range: _common_pb2.TimeRange
    sample_rate: int
    channels: int
    bytes: int
    partial: bool
    sample_format: str
    def __init__(self, segment_id: _Optional[str] = ..., time_range: _Optional[_Union[_common_pb2.TimeRange, _Mapping]] = ..., sample_rate: _Optional[int] = ..., channels: _Optional[int] = ..., bytes: _Optional[int] = ..., partial: bool = ..., sample_format: _Optional[str] = ...) -> None: ...

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

class SamplingReport(_message.Message):
    __slots__ = ("track_kind", "min_interval_ms", "static_hold_ms", "change_threshold", "observed", "kept", "kept_first_frame", "kept_content_change", "kept_static_heartbeat", "skipped_rate_limited", "skipped_no_change", "skipped_non_monotonic", "skipped_missing_signature", "max_gap_ms", "max_keeps_bound", "max_frame_interval_ms", "skipped_backpressure_throttled")
    TRACK_KIND_FIELD_NUMBER: _ClassVar[int]
    MIN_INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    STATIC_HOLD_MS_FIELD_NUMBER: _ClassVar[int]
    CHANGE_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_FIELD_NUMBER: _ClassVar[int]
    KEPT_FIELD_NUMBER: _ClassVar[int]
    KEPT_FIRST_FRAME_FIELD_NUMBER: _ClassVar[int]
    KEPT_CONTENT_CHANGE_FIELD_NUMBER: _ClassVar[int]
    KEPT_STATIC_HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_RATE_LIMITED_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_NO_CHANGE_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_NON_MONOTONIC_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_MISSING_SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    MAX_GAP_MS_FIELD_NUMBER: _ClassVar[int]
    MAX_KEEPS_BOUND_FIELD_NUMBER: _ClassVar[int]
    MAX_FRAME_INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_BACKPRESSURE_THROTTLED_FIELD_NUMBER: _ClassVar[int]
    track_kind: str
    min_interval_ms: int
    static_hold_ms: int
    change_threshold: int
    observed: int
    kept: int
    kept_first_frame: int
    kept_content_change: int
    kept_static_heartbeat: int
    skipped_rate_limited: int
    skipped_no_change: int
    skipped_non_monotonic: int
    skipped_missing_signature: int
    max_gap_ms: int
    max_keeps_bound: int
    max_frame_interval_ms: int
    skipped_backpressure_throttled: int
    def __init__(self, track_kind: _Optional[str] = ..., min_interval_ms: _Optional[int] = ..., static_hold_ms: _Optional[int] = ..., change_threshold: _Optional[int] = ..., observed: _Optional[int] = ..., kept: _Optional[int] = ..., kept_first_frame: _Optional[int] = ..., kept_content_change: _Optional[int] = ..., kept_static_heartbeat: _Optional[int] = ..., skipped_rate_limited: _Optional[int] = ..., skipped_no_change: _Optional[int] = ..., skipped_non_monotonic: _Optional[int] = ..., skipped_missing_signature: _Optional[int] = ..., max_gap_ms: _Optional[int] = ..., max_keeps_bound: _Optional[int] = ..., max_frame_interval_ms: _Optional[int] = ..., skipped_backpressure_throttled: _Optional[int] = ...) -> None: ...

class DecodedDataPlane(_message.Message):
    __slots__ = ("arena_id", "arena_capacity_bytes", "arena_peak_bytes", "decoded_bytes", "tracks", "descriptors_built", "descriptors_validated", "descriptor_failures", "failure_reasons", "leases_issued", "leases_released", "evidence_descriptors", "audio_segments", "sampling", "backpressure", "rejected_tracks")
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
    SAMPLING_FIELD_NUMBER: _ClassVar[int]
    BACKPRESSURE_FIELD_NUMBER: _ClassVar[int]
    REJECTED_TRACKS_FIELD_NUMBER: _ClassVar[int]
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
    sampling: _containers.RepeatedCompositeFieldContainer[SamplingReport]
    backpressure: BackpressureReport
    rejected_tracks: _containers.RepeatedCompositeFieldContainer[RejectedTrack]
    def __init__(self, arena_id: _Optional[str] = ..., arena_capacity_bytes: _Optional[int] = ..., arena_peak_bytes: _Optional[int] = ..., decoded_bytes: _Optional[int] = ..., tracks: _Optional[_Iterable[_Union[DecodedTrackStat, _Mapping]]] = ..., descriptors_built: _Optional[int] = ..., descriptors_validated: _Optional[int] = ..., descriptor_failures: _Optional[int] = ..., failure_reasons: _Optional[_Iterable[str]] = ..., leases_issued: _Optional[int] = ..., leases_released: _Optional[int] = ..., evidence_descriptors: _Optional[_Iterable[_Union[_common_pb2.BufferDescriptor, _Mapping]]] = ..., audio_segments: _Optional[_Union[AudioSegmentReport, _Mapping]] = ..., sampling: _Optional[_Iterable[_Union[SamplingReport, _Mapping]]] = ..., backpressure: _Optional[_Union[BackpressureReport, _Mapping]] = ..., rejected_tracks: _Optional[_Iterable[_Union[RejectedTrack, _Mapping]]] = ...) -> None: ...

class BackpressureQueue(_message.Message):
    __slots__ = ("name", "unit", "capacity", "current", "peak")
    NAME_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    CAPACITY_FIELD_NUMBER: _ClassVar[int]
    CURRENT_FIELD_NUMBER: _ClassVar[int]
    PEAK_FIELD_NUMBER: _ClassVar[int]
    name: str
    unit: str
    capacity: int
    current: int
    peak: int
    def __init__(self, name: _Optional[str] = ..., unit: _Optional[str] = ..., capacity: _Optional[int] = ..., current: _Optional[int] = ..., peak: _Optional[int] = ...) -> None: ...

class BackpressureDropReason(_message.Message):
    __slots__ = ("reason", "count")
    REASON_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    reason: str
    count: int
    def __init__(self, reason: _Optional[str] = ..., count: _Optional[int] = ...) -> None: ...

class BackpressureDropKind(_message.Message):
    __slots__ = ("kind", "count")
    KIND_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    kind: str
    count: int
    def __init__(self, kind: _Optional[str] = ..., count: _Optional[int] = ...) -> None: ...

class BackpressureReport(_message.Message):
    __slots__ = ("observed", "queues", "state", "degraded_entries", "saturated_entries", "dropped_total", "drop_reasons", "timeouts_total", "residency_samples", "residency_max_ms", "residency_avg_ms", "sampling_throttled_samples", "throttle_factor", "drop_kinds")
    OBSERVED_FIELD_NUMBER: _ClassVar[int]
    QUEUES_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_ENTRIES_FIELD_NUMBER: _ClassVar[int]
    SATURATED_ENTRIES_FIELD_NUMBER: _ClassVar[int]
    DROPPED_TOTAL_FIELD_NUMBER: _ClassVar[int]
    DROP_REASONS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUTS_TOTAL_FIELD_NUMBER: _ClassVar[int]
    RESIDENCY_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    RESIDENCY_MAX_MS_FIELD_NUMBER: _ClassVar[int]
    RESIDENCY_AVG_MS_FIELD_NUMBER: _ClassVar[int]
    SAMPLING_THROTTLED_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    THROTTLE_FACTOR_FIELD_NUMBER: _ClassVar[int]
    DROP_KINDS_FIELD_NUMBER: _ClassVar[int]
    observed: bool
    queues: _containers.RepeatedCompositeFieldContainer[BackpressureQueue]
    state: str
    degraded_entries: int
    saturated_entries: int
    dropped_total: int
    drop_reasons: _containers.RepeatedCompositeFieldContainer[BackpressureDropReason]
    timeouts_total: int
    residency_samples: int
    residency_max_ms: int
    residency_avg_ms: int
    sampling_throttled_samples: int
    throttle_factor: int
    drop_kinds: _containers.RepeatedCompositeFieldContainer[BackpressureDropKind]
    def __init__(self, observed: bool = ..., queues: _Optional[_Iterable[_Union[BackpressureQueue, _Mapping]]] = ..., state: _Optional[str] = ..., degraded_entries: _Optional[int] = ..., saturated_entries: _Optional[int] = ..., dropped_total: _Optional[int] = ..., drop_reasons: _Optional[_Iterable[_Union[BackpressureDropReason, _Mapping]]] = ..., timeouts_total: _Optional[int] = ..., residency_samples: _Optional[int] = ..., residency_max_ms: _Optional[int] = ..., residency_avg_ms: _Optional[int] = ..., sampling_throttled_samples: _Optional[int] = ..., throttle_factor: _Optional[int] = ..., drop_kinds: _Optional[_Iterable[_Union[BackpressureDropKind, _Mapping]]] = ...) -> None: ...

class ReplayReport(_message.Message):
    __slots__ = ("source", "anchors", "decoded_items", "emitted_anchors", "dropped_items", "out_of_order_items", "gap_items", "platform", "blockers", "drop_reasons", "golden_path_verified", "decoded", "handoff_state")
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
    HANDOFF_STATE_FIELD_NUMBER: _ClassVar[int]
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
    handoff_state: str
    def __init__(self, source: _Optional[_Union[MediaSourceDescription, _Mapping]] = ..., anchors: _Optional[_Iterable[_Union[TimelineAnchor, _Mapping]]] = ..., decoded_items: _Optional[int] = ..., emitted_anchors: _Optional[int] = ..., dropped_items: _Optional[int] = ..., out_of_order_items: _Optional[int] = ..., gap_items: _Optional[int] = ..., platform: _Optional[str] = ..., blockers: _Optional[_Iterable[str]] = ..., drop_reasons: _Optional[_Iterable[str]] = ..., golden_path_verified: bool = ..., decoded: _Optional[_Union[DecodedDataPlane, _Mapping]] = ..., handoff_state: _Optional[str] = ...) -> None: ...

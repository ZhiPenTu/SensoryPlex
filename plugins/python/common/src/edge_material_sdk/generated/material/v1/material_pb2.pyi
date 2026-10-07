from edge_material_sdk.generated.common.v1 import common_pb2 as _common_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ModelApplicability(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MODEL_APPLICABILITY_UNSPECIFIED: _ClassVar[ModelApplicability]
    MODEL_APPLICABILITY_MODEL_BASED: _ClassVar[ModelApplicability]
    MODEL_APPLICABILITY_NOT_APPLICABLE: _ClassVar[ModelApplicability]
MODEL_APPLICABILITY_UNSPECIFIED: ModelApplicability
MODEL_APPLICABILITY_MODEL_BASED: ModelApplicability
MODEL_APPLICABILITY_NOT_APPLICABLE: ModelApplicability

class Provenance(_message.Message):
    __slots__ = ("plugin", "plugin_version", "artifact_digest", "model_release_id", "model_id", "model_version", "config_hash", "execution_backend", "model_artifact_digest", "processor_release_id", "model_applicability")
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_VERSION_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    MODEL_RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_VERSION_FIELD_NUMBER: _ClassVar[int]
    CONFIG_HASH_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_BACKEND_FIELD_NUMBER: _ClassVar[int]
    MODEL_ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    PROCESSOR_RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_APPLICABILITY_FIELD_NUMBER: _ClassVar[int]
    plugin: str
    plugin_version: str
    artifact_digest: str
    model_release_id: str
    model_id: str
    model_version: str
    config_hash: str
    execution_backend: str
    model_artifact_digest: str
    processor_release_id: str
    model_applicability: ModelApplicability
    def __init__(self, plugin: _Optional[str] = ..., plugin_version: _Optional[str] = ..., artifact_digest: _Optional[str] = ..., model_release_id: _Optional[str] = ..., model_id: _Optional[str] = ..., model_version: _Optional[str] = ..., config_hash: _Optional[str] = ..., execution_backend: _Optional[str] = ..., model_artifact_digest: _Optional[str] = ..., processor_release_id: _Optional[str] = ..., model_applicability: _Optional[_Union[ModelApplicability, str]] = ...) -> None: ...

class Observation(_message.Message):
    __slots__ = ("observation_id", "modality", "stream_id", "source_id", "source_item_id", "time_range", "payload", "confidence", "confidence_unavailable_reason", "quality_state", "quality_reasons", "provenance", "content_hash", "created_at_unix_ms", "timing_source", "timing_confidence", "schema_id", "schema_version", "schema_digest")
    OBSERVATION_ID_FIELD_NUMBER: _ClassVar[int]
    MODALITY_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_RANGE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_UNAVAILABLE_REASON_FIELD_NUMBER: _ClassVar[int]
    QUALITY_STATE_FIELD_NUMBER: _ClassVar[int]
    QUALITY_REASONS_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    TIMING_SOURCE_FIELD_NUMBER: _ClassVar[int]
    TIMING_CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_ID_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_DIGEST_FIELD_NUMBER: _ClassVar[int]
    observation_id: str
    modality: str
    stream_id: str
    source_id: str
    source_item_id: str
    time_range: _common_pb2.TimeRange
    payload: _struct_pb2.Struct
    confidence: float
    confidence_unavailable_reason: str
    quality_state: str
    quality_reasons: _containers.RepeatedScalarFieldContainer[str]
    provenance: Provenance
    content_hash: str
    created_at_unix_ms: int
    timing_source: str
    timing_confidence: float
    schema_id: str
    schema_version: str
    schema_digest: str
    def __init__(self, observation_id: _Optional[str] = ..., modality: _Optional[str] = ..., stream_id: _Optional[str] = ..., source_id: _Optional[str] = ..., source_item_id: _Optional[str] = ..., time_range: _Optional[_Union[_common_pb2.TimeRange, _Mapping]] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., confidence: _Optional[float] = ..., confidence_unavailable_reason: _Optional[str] = ..., quality_state: _Optional[str] = ..., quality_reasons: _Optional[_Iterable[str]] = ..., provenance: _Optional[_Union[Provenance, _Mapping]] = ..., content_hash: _Optional[str] = ..., created_at_unix_ms: _Optional[int] = ..., timing_source: _Optional[str] = ..., timing_confidence: _Optional[float] = ..., schema_id: _Optional[str] = ..., schema_version: _Optional[str] = ..., schema_digest: _Optional[str] = ...) -> None: ...

class SourceReference(_message.Message):
    __slots__ = ("asset_id", "time_range", "content_hash")
    ASSET_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_RANGE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    asset_id: str
    time_range: _common_pb2.TimeRange
    content_hash: str
    def __init__(self, asset_id: _Optional[str] = ..., time_range: _Optional[_Union[_common_pb2.TimeRange, _Mapping]] = ..., content_hash: _Optional[str] = ...) -> None: ...

class MaterialUnit(_message.Message):
    __slots__ = ("material_unit_id", "stream_id", "time_range", "status", "revision", "observations", "tags", "source_refs", "pipeline_version", "pending_enrichments", "created_at_unix_ms", "superseded", "prev_revision")
    MATERIAL_UNIT_ID_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_RANGE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    OBSERVATIONS_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REFS_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PENDING_ENRICHMENTS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    SUPERSEDED_FIELD_NUMBER: _ClassVar[int]
    PREV_REVISION_FIELD_NUMBER: _ClassVar[int]
    material_unit_id: str
    stream_id: str
    time_range: _common_pb2.TimeRange
    status: str
    revision: int
    observations: _containers.RepeatedCompositeFieldContainer[Observation]
    tags: _containers.RepeatedScalarFieldContainer[str]
    source_refs: _containers.RepeatedCompositeFieldContainer[SourceReference]
    pipeline_version: str
    pending_enrichments: _containers.RepeatedScalarFieldContainer[str]
    created_at_unix_ms: int
    superseded: bool
    prev_revision: int
    def __init__(self, material_unit_id: _Optional[str] = ..., stream_id: _Optional[str] = ..., time_range: _Optional[_Union[_common_pb2.TimeRange, _Mapping]] = ..., status: _Optional[str] = ..., revision: _Optional[int] = ..., observations: _Optional[_Iterable[_Union[Observation, _Mapping]]] = ..., tags: _Optional[_Iterable[str]] = ..., source_refs: _Optional[_Iterable[_Union[SourceReference, _Mapping]]] = ..., pipeline_version: _Optional[str] = ..., pending_enrichments: _Optional[_Iterable[str]] = ..., created_at_unix_ms: _Optional[int] = ..., superseded: bool = ..., prev_revision: _Optional[int] = ...) -> None: ...

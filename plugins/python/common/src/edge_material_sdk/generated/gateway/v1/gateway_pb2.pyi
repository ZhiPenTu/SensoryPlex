from edge_material_sdk.generated.material.v1 import material_pb2 as _material_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SearchRequest(_message.Message):
    __slots__ = ("query", "stream_id", "start_ms", "end_ms", "modalities", "tags", "min_confidence", "limit", "mode", "execution_id")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    START_MS_FIELD_NUMBER: _ClassVar[int]
    END_MS_FIELD_NUMBER: _ClassVar[int]
    MODALITIES_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    MIN_CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    query: str
    stream_id: str
    start_ms: int
    end_ms: int
    modalities: _containers.RepeatedScalarFieldContainer[str]
    tags: _containers.RepeatedScalarFieldContainer[str]
    min_confidence: float
    limit: int
    mode: str
    execution_id: str
    def __init__(self, query: _Optional[str] = ..., stream_id: _Optional[str] = ..., start_ms: _Optional[int] = ..., end_ms: _Optional[int] = ..., modalities: _Optional[_Iterable[str]] = ..., tags: _Optional[_Iterable[str]] = ..., min_confidence: _Optional[float] = ..., limit: _Optional[int] = ..., mode: _Optional[str] = ..., execution_id: _Optional[str] = ...) -> None: ...

class SearchHit(_message.Message):
    __slots__ = ("material_unit_id", "revision", "distance", "embedding_id", "vector_ref", "observation_id")
    MATERIAL_UNIT_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    DISTANCE_FIELD_NUMBER: _ClassVar[int]
    EMBEDDING_ID_FIELD_NUMBER: _ClassVar[int]
    VECTOR_REF_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_ID_FIELD_NUMBER: _ClassVar[int]
    material_unit_id: str
    revision: int
    distance: float
    embedding_id: str
    vector_ref: str
    observation_id: str
    def __init__(self, material_unit_id: _Optional[str] = ..., revision: _Optional[int] = ..., distance: _Optional[float] = ..., embedding_id: _Optional[str] = ..., vector_ref: _Optional[str] = ..., observation_id: _Optional[str] = ...) -> None: ...

class SearchResponse(_message.Message):
    __slots__ = ("materials", "mode", "index_version", "hits", "unindexed_hits", "vector_index_key", "unresolved_hits")
    MATERIALS_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    INDEX_VERSION_FIELD_NUMBER: _ClassVar[int]
    HITS_FIELD_NUMBER: _ClassVar[int]
    UNINDEXED_HITS_FIELD_NUMBER: _ClassVar[int]
    VECTOR_INDEX_KEY_FIELD_NUMBER: _ClassVar[int]
    UNRESOLVED_HITS_FIELD_NUMBER: _ClassVar[int]
    materials: _containers.RepeatedCompositeFieldContainer[_material_pb2.MaterialUnit]
    mode: str
    index_version: str
    hits: _containers.RepeatedCompositeFieldContainer[SearchHit]
    unindexed_hits: int
    vector_index_key: str
    unresolved_hits: int
    def __init__(self, materials: _Optional[_Iterable[_Union[_material_pb2.MaterialUnit, _Mapping]]] = ..., mode: _Optional[str] = ..., index_version: _Optional[str] = ..., hits: _Optional[_Iterable[_Union[SearchHit, _Mapping]]] = ..., unindexed_hits: _Optional[int] = ..., vector_index_key: _Optional[str] = ..., unresolved_hits: _Optional[int] = ...) -> None: ...

class GetMaterialRequest(_message.Message):
    __slots__ = ("material_unit_id", "revision")
    MATERIAL_UNIT_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    material_unit_id: str
    revision: int
    def __init__(self, material_unit_id: _Optional[str] = ..., revision: _Optional[int] = ...) -> None: ...

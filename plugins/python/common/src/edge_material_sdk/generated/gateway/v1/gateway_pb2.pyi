from edge_material_sdk.generated.material.v1 import material_pb2 as _material_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SearchRequest(_message.Message):
    __slots__ = ("query", "stream_id", "start_ms", "end_ms", "modalities", "tags", "min_confidence", "limit", "mode")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    START_MS_FIELD_NUMBER: _ClassVar[int]
    END_MS_FIELD_NUMBER: _ClassVar[int]
    MODALITIES_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    MIN_CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    query: str
    stream_id: str
    start_ms: int
    end_ms: int
    modalities: _containers.RepeatedScalarFieldContainer[str]
    tags: _containers.RepeatedScalarFieldContainer[str]
    min_confidence: float
    limit: int
    mode: str
    def __init__(self, query: _Optional[str] = ..., stream_id: _Optional[str] = ..., start_ms: _Optional[int] = ..., end_ms: _Optional[int] = ..., modalities: _Optional[_Iterable[str]] = ..., tags: _Optional[_Iterable[str]] = ..., min_confidence: _Optional[float] = ..., limit: _Optional[int] = ..., mode: _Optional[str] = ...) -> None: ...

class SearchResponse(_message.Message):
    __slots__ = ("materials", "mode", "index_version")
    MATERIALS_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    INDEX_VERSION_FIELD_NUMBER: _ClassVar[int]
    materials: _containers.RepeatedCompositeFieldContainer[_material_pb2.MaterialUnit]
    mode: str
    index_version: str
    def __init__(self, materials: _Optional[_Iterable[_Union[_material_pb2.MaterialUnit, _Mapping]]] = ..., mode: _Optional[str] = ..., index_version: _Optional[str] = ...) -> None: ...

class GetMaterialRequest(_message.Message):
    __slots__ = ("material_unit_id", "revision")
    MATERIAL_UNIT_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    material_unit_id: str
    revision: int
    def __init__(self, material_unit_id: _Optional[str] = ..., revision: _Optional[int] = ...) -> None: ...

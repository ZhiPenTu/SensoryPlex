from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SemanticSearchRequest(_message.Message):
    __slots__ = ("query", "principal", "vector_index_key", "limit")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    PRINCIPAL_FIELD_NUMBER: _ClassVar[int]
    VECTOR_INDEX_KEY_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    query: str
    principal: str
    vector_index_key: str
    limit: int
    def __init__(self, query: _Optional[str] = ..., principal: _Optional[str] = ..., vector_index_key: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class SemanticHit(_message.Message):
    __slots__ = ("embedding_id", "material_unit_id", "material_revision", "distance", "vector_ref", "observation_id", "model_release_id", "dimension")
    EMBEDDING_ID_FIELD_NUMBER: _ClassVar[int]
    MATERIAL_UNIT_ID_FIELD_NUMBER: _ClassVar[int]
    MATERIAL_REVISION_FIELD_NUMBER: _ClassVar[int]
    DISTANCE_FIELD_NUMBER: _ClassVar[int]
    VECTOR_REF_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    DIMENSION_FIELD_NUMBER: _ClassVar[int]
    embedding_id: str
    material_unit_id: str
    material_revision: int
    distance: float
    vector_ref: str
    observation_id: str
    model_release_id: str
    dimension: int
    def __init__(self, embedding_id: _Optional[str] = ..., material_unit_id: _Optional[str] = ..., material_revision: _Optional[int] = ..., distance: _Optional[float] = ..., vector_ref: _Optional[str] = ..., observation_id: _Optional[str] = ..., model_release_id: _Optional[str] = ..., dimension: _Optional[int] = ...) -> None: ...

class IndexFailure(_message.Message):
    __slots__ = ("reason_code", "detail", "retryable")
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    reason_code: str
    detail: str
    retryable: bool
    def __init__(self, reason_code: _Optional[str] = ..., detail: _Optional[str] = ..., retryable: bool = ...) -> None: ...

class SemanticSearchResponse(_message.Message):
    __slots__ = ("hits", "vector_index_key", "collection", "index_version", "unindexed_hits", "query_model_release_id", "query_dimension", "error")
    HITS_FIELD_NUMBER: _ClassVar[int]
    VECTOR_INDEX_KEY_FIELD_NUMBER: _ClassVar[int]
    COLLECTION_FIELD_NUMBER: _ClassVar[int]
    INDEX_VERSION_FIELD_NUMBER: _ClassVar[int]
    UNINDEXED_HITS_FIELD_NUMBER: _ClassVar[int]
    QUERY_MODEL_RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    QUERY_DIMENSION_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    hits: _containers.RepeatedCompositeFieldContainer[SemanticHit]
    vector_index_key: str
    collection: str
    index_version: str
    unindexed_hits: int
    query_model_release_id: str
    query_dimension: int
    error: IndexFailure
    def __init__(self, hits: _Optional[_Iterable[_Union[SemanticHit, _Mapping]]] = ..., vector_index_key: _Optional[str] = ..., collection: _Optional[str] = ..., index_version: _Optional[str] = ..., unindexed_hits: _Optional[int] = ..., query_model_release_id: _Optional[str] = ..., query_dimension: _Optional[int] = ..., error: _Optional[_Union[IndexFailure, _Mapping]] = ...) -> None: ...

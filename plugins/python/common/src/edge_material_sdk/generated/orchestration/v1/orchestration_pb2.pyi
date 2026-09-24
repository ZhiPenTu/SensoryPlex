from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PipelineRunState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PIPELINE_RUN_STATE_UNSPECIFIED: _ClassVar[PipelineRunState]
    PIPELINE_RUN_STATE_ACCEPTED: _ClassVar[PipelineRunState]
    PIPELINE_RUN_STATE_VALIDATING: _ClassVar[PipelineRunState]
    PIPELINE_RUN_STATE_QUEUED: _ClassVar[PipelineRunState]
    PIPELINE_RUN_STATE_RUNNING: _ClassVar[PipelineRunState]
    PIPELINE_RUN_STATE_SUCCEEDED: _ClassVar[PipelineRunState]
    PIPELINE_RUN_STATE_FAILED: _ClassVar[PipelineRunState]
    PIPELINE_RUN_STATE_CANCELLED: _ClassVar[PipelineRunState]
    PIPELINE_RUN_STATE_EXPIRED: _ClassVar[PipelineRunState]

class PipelineTaskState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PIPELINE_TASK_STATE_UNSPECIFIED: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_PENDING: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_READY: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_ASSIGNED: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_RUNNING: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_RETRY_WAIT: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_SUCCEEDED: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_FAILED: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_CANCELLED: _ClassVar[PipelineTaskState]
    PIPELINE_TASK_STATE_BLOCKED: _ClassVar[PipelineTaskState]
PIPELINE_RUN_STATE_UNSPECIFIED: PipelineRunState
PIPELINE_RUN_STATE_ACCEPTED: PipelineRunState
PIPELINE_RUN_STATE_VALIDATING: PipelineRunState
PIPELINE_RUN_STATE_QUEUED: PipelineRunState
PIPELINE_RUN_STATE_RUNNING: PipelineRunState
PIPELINE_RUN_STATE_SUCCEEDED: PipelineRunState
PIPELINE_RUN_STATE_FAILED: PipelineRunState
PIPELINE_RUN_STATE_CANCELLED: PipelineRunState
PIPELINE_RUN_STATE_EXPIRED: PipelineRunState
PIPELINE_TASK_STATE_UNSPECIFIED: PipelineTaskState
PIPELINE_TASK_STATE_PENDING: PipelineTaskState
PIPELINE_TASK_STATE_READY: PipelineTaskState
PIPELINE_TASK_STATE_ASSIGNED: PipelineTaskState
PIPELINE_TASK_STATE_RUNNING: PipelineTaskState
PIPELINE_TASK_STATE_RETRY_WAIT: PipelineTaskState
PIPELINE_TASK_STATE_SUCCEEDED: PipelineTaskState
PIPELINE_TASK_STATE_FAILED: PipelineTaskState
PIPELINE_TASK_STATE_CANCELLED: PipelineTaskState
PIPELINE_TASK_STATE_BLOCKED: PipelineTaskState

class PluginReference(_message.Message):
    __slots__ = ("plugin_id", "version", "artifact_digest", "config_hash")
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    CONFIG_HASH_FIELD_NUMBER: _ClassVar[int]
    plugin_id: str
    version: str
    artifact_digest: str
    config_hash: str
    def __init__(self, plugin_id: _Optional[str] = ..., version: _Optional[str] = ..., artifact_digest: _Optional[str] = ..., config_hash: _Optional[str] = ...) -> None: ...

class PipelineNode(_message.Message):
    __slots__ = ("node_id", "plugin", "consumes", "produces", "placement", "deadline_ms", "max_attempts", "priority")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    CONSUMES_FIELD_NUMBER: _ClassVar[int]
    PRODUCES_FIELD_NUMBER: _ClassVar[int]
    PLACEMENT_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_MS_FIELD_NUMBER: _ClassVar[int]
    MAX_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    plugin: PluginReference
    consumes: _containers.RepeatedScalarFieldContainer[str]
    produces: _containers.RepeatedScalarFieldContainer[str]
    placement: str
    deadline_ms: int
    max_attempts: int
    priority: int
    def __init__(self, node_id: _Optional[str] = ..., plugin: _Optional[_Union[PluginReference, _Mapping]] = ..., consumes: _Optional[_Iterable[str]] = ..., produces: _Optional[_Iterable[str]] = ..., placement: _Optional[str] = ..., deadline_ms: _Optional[int] = ..., max_attempts: _Optional[int] = ..., priority: _Optional[int] = ...) -> None: ...

class PipelineEdge(_message.Message):
    __slots__ = ("from_node_id", "to_node_id", "modality", "join_policy", "required")
    FROM_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    TO_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    MODALITY_FIELD_NUMBER: _ClassVar[int]
    JOIN_POLICY_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_FIELD_NUMBER: _ClassVar[int]
    from_node_id: str
    to_node_id: str
    modality: str
    join_policy: str
    required: bool
    def __init__(self, from_node_id: _Optional[str] = ..., to_node_id: _Optional[str] = ..., modality: _Optional[str] = ..., join_policy: _Optional[str] = ..., required: bool = ...) -> None: ...

class PipelineRevision(_message.Message):
    __slots__ = ("pipeline_id", "revision", "graph_digest", "nodes", "edges")
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    GRAPH_DIGEST_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    EDGES_FIELD_NUMBER: _ClassVar[int]
    pipeline_id: str
    revision: int
    graph_digest: str
    nodes: _containers.RepeatedCompositeFieldContainer[PipelineNode]
    edges: _containers.RepeatedCompositeFieldContainer[PipelineEdge]
    def __init__(self, pipeline_id: _Optional[str] = ..., revision: _Optional[int] = ..., graph_digest: _Optional[str] = ..., nodes: _Optional[_Iterable[_Union[PipelineNode, _Mapping]]] = ..., edges: _Optional[_Iterable[_Union[PipelineEdge, _Mapping]]] = ...) -> None: ...

class PipelineRun(_message.Message):
    __slots__ = ("run_id", "pipeline_id", "revision", "input_ref", "idempotency_key", "deadline_unix_ms", "state")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    INPUT_REF_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    pipeline_id: str
    revision: int
    input_ref: str
    idempotency_key: str
    deadline_unix_ms: int
    state: PipelineRunState
    def __init__(self, run_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., revision: _Optional[int] = ..., input_ref: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., deadline_unix_ms: _Optional[int] = ..., state: _Optional[_Union[PipelineRunState, str]] = ...) -> None: ...

class PipelineTask(_message.Message):
    __slots__ = ("task_id", "run_id", "node_id", "attempt", "idempotency_key", "state", "assignment_id", "reason_code")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    run_id: str
    node_id: str
    attempt: int
    idempotency_key: str
    state: PipelineTaskState
    assignment_id: str
    reason_code: str
    def __init__(self, task_id: _Optional[str] = ..., run_id: _Optional[str] = ..., node_id: _Optional[str] = ..., attempt: _Optional[int] = ..., idempotency_key: _Optional[str] = ..., state: _Optional[_Union[PipelineTaskState, str]] = ..., assignment_id: _Optional[str] = ..., reason_code: _Optional[str] = ...) -> None: ...

class SchedulerAssignment(_message.Message):
    __slots__ = ("assignment_id", "task_id", "requested_node_id", "actual_node_id", "data_plane_node_id", "decision", "reason_code", "lease_expires_at_unix_ms")
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    REQUESTED_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    DATA_PLANE_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    DECISION_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    LEASE_EXPIRES_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    assignment_id: str
    task_id: str
    requested_node_id: str
    actual_node_id: str
    data_plane_node_id: str
    decision: str
    reason_code: str
    lease_expires_at_unix_ms: int
    def __init__(self, assignment_id: _Optional[str] = ..., task_id: _Optional[str] = ..., requested_node_id: _Optional[str] = ..., actual_node_id: _Optional[str] = ..., data_plane_node_id: _Optional[str] = ..., decision: _Optional[str] = ..., reason_code: _Optional[str] = ..., lease_expires_at_unix_ms: _Optional[int] = ...) -> None: ...

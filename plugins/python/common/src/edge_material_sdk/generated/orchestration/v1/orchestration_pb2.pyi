from google.protobuf import struct_pb2 as _struct_pb2
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
    __slots__ = ("node_id", "plugin", "consumes", "produces", "placement", "deadline_ms", "max_attempts", "priority", "required")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    CONSUMES_FIELD_NUMBER: _ClassVar[int]
    PRODUCES_FIELD_NUMBER: _ClassVar[int]
    PLACEMENT_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_MS_FIELD_NUMBER: _ClassVar[int]
    MAX_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    plugin: PluginReference
    consumes: _containers.RepeatedScalarFieldContainer[str]
    produces: _containers.RepeatedScalarFieldContainer[str]
    placement: str
    deadline_ms: int
    max_attempts: int
    priority: int
    required: bool
    def __init__(self, node_id: _Optional[str] = ..., plugin: _Optional[_Union[PluginReference, _Mapping]] = ..., consumes: _Optional[_Iterable[str]] = ..., produces: _Optional[_Iterable[str]] = ..., placement: _Optional[str] = ..., deadline_ms: _Optional[int] = ..., max_attempts: _Optional[int] = ..., priority: _Optional[int] = ..., required: bool = ...) -> None: ...

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
    __slots__ = ("run_id", "pipeline_id", "revision", "input_ref", "idempotency_key", "deadline_unix_ms", "state", "owner", "error_code", "error_detail", "created_at_unix_ms", "updated_at_unix_ms", "completed_at_unix_ms")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    INPUT_REF_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    pipeline_id: str
    revision: int
    input_ref: str
    idempotency_key: str
    deadline_unix_ms: int
    state: PipelineRunState
    owner: str
    error_code: str
    error_detail: str
    created_at_unix_ms: int
    updated_at_unix_ms: int
    completed_at_unix_ms: int
    def __init__(self, run_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., revision: _Optional[int] = ..., input_ref: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., deadline_unix_ms: _Optional[int] = ..., state: _Optional[_Union[PipelineRunState, str]] = ..., owner: _Optional[str] = ..., error_code: _Optional[str] = ..., error_detail: _Optional[str] = ..., created_at_unix_ms: _Optional[int] = ..., updated_at_unix_ms: _Optional[int] = ..., completed_at_unix_ms: _Optional[int] = ...) -> None: ...

class PipelineTask(_message.Message):
    __slots__ = ("task_id", "run_id", "node_id", "attempt", "idempotency_key", "state", "assignment_id", "reason_code", "max_attempts", "error_detail", "output_ref", "created_at_unix_ms", "updated_at_unix_ms", "required")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    MAX_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_REF_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    run_id: str
    node_id: str
    attempt: int
    idempotency_key: str
    state: PipelineTaskState
    assignment_id: str
    reason_code: str
    max_attempts: int
    error_detail: str
    output_ref: str
    created_at_unix_ms: int
    updated_at_unix_ms: int
    required: bool
    def __init__(self, task_id: _Optional[str] = ..., run_id: _Optional[str] = ..., node_id: _Optional[str] = ..., attempt: _Optional[int] = ..., idempotency_key: _Optional[str] = ..., state: _Optional[_Union[PipelineTaskState, str]] = ..., assignment_id: _Optional[str] = ..., reason_code: _Optional[str] = ..., max_attempts: _Optional[int] = ..., error_detail: _Optional[str] = ..., output_ref: _Optional[str] = ..., created_at_unix_ms: _Optional[int] = ..., updated_at_unix_ms: _Optional[int] = ..., required: bool = ...) -> None: ...

class SchedulerAssignment(_message.Message):
    __slots__ = ("assignment_id", "task_id", "requested_node_id", "actual_node_id", "data_plane_node_id", "decision", "reason_code", "lease_expires_at_unix_ms", "run_id", "attempt", "created_at_unix_ms")
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    REQUESTED_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    DATA_PLANE_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    DECISION_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    LEASE_EXPIRES_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    assignment_id: str
    task_id: str
    requested_node_id: str
    actual_node_id: str
    data_plane_node_id: str
    decision: str
    reason_code: str
    lease_expires_at_unix_ms: int
    run_id: str
    attempt: int
    created_at_unix_ms: int
    def __init__(self, assignment_id: _Optional[str] = ..., task_id: _Optional[str] = ..., requested_node_id: _Optional[str] = ..., actual_node_id: _Optional[str] = ..., data_plane_node_id: _Optional[str] = ..., decision: _Optional[str] = ..., reason_code: _Optional[str] = ..., lease_expires_at_unix_ms: _Optional[int] = ..., run_id: _Optional[str] = ..., attempt: _Optional[int] = ..., created_at_unix_ms: _Optional[int] = ...) -> None: ...

class TaskInputManifest(_message.Message):
    __slots__ = ("run_id", "task_id", "attempt", "assignment_id", "data_plane_node_id", "descriptor_ref", "observation_refs", "stream_id", "start_ms", "end_ms", "content_hash")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    DATA_PLANE_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTOR_REF_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_REFS_FIELD_NUMBER: _ClassVar[int]
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    START_MS_FIELD_NUMBER: _ClassVar[int]
    END_MS_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    task_id: str
    attempt: int
    assignment_id: str
    data_plane_node_id: str
    descriptor_ref: str
    observation_refs: _containers.RepeatedScalarFieldContainer[str]
    stream_id: str
    start_ms: int
    end_ms: int
    content_hash: str
    def __init__(self, run_id: _Optional[str] = ..., task_id: _Optional[str] = ..., attempt: _Optional[int] = ..., assignment_id: _Optional[str] = ..., data_plane_node_id: _Optional[str] = ..., descriptor_ref: _Optional[str] = ..., observation_refs: _Optional[_Iterable[str]] = ..., stream_id: _Optional[str] = ..., start_ms: _Optional[int] = ..., end_ms: _Optional[int] = ..., content_hash: _Optional[str] = ...) -> None: ...

class TaskExecutionReceipt(_message.Message):
    __slots__ = ("run_id", "task_id", "attempt", "assignment_id", "plugin", "started_at_unix_ms", "completed_at_unix_ms", "input_count", "output_count", "result_manifest_ref", "reason_code", "receipt_digest")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    INPUT_COUNT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_COUNT_FIELD_NUMBER: _ClassVar[int]
    RESULT_MANIFEST_REF_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    RECEIPT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    task_id: str
    attempt: int
    assignment_id: str
    plugin: PluginReference
    started_at_unix_ms: int
    completed_at_unix_ms: int
    input_count: int
    output_count: int
    result_manifest_ref: str
    reason_code: str
    receipt_digest: str
    def __init__(self, run_id: _Optional[str] = ..., task_id: _Optional[str] = ..., attempt: _Optional[int] = ..., assignment_id: _Optional[str] = ..., plugin: _Optional[_Union[PluginReference, _Mapping]] = ..., started_at_unix_ms: _Optional[int] = ..., completed_at_unix_ms: _Optional[int] = ..., input_count: _Optional[int] = ..., output_count: _Optional[int] = ..., result_manifest_ref: _Optional[str] = ..., reason_code: _Optional[str] = ..., receipt_digest: _Optional[str] = ...) -> None: ...

class ValidatePipelineRevisionRequest(_message.Message):
    __slots__ = ("pipeline_id", "nodes", "edges")
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    EDGES_FIELD_NUMBER: _ClassVar[int]
    pipeline_id: str
    nodes: _containers.RepeatedCompositeFieldContainer[PipelineNode]
    edges: _containers.RepeatedCompositeFieldContainer[PipelineEdge]
    def __init__(self, pipeline_id: _Optional[str] = ..., nodes: _Optional[_Iterable[_Union[PipelineNode, _Mapping]]] = ..., edges: _Optional[_Iterable[_Union[PipelineEdge, _Mapping]]] = ...) -> None: ...

class ValidatePipelineRevisionResponse(_message.Message):
    __slots__ = ("valid", "errors", "graph_digest")
    VALID_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    GRAPH_DIGEST_FIELD_NUMBER: _ClassVar[int]
    valid: bool
    errors: _containers.RepeatedScalarFieldContainer[str]
    graph_digest: str
    def __init__(self, valid: bool = ..., errors: _Optional[_Iterable[str]] = ..., graph_digest: _Optional[str] = ...) -> None: ...

class PublishPipelineRevisionRequest(_message.Message):
    __slots__ = ("pipeline_id", "name", "description", "nodes", "edges")
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    EDGES_FIELD_NUMBER: _ClassVar[int]
    pipeline_id: str
    name: str
    description: str
    nodes: _containers.RepeatedCompositeFieldContainer[PipelineNode]
    edges: _containers.RepeatedCompositeFieldContainer[PipelineEdge]
    def __init__(self, pipeline_id: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., nodes: _Optional[_Iterable[_Union[PipelineNode, _Mapping]]] = ..., edges: _Optional[_Iterable[_Union[PipelineEdge, _Mapping]]] = ...) -> None: ...

class PublishPipelineRevisionResponse(_message.Message):
    __slots__ = ("revision",)
    REVISION_FIELD_NUMBER: _ClassVar[int]
    revision: PipelineRevision
    def __init__(self, revision: _Optional[_Union[PipelineRevision, _Mapping]] = ...) -> None: ...

class SubmitPipelineRunRequest(_message.Message):
    __slots__ = ("pipeline_id", "revision", "input_ref", "idempotency_key", "deadline_unix_ms")
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    INPUT_REF_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    pipeline_id: str
    revision: int
    input_ref: str
    idempotency_key: str
    deadline_unix_ms: int
    def __init__(self, pipeline_id: _Optional[str] = ..., revision: _Optional[int] = ..., input_ref: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., deadline_unix_ms: _Optional[int] = ...) -> None: ...

class SubmitPipelineRunResponse(_message.Message):
    __slots__ = ("run", "tasks", "is_duplicate")
    RUN_FIELD_NUMBER: _ClassVar[int]
    TASKS_FIELD_NUMBER: _ClassVar[int]
    IS_DUPLICATE_FIELD_NUMBER: _ClassVar[int]
    run: PipelineRun
    tasks: _containers.RepeatedCompositeFieldContainer[PipelineTask]
    is_duplicate: bool
    def __init__(self, run: _Optional[_Union[PipelineRun, _Mapping]] = ..., tasks: _Optional[_Iterable[_Union[PipelineTask, _Mapping]]] = ..., is_duplicate: bool = ...) -> None: ...

class GetPipelineRunRequest(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class GetPipelineRunResponse(_message.Message):
    __slots__ = ("run", "tasks", "assignments")
    RUN_FIELD_NUMBER: _ClassVar[int]
    TASKS_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENTS_FIELD_NUMBER: _ClassVar[int]
    run: PipelineRun
    tasks: _containers.RepeatedCompositeFieldContainer[PipelineTask]
    assignments: _containers.RepeatedCompositeFieldContainer[SchedulerAssignment]
    def __init__(self, run: _Optional[_Union[PipelineRun, _Mapping]] = ..., tasks: _Optional[_Iterable[_Union[PipelineTask, _Mapping]]] = ..., assignments: _Optional[_Iterable[_Union[SchedulerAssignment, _Mapping]]] = ...) -> None: ...

class CancelPipelineRunRequest(_message.Message):
    __slots__ = ("run_id", "reason")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    reason: str
    def __init__(self, run_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class CancelPipelineRunResponse(_message.Message):
    __slots__ = ("run", "cancelled_tasks")
    RUN_FIELD_NUMBER: _ClassVar[int]
    CANCELLED_TASKS_FIELD_NUMBER: _ClassVar[int]
    run: PipelineRun
    cancelled_tasks: _containers.RepeatedCompositeFieldContainer[PipelineTask]
    def __init__(self, run: _Optional[_Union[PipelineRun, _Mapping]] = ..., cancelled_tasks: _Optional[_Iterable[_Union[PipelineTask, _Mapping]]] = ...) -> None: ...

class RetryPipelineTaskRequest(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

class RetryPipelineTaskResponse(_message.Message):
    __slots__ = ("task",)
    TASK_FIELD_NUMBER: _ClassVar[int]
    task: PipelineTask
    def __init__(self, task: _Optional[_Union[PipelineTask, _Mapping]] = ...) -> None: ...

class ClaimTaskRequest(_message.Message):
    __slots__ = ("node_id", "supported_plugins", "max_tasks")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_PLUGINS_FIELD_NUMBER: _ClassVar[int]
    MAX_TASKS_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    supported_plugins: _containers.RepeatedScalarFieldContainer[str]
    max_tasks: int
    def __init__(self, node_id: _Optional[str] = ..., supported_plugins: _Optional[_Iterable[str]] = ..., max_tasks: _Optional[int] = ...) -> None: ...

class ClaimTaskResponse(_message.Message):
    __slots__ = ("tasks", "assignments")
    TASKS_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENTS_FIELD_NUMBER: _ClassVar[int]
    tasks: _containers.RepeatedCompositeFieldContainer[PipelineTask]
    assignments: _containers.RepeatedCompositeFieldContainer[SchedulerAssignment]
    def __init__(self, tasks: _Optional[_Iterable[_Union[PipelineTask, _Mapping]]] = ..., assignments: _Optional[_Iterable[_Union[SchedulerAssignment, _Mapping]]] = ...) -> None: ...

class TaskResult(_message.Message):
    __slots__ = ("task_id", "run_id", "attempt", "assignment_id", "success", "output_ref", "retryable", "reason_code", "error_detail", "result_manifest_ref", "receipt", "result_summary")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_REF_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    RESULT_MANIFEST_REF_FIELD_NUMBER: _ClassVar[int]
    RECEIPT_FIELD_NUMBER: _ClassVar[int]
    RESULT_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    run_id: str
    attempt: int
    assignment_id: str
    success: bool
    output_ref: str
    retryable: bool
    reason_code: str
    error_detail: str
    result_manifest_ref: str
    receipt: TaskExecutionReceipt
    result_summary: _struct_pb2.Struct
    def __init__(self, task_id: _Optional[str] = ..., run_id: _Optional[str] = ..., attempt: _Optional[int] = ..., assignment_id: _Optional[str] = ..., success: bool = ..., output_ref: _Optional[str] = ..., retryable: bool = ..., reason_code: _Optional[str] = ..., error_detail: _Optional[str] = ..., result_manifest_ref: _Optional[str] = ..., receipt: _Optional[_Union[TaskExecutionReceipt, _Mapping]] = ..., result_summary: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ReportTaskResultRequest(_message.Message):
    __slots__ = ("result",)
    RESULT_FIELD_NUMBER: _ClassVar[int]
    result: TaskResult
    def __init__(self, result: _Optional[_Union[TaskResult, _Mapping]] = ...) -> None: ...

class ReportTaskResultResponse(_message.Message):
    __slots__ = ("task", "unlocked_task_ids")
    TASK_FIELD_NUMBER: _ClassVar[int]
    UNLOCKED_TASK_IDS_FIELD_NUMBER: _ClassVar[int]
    task: PipelineTask
    unlocked_task_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, task: _Optional[_Union[PipelineTask, _Mapping]] = ..., unlocked_task_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class CancelTaskRequest(_message.Message):
    __slots__ = ("task_id", "reason")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    reason: str
    def __init__(self, task_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class CancelTaskResponse(_message.Message):
    __slots__ = ("task",)
    TASK_FIELD_NUMBER: _ClassVar[int]
    task: PipelineTask
    def __init__(self, task: _Optional[_Union[PipelineTask, _Mapping]] = ...) -> None: ...

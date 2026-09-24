from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as _runtime_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class NodeStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NODE_STATUS_UNSPECIFIED: _ClassVar[NodeStatus]
    NODE_STATUS_CANDIDATE: _ClassVar[NodeStatus]
    NODE_STATUS_ENROLLING: _ClassVar[NodeStatus]
    NODE_STATUS_READY: _ClassVar[NodeStatus]
    NODE_STATUS_DRAINING: _ClassVar[NodeStatus]
    NODE_STATUS_OFFLINE: _ClassVar[NodeStatus]
    NODE_STATUS_REVOKED: _ClassVar[NodeStatus]

class PluginInstanceState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLUGIN_INSTANCE_STATE_UNSPECIFIED: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_PLANNED: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_INSTALLING: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_READY: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_DEGRADED: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_DRAINING: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_STOPPED: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_FAILED: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_ROLLED_BACK: _ClassVar[PluginInstanceState]
    PLUGIN_INSTANCE_STATE_UNINSTALLED: _ClassVar[PluginInstanceState]

class DeploymentAction(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DEPLOYMENT_ACTION_UNSPECIFIED: _ClassVar[DeploymentAction]
    DEPLOYMENT_ACTION_INSTALL: _ClassVar[DeploymentAction]
    DEPLOYMENT_ACTION_START: _ClassVar[DeploymentAction]
    DEPLOYMENT_ACTION_STOP: _ClassVar[DeploymentAction]
    DEPLOYMENT_ACTION_UNINSTALL: _ClassVar[DeploymentAction]
    DEPLOYMENT_ACTION_ROLLBACK: _ClassVar[DeploymentAction]
    DEPLOYMENT_ACTION_DRAIN: _ClassVar[DeploymentAction]
NODE_STATUS_UNSPECIFIED: NodeStatus
NODE_STATUS_CANDIDATE: NodeStatus
NODE_STATUS_ENROLLING: NodeStatus
NODE_STATUS_READY: NodeStatus
NODE_STATUS_DRAINING: NodeStatus
NODE_STATUS_OFFLINE: NodeStatus
NODE_STATUS_REVOKED: NodeStatus
PLUGIN_INSTANCE_STATE_UNSPECIFIED: PluginInstanceState
PLUGIN_INSTANCE_STATE_PLANNED: PluginInstanceState
PLUGIN_INSTANCE_STATE_INSTALLING: PluginInstanceState
PLUGIN_INSTANCE_STATE_READY: PluginInstanceState
PLUGIN_INSTANCE_STATE_DEGRADED: PluginInstanceState
PLUGIN_INSTANCE_STATE_DRAINING: PluginInstanceState
PLUGIN_INSTANCE_STATE_STOPPED: PluginInstanceState
PLUGIN_INSTANCE_STATE_FAILED: PluginInstanceState
PLUGIN_INSTANCE_STATE_ROLLED_BACK: PluginInstanceState
PLUGIN_INSTANCE_STATE_UNINSTALLED: PluginInstanceState
DEPLOYMENT_ACTION_UNSPECIFIED: DeploymentAction
DEPLOYMENT_ACTION_INSTALL: DeploymentAction
DEPLOYMENT_ACTION_START: DeploymentAction
DEPLOYMENT_ACTION_STOP: DeploymentAction
DEPLOYMENT_ACTION_UNINSTALL: DeploymentAction
DEPLOYMENT_ACTION_ROLLBACK: DeploymentAction
DEPLOYMENT_ACTION_DRAIN: DeploymentAction

class NodeCapabilityProfile(_message.Message):
    __slots__ = ("platform", "arch", "cpu_cores", "memory_bytes", "unified_memory_bytes", "accelerators", "supported_artifacts", "labels")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    ARCH_FIELD_NUMBER: _ClassVar[int]
    CPU_CORES_FIELD_NUMBER: _ClassVar[int]
    MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    UNIFIED_MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    ACCELERATORS_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_ARTIFACTS_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    platform: str
    arch: str
    cpu_cores: int
    memory_bytes: int
    unified_memory_bytes: int
    accelerators: _containers.RepeatedCompositeFieldContainer[_runtime_pb2.HostAccelerator]
    supported_artifacts: _containers.RepeatedScalarFieldContainer[str]
    labels: _containers.ScalarMap[str, str]
    def __init__(self, platform: _Optional[str] = ..., arch: _Optional[str] = ..., cpu_cores: _Optional[int] = ..., memory_bytes: _Optional[int] = ..., unified_memory_bytes: _Optional[int] = ..., accelerators: _Optional[_Iterable[_Union[_runtime_pb2.HostAccelerator, _Mapping]]] = ..., supported_artifacts: _Optional[_Iterable[str]] = ..., labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class NodeInfo(_message.Message):
    __slots__ = ("node_id", "display_name", "status", "status_reason", "capabilities", "is_co_located", "last_heartbeat_at", "enrolled_at", "instances")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_REASON_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    IS_CO_LOCATED_FIELD_NUMBER: _ClassVar[int]
    LAST_HEARTBEAT_AT_FIELD_NUMBER: _ClassVar[int]
    ENROLLED_AT_FIELD_NUMBER: _ClassVar[int]
    INSTANCES_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    display_name: str
    status: NodeStatus
    status_reason: str
    capabilities: NodeCapabilityProfile
    is_co_located: bool
    last_heartbeat_at: str
    enrolled_at: str
    instances: _containers.RepeatedCompositeFieldContainer[PluginInstance]
    def __init__(self, node_id: _Optional[str] = ..., display_name: _Optional[str] = ..., status: _Optional[_Union[NodeStatus, str]] = ..., status_reason: _Optional[str] = ..., capabilities: _Optional[_Union[NodeCapabilityProfile, _Mapping]] = ..., is_co_located: bool = ..., last_heartbeat_at: _Optional[str] = ..., enrolled_at: _Optional[str] = ..., instances: _Optional[_Iterable[_Union[PluginInstance, _Mapping]]] = ...) -> None: ...

class NodeList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[NodeInfo]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[NodeInfo, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class PluginInstance(_message.Message):
    __slots__ = ("instance_id", "node_id", "plugin_id", "plugin_version", "artifact_digest", "previous_digest", "desired_state", "actual_state", "config_hash", "config", "error_code", "error_detail", "created_at", "updated_at")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_VERSION_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_DIGEST_FIELD_NUMBER: _ClassVar[int]
    DESIRED_STATE_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_STATE_FIELD_NUMBER: _ClassVar[int]
    CONFIG_HASH_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    node_id: str
    plugin_id: str
    plugin_version: str
    artifact_digest: str
    previous_digest: str
    desired_state: str
    actual_state: str
    config_hash: str
    config: _struct_pb2.Struct
    error_code: str
    error_detail: str
    created_at: str
    updated_at: str
    def __init__(self, instance_id: _Optional[str] = ..., node_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., plugin_version: _Optional[str] = ..., artifact_digest: _Optional[str] = ..., previous_digest: _Optional[str] = ..., desired_state: _Optional[str] = ..., actual_state: _Optional[str] = ..., config_hash: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., error_code: _Optional[str] = ..., error_detail: _Optional[str] = ..., created_at: _Optional[str] = ..., updated_at: _Optional[str] = ...) -> None: ...

class PluginInstanceList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PluginInstance]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[PluginInstance, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class EnrollmentToken(_message.Message):
    __slots__ = ("token", "node_id", "expires_at", "created_at")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    token: str
    node_id: str
    expires_at: str
    created_at: str
    def __init__(self, token: _Optional[str] = ..., node_id: _Optional[str] = ..., expires_at: _Optional[str] = ..., created_at: _Optional[str] = ...) -> None: ...

class CreateEnrollmentTokenRequest(_message.Message):
    __slots__ = ("node_id", "expires_in_minutes")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_IN_MINUTES_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    expires_in_minutes: int
    def __init__(self, node_id: _Optional[str] = ..., expires_in_minutes: _Optional[int] = ...) -> None: ...

class EnrollNodeRequest(_message.Message):
    __slots__ = ("enrollment_token", "node_id", "display_name", "is_co_located", "capabilities")
    ENROLLMENT_TOKEN_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    IS_CO_LOCATED_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    enrollment_token: str
    node_id: str
    display_name: str
    is_co_located: bool
    capabilities: NodeCapabilityProfile
    def __init__(self, enrollment_token: _Optional[str] = ..., node_id: _Optional[str] = ..., display_name: _Optional[str] = ..., is_co_located: bool = ..., capabilities: _Optional[_Union[NodeCapabilityProfile, _Mapping]] = ...) -> None: ...

class EnrollNodeResponse(_message.Message):
    __slots__ = ("success", "node_id", "status", "session_token", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    node_id: str
    status: NodeStatus
    session_token: str
    message: str
    def __init__(self, success: bool = ..., node_id: _Optional[str] = ..., status: _Optional[_Union[NodeStatus, str]] = ..., session_token: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class NodeHeartbeatRequest(_message.Message):
    __slots__ = ("node_id", "session_token", "timestamp_unix_ms", "available_memory_bytes", "current_concurrency", "running_instance_ids")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    CURRENT_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    RUNNING_INSTANCE_IDS_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    session_token: str
    timestamp_unix_ms: int
    available_memory_bytes: int
    current_concurrency: int
    running_instance_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, node_id: _Optional[str] = ..., session_token: _Optional[str] = ..., timestamp_unix_ms: _Optional[int] = ..., available_memory_bytes: _Optional[int] = ..., current_concurrency: _Optional[int] = ..., running_instance_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class NodeHeartbeatResponse(_message.Message):
    __slots__ = ("status", "heartbeat_interval_ms", "pending_intents")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    PENDING_INTENTS_FIELD_NUMBER: _ClassVar[int]
    status: NodeStatus
    heartbeat_interval_ms: int
    pending_intents: _containers.RepeatedCompositeFieldContainer[DeploymentIntent]
    def __init__(self, status: _Optional[_Union[NodeStatus, str]] = ..., heartbeat_interval_ms: _Optional[int] = ..., pending_intents: _Optional[_Iterable[_Union[DeploymentIntent, _Mapping]]] = ...) -> None: ...

class DeploymentIntent(_message.Message):
    __slots__ = ("intent_id", "instance_id", "node_id", "plugin_id", "plugin_version", "action", "artifact_digest", "rollback_digest", "config", "created_at", "deadline_unix_ms")
    INTENT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_VERSION_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    ROLLBACK_DIGEST_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    intent_id: str
    instance_id: str
    node_id: str
    plugin_id: str
    plugin_version: str
    action: DeploymentAction
    artifact_digest: str
    rollback_digest: str
    config: _struct_pb2.Struct
    created_at: str
    deadline_unix_ms: int
    def __init__(self, intent_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., node_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., plugin_version: _Optional[str] = ..., action: _Optional[_Union[DeploymentAction, str]] = ..., artifact_digest: _Optional[str] = ..., rollback_digest: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[str] = ..., deadline_unix_ms: _Optional[int] = ...) -> None: ...

class ReportDeploymentRequest(_message.Message):
    __slots__ = ("intent_id", "instance_id", "node_id", "action", "success", "actual_state", "error_code", "error_detail")
    INTENT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    intent_id: str
    instance_id: str
    node_id: str
    action: DeploymentAction
    success: bool
    actual_state: str
    error_code: str
    error_detail: str
    def __init__(self, intent_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., node_id: _Optional[str] = ..., action: _Optional[_Union[DeploymentAction, str]] = ..., success: bool = ..., actual_state: _Optional[str] = ..., error_code: _Optional[str] = ..., error_detail: _Optional[str] = ...) -> None: ...

class PreflightRequest(_message.Message):
    __slots__ = ("node_id", "plugin_id", "config_id", "requires_data_locality", "data_plane_node_id", "config")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_DATA_LOCALITY_FIELD_NUMBER: _ClassVar[int]
    DATA_PLANE_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    plugin_id: str
    config_id: str
    requires_data_locality: bool
    data_plane_node_id: str
    config: _struct_pb2.Struct
    def __init__(self, node_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., config_id: _Optional[str] = ..., requires_data_locality: bool = ..., data_plane_node_id: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PreflightResponse(_message.Message):
    __slots__ = ("eligible", "reason_code", "detail", "matched_capabilities", "missing_capabilities")
    ELIGIBLE_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    MATCHED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    MISSING_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    eligible: bool
    reason_code: str
    detail: str
    matched_capabilities: _containers.RepeatedScalarFieldContainer[str]
    missing_capabilities: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, eligible: bool = ..., reason_code: _Optional[str] = ..., detail: _Optional[str] = ..., matched_capabilities: _Optional[_Iterable[str]] = ..., missing_capabilities: _Optional[_Iterable[str]] = ...) -> None: ...

class TaskAssignment(_message.Message):
    __slots__ = ("assignment_id", "job_id", "target_node_id", "actual_node_id", "state", "reassignment_reason", "data_locality_checked", "deadline_unix_ms", "created_at")
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REASSIGNMENT_REASON_FIELD_NUMBER: _ClassVar[int]
    DATA_LOCALITY_CHECKED_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    assignment_id: str
    job_id: str
    target_node_id: str
    actual_node_id: str
    state: str
    reassignment_reason: str
    data_locality_checked: bool
    deadline_unix_ms: int
    created_at: str
    def __init__(self, assignment_id: _Optional[str] = ..., job_id: _Optional[str] = ..., target_node_id: _Optional[str] = ..., actual_node_id: _Optional[str] = ..., state: _Optional[str] = ..., reassignment_reason: _Optional[str] = ..., data_locality_checked: bool = ..., deadline_unix_ms: _Optional[int] = ..., created_at: _Optional[str] = ...) -> None: ...

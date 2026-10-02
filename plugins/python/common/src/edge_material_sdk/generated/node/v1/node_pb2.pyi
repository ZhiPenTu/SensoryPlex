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
    DEPLOYMENT_ACTION_TASK_PROCESS: _ClassVar[DeploymentAction]
    DEPLOYMENT_ACTION_STAGE_RELEASE: _ClassVar[DeploymentAction]
    DEPLOYMENT_ACTION_RECONCILE: _ClassVar[DeploymentAction]

class PluginOperationStage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLUGIN_OPERATION_STAGE_UNSPECIFIED: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_ACCEPTED: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_STAGING: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_STARTING: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_VALIDATING: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_CANDIDATE_READY: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_CUTTING_OVER: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_DRAINING_OLD: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_SUCCEEDED: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_FAILED: _ClassVar[PluginOperationStage]
    PLUGIN_OPERATION_STAGE_CANCELLED: _ClassVar[PluginOperationStage]

class PluginRuntimeRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLUGIN_RUNTIME_ROLE_UNSPECIFIED: _ClassVar[PluginRuntimeRole]
    PLUGIN_RUNTIME_ROLE_CANDIDATE: _ClassVar[PluginRuntimeRole]
    PLUGIN_RUNTIME_ROLE_ACTIVE: _ClassVar[PluginRuntimeRole]
    PLUGIN_RUNTIME_ROLE_PREVIOUS: _ClassVar[PluginRuntimeRole]
    PLUGIN_RUNTIME_ROLE_FAILED: _ClassVar[PluginRuntimeRole]

class PluginRuntimeState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLUGIN_RUNTIME_STATE_UNSPECIFIED: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_PLANNED: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_STAGED: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_STARTING: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_VALIDATING: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_CANDIDATE_READY: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_ACTIVE: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_DRAINING: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_STOPPED: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_FAILED: _ClassVar[PluginRuntimeState]
    PLUGIN_RUNTIME_STATE_UNINSTALLED: _ClassVar[PluginRuntimeState]
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
DEPLOYMENT_ACTION_TASK_PROCESS: DeploymentAction
DEPLOYMENT_ACTION_STAGE_RELEASE: DeploymentAction
DEPLOYMENT_ACTION_RECONCILE: DeploymentAction
PLUGIN_OPERATION_STAGE_UNSPECIFIED: PluginOperationStage
PLUGIN_OPERATION_STAGE_ACCEPTED: PluginOperationStage
PLUGIN_OPERATION_STAGE_STAGING: PluginOperationStage
PLUGIN_OPERATION_STAGE_STARTING: PluginOperationStage
PLUGIN_OPERATION_STAGE_VALIDATING: PluginOperationStage
PLUGIN_OPERATION_STAGE_CANDIDATE_READY: PluginOperationStage
PLUGIN_OPERATION_STAGE_CUTTING_OVER: PluginOperationStage
PLUGIN_OPERATION_STAGE_DRAINING_OLD: PluginOperationStage
PLUGIN_OPERATION_STAGE_SUCCEEDED: PluginOperationStage
PLUGIN_OPERATION_STAGE_FAILED: PluginOperationStage
PLUGIN_OPERATION_STAGE_CANCELLED: PluginOperationStage
PLUGIN_RUNTIME_ROLE_UNSPECIFIED: PluginRuntimeRole
PLUGIN_RUNTIME_ROLE_CANDIDATE: PluginRuntimeRole
PLUGIN_RUNTIME_ROLE_ACTIVE: PluginRuntimeRole
PLUGIN_RUNTIME_ROLE_PREVIOUS: PluginRuntimeRole
PLUGIN_RUNTIME_ROLE_FAILED: PluginRuntimeRole
PLUGIN_RUNTIME_STATE_UNSPECIFIED: PluginRuntimeState
PLUGIN_RUNTIME_STATE_PLANNED: PluginRuntimeState
PLUGIN_RUNTIME_STATE_STAGED: PluginRuntimeState
PLUGIN_RUNTIME_STATE_STARTING: PluginRuntimeState
PLUGIN_RUNTIME_STATE_VALIDATING: PluginRuntimeState
PLUGIN_RUNTIME_STATE_CANDIDATE_READY: PluginRuntimeState
PLUGIN_RUNTIME_STATE_ACTIVE: PluginRuntimeState
PLUGIN_RUNTIME_STATE_DRAINING: PluginRuntimeState
PLUGIN_RUNTIME_STATE_STOPPED: PluginRuntimeState
PLUGIN_RUNTIME_STATE_FAILED: PluginRuntimeState
PLUGIN_RUNTIME_STATE_UNINSTALLED: PluginRuntimeState

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
    __slots__ = ("instance_id", "node_id", "plugin_id", "plugin_version", "artifact_digest", "previous_digest", "desired_state", "actual_state", "config_hash", "config", "error_code", "error_detail", "created_at", "updated_at", "active_runtime_instance_id", "active_release_id", "previous_runtime_instance_id", "generation", "endpoint")
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
    ACTIVE_RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
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
    active_runtime_instance_id: str
    active_release_id: str
    previous_runtime_instance_id: str
    generation: int
    endpoint: str
    def __init__(self, instance_id: _Optional[str] = ..., node_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., plugin_version: _Optional[str] = ..., artifact_digest: _Optional[str] = ..., previous_digest: _Optional[str] = ..., desired_state: _Optional[str] = ..., actual_state: _Optional[str] = ..., config_hash: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., error_code: _Optional[str] = ..., error_detail: _Optional[str] = ..., created_at: _Optional[str] = ..., updated_at: _Optional[str] = ..., active_runtime_instance_id: _Optional[str] = ..., active_release_id: _Optional[str] = ..., previous_runtime_instance_id: _Optional[str] = ..., generation: _Optional[int] = ..., endpoint: _Optional[str] = ...) -> None: ...

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
    __slots__ = ("node_id", "session_token", "timestamp_unix_ms", "available_memory_bytes", "current_concurrency", "running_instance_ids", "runtime_observations")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    CURRENT_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    RUNNING_INSTANCE_IDS_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_OBSERVATIONS_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    session_token: str
    timestamp_unix_ms: int
    available_memory_bytes: int
    current_concurrency: int
    running_instance_ids: _containers.RepeatedScalarFieldContainer[str]
    runtime_observations: _containers.RepeatedCompositeFieldContainer[PluginRuntimeObservation]
    def __init__(self, node_id: _Optional[str] = ..., session_token: _Optional[str] = ..., timestamp_unix_ms: _Optional[int] = ..., available_memory_bytes: _Optional[int] = ..., current_concurrency: _Optional[int] = ..., running_instance_ids: _Optional[_Iterable[str]] = ..., runtime_observations: _Optional[_Iterable[_Union[PluginRuntimeObservation, _Mapping]]] = ...) -> None: ...

class PluginRuntimeObservation(_message.Message):
    __slots__ = ("runtime_instance_id", "operation_id", "generation", "observed_state", "endpoint", "supervisor_id", "supervisor_managed", "unit_loaded", "last_exit_code", "observed_at", "reconciliation", "detail")
    RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_STATE_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    SUPERVISOR_ID_FIELD_NUMBER: _ClassVar[int]
    SUPERVISOR_MANAGED_FIELD_NUMBER: _ClassVar[int]
    UNIT_LOADED_FIELD_NUMBER: _ClassVar[int]
    LAST_EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    RECONCILIATION_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    runtime_instance_id: str
    operation_id: str
    generation: int
    observed_state: str
    endpoint: str
    supervisor_id: str
    supervisor_managed: bool
    unit_loaded: bool
    last_exit_code: int
    observed_at: str
    reconciliation: str
    detail: str
    def __init__(self, runtime_instance_id: _Optional[str] = ..., operation_id: _Optional[str] = ..., generation: _Optional[int] = ..., observed_state: _Optional[str] = ..., endpoint: _Optional[str] = ..., supervisor_id: _Optional[str] = ..., supervisor_managed: bool = ..., unit_loaded: bool = ..., last_exit_code: _Optional[int] = ..., observed_at: _Optional[str] = ..., reconciliation: _Optional[str] = ..., detail: _Optional[str] = ...) -> None: ...

class NodeHeartbeatResponse(_message.Message):
    __slots__ = ("status", "heartbeat_interval_ms", "pending_intents", "reconciliation_required")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    PENDING_INTENTS_FIELD_NUMBER: _ClassVar[int]
    RECONCILIATION_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    status: NodeStatus
    heartbeat_interval_ms: int
    pending_intents: _containers.RepeatedCompositeFieldContainer[DeploymentIntent]
    reconciliation_required: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, status: _Optional[_Union[NodeStatus, str]] = ..., heartbeat_interval_ms: _Optional[int] = ..., pending_intents: _Optional[_Iterable[_Union[DeploymentIntent, _Mapping]]] = ..., reconciliation_required: _Optional[_Iterable[str]] = ...) -> None: ...

class DeploymentIntent(_message.Message):
    __slots__ = ("intent_id", "instance_id", "node_id", "plugin_id", "plugin_version", "action", "artifact_digest", "rollback_digest", "config", "created_at", "deadline_unix_ms", "operation_id", "generation", "release_id", "bundle_digest", "runtime_instance_id", "grace_period_ms", "config_hash")
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
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    BUNDLE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    GRACE_PERIOD_MS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_HASH_FIELD_NUMBER: _ClassVar[int]
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
    operation_id: str
    generation: int
    release_id: str
    bundle_digest: str
    runtime_instance_id: str
    grace_period_ms: int
    config_hash: str
    def __init__(self, intent_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., node_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., plugin_version: _Optional[str] = ..., action: _Optional[_Union[DeploymentAction, str]] = ..., artifact_digest: _Optional[str] = ..., rollback_digest: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[str] = ..., deadline_unix_ms: _Optional[int] = ..., operation_id: _Optional[str] = ..., generation: _Optional[int] = ..., release_id: _Optional[str] = ..., bundle_digest: _Optional[str] = ..., runtime_instance_id: _Optional[str] = ..., grace_period_ms: _Optional[int] = ..., config_hash: _Optional[str] = ...) -> None: ...

class ReportDeploymentRequest(_message.Message):
    __slots__ = ("intent_id", "instance_id", "node_id", "action", "success", "actual_state", "error_code", "error_detail", "operation_id", "generation", "release_id", "runtime_instance_id", "stage", "verified_plugin_id", "verified_artifact_digest", "endpoint", "supervisor_id", "staging_ms", "starting_ms", "validating_ms", "draining_ms")
    INTENT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    VERIFIED_PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    VERIFIED_ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    SUPERVISOR_ID_FIELD_NUMBER: _ClassVar[int]
    STAGING_MS_FIELD_NUMBER: _ClassVar[int]
    STARTING_MS_FIELD_NUMBER: _ClassVar[int]
    VALIDATING_MS_FIELD_NUMBER: _ClassVar[int]
    DRAINING_MS_FIELD_NUMBER: _ClassVar[int]
    intent_id: str
    instance_id: str
    node_id: str
    action: DeploymentAction
    success: bool
    actual_state: str
    error_code: str
    error_detail: str
    operation_id: str
    generation: int
    release_id: str
    runtime_instance_id: str
    stage: PluginOperationStage
    verified_plugin_id: str
    verified_artifact_digest: str
    endpoint: str
    supervisor_id: str
    staging_ms: int
    starting_ms: int
    validating_ms: int
    draining_ms: int
    def __init__(self, intent_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., node_id: _Optional[str] = ..., action: _Optional[_Union[DeploymentAction, str]] = ..., success: bool = ..., actual_state: _Optional[str] = ..., error_code: _Optional[str] = ..., error_detail: _Optional[str] = ..., operation_id: _Optional[str] = ..., generation: _Optional[int] = ..., release_id: _Optional[str] = ..., runtime_instance_id: _Optional[str] = ..., stage: _Optional[_Union[PluginOperationStage, str]] = ..., verified_plugin_id: _Optional[str] = ..., verified_artifact_digest: _Optional[str] = ..., endpoint: _Optional[str] = ..., supervisor_id: _Optional[str] = ..., staging_ms: _Optional[int] = ..., starting_ms: _Optional[int] = ..., validating_ms: _Optional[int] = ..., draining_ms: _Optional[int] = ...) -> None: ...

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

class DeregisterNodeRequest(_message.Message):
    __slots__ = ("node_id", "session_token", "reason")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_TOKEN_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    session_token: str
    reason: str
    def __init__(self, node_id: _Optional[str] = ..., session_token: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class DeregisterNodeResponse(_message.Message):
    __slots__ = ("success", "node_id", "status", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    node_id: str
    status: NodeStatus
    message: str
    def __init__(self, success: bool = ..., node_id: _Optional[str] = ..., status: _Optional[_Union[NodeStatus, str]] = ..., message: _Optional[str] = ...) -> None: ...

class PluginRelease(_message.Message):
    __slots__ = ("release_id", "plugin_id", "plugin_version", "platform", "arch", "form", "artifact_digest", "bundle_digest", "manifest_digest", "config_schema_digest", "sbom_digest", "bundle_bytes", "entrypoint", "runtime_requirements", "trust", "authenticated", "authentication_method", "signature_status", "sbom_components", "declared_memory_bytes", "declared_cpu_millicores", "default_deadline_ms", "published_at", "created_by")
    RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    ARCH_FIELD_NUMBER: _ClassVar[int]
    FORM_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    BUNDLE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_DIGEST_FIELD_NUMBER: _ClassVar[int]
    CONFIG_SCHEMA_DIGEST_FIELD_NUMBER: _ClassVar[int]
    SBOM_DIGEST_FIELD_NUMBER: _ClassVar[int]
    BUNDLE_BYTES_FIELD_NUMBER: _ClassVar[int]
    ENTRYPOINT_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_REQUIREMENTS_FIELD_NUMBER: _ClassVar[int]
    TRUST_FIELD_NUMBER: _ClassVar[int]
    AUTHENTICATED_FIELD_NUMBER: _ClassVar[int]
    AUTHENTICATION_METHOD_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_STATUS_FIELD_NUMBER: _ClassVar[int]
    SBOM_COMPONENTS_FIELD_NUMBER: _ClassVar[int]
    DECLARED_MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    DECLARED_CPU_MILLICORES_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_DEADLINE_MS_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_AT_FIELD_NUMBER: _ClassVar[int]
    CREATED_BY_FIELD_NUMBER: _ClassVar[int]
    release_id: str
    plugin_id: str
    plugin_version: str
    platform: str
    arch: str
    form: str
    artifact_digest: str
    bundle_digest: str
    manifest_digest: str
    config_schema_digest: str
    sbom_digest: str
    bundle_bytes: int
    entrypoint: _struct_pb2.Struct
    runtime_requirements: _struct_pb2.Struct
    trust: str
    authenticated: bool
    authentication_method: str
    signature_status: str
    sbom_components: int
    declared_memory_bytes: int
    declared_cpu_millicores: int
    default_deadline_ms: int
    published_at: str
    created_by: str
    def __init__(self, release_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., plugin_version: _Optional[str] = ..., platform: _Optional[str] = ..., arch: _Optional[str] = ..., form: _Optional[str] = ..., artifact_digest: _Optional[str] = ..., bundle_digest: _Optional[str] = ..., manifest_digest: _Optional[str] = ..., config_schema_digest: _Optional[str] = ..., sbom_digest: _Optional[str] = ..., bundle_bytes: _Optional[int] = ..., entrypoint: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., runtime_requirements: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., trust: _Optional[str] = ..., authenticated: bool = ..., authentication_method: _Optional[str] = ..., signature_status: _Optional[str] = ..., sbom_components: _Optional[int] = ..., declared_memory_bytes: _Optional[int] = ..., declared_cpu_millicores: _Optional[int] = ..., default_deadline_ms: _Optional[int] = ..., published_at: _Optional[str] = ..., created_by: _Optional[str] = ...) -> None: ...

class PluginReleaseList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PluginRelease]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[PluginRelease, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class PluginRuntimeInstance(_message.Message):
    __slots__ = ("runtime_instance_id", "instance_id", "node_id", "plugin_id", "release_id", "artifact_digest", "bundle_digest", "generation", "role", "state", "endpoint", "supervisor_id", "unit_name", "install_dir", "verified_plugin_id", "verified_artifact_digest", "launch_ms", "drain_ms", "error_code", "error_detail", "created_at", "updated_at")
    RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    BUNDLE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    SUPERVISOR_ID_FIELD_NUMBER: _ClassVar[int]
    UNIT_NAME_FIELD_NUMBER: _ClassVar[int]
    INSTALL_DIR_FIELD_NUMBER: _ClassVar[int]
    VERIFIED_PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    VERIFIED_ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    LAUNCH_MS_FIELD_NUMBER: _ClassVar[int]
    DRAIN_MS_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    runtime_instance_id: str
    instance_id: str
    node_id: str
    plugin_id: str
    release_id: str
    artifact_digest: str
    bundle_digest: str
    generation: int
    role: PluginRuntimeRole
    state: PluginRuntimeState
    endpoint: str
    supervisor_id: str
    unit_name: str
    install_dir: str
    verified_plugin_id: str
    verified_artifact_digest: str
    launch_ms: int
    drain_ms: int
    error_code: str
    error_detail: str
    created_at: str
    updated_at: str
    def __init__(self, runtime_instance_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., node_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., release_id: _Optional[str] = ..., artifact_digest: _Optional[str] = ..., bundle_digest: _Optional[str] = ..., generation: _Optional[int] = ..., role: _Optional[_Union[PluginRuntimeRole, str]] = ..., state: _Optional[_Union[PluginRuntimeState, str]] = ..., endpoint: _Optional[str] = ..., supervisor_id: _Optional[str] = ..., unit_name: _Optional[str] = ..., install_dir: _Optional[str] = ..., verified_plugin_id: _Optional[str] = ..., verified_artifact_digest: _Optional[str] = ..., launch_ms: _Optional[int] = ..., drain_ms: _Optional[int] = ..., error_code: _Optional[str] = ..., error_detail: _Optional[str] = ..., created_at: _Optional[str] = ..., updated_at: _Optional[str] = ...) -> None: ...

class PluginDeploymentOperation(_message.Message):
    __slots__ = ("operation_id", "kind", "node_id", "instance_id", "plugin_id", "release_id", "from_runtime_instance_id", "candidate_runtime_instance_id", "rollback_of_operation_id", "generation", "stage", "cancellable", "deadline_unix_ms", "error_code", "error_detail", "staging_ms", "starting_ms", "validating_ms", "draining_ms", "config", "candidate", "active", "previous", "created_by", "created_at", "updated_at", "completed_at")
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    FROM_RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    ROLLBACK_OF_OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    CANCELLABLE_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    STAGING_MS_FIELD_NUMBER: _ClassVar[int]
    STARTING_MS_FIELD_NUMBER: _ClassVar[int]
    VALIDATING_MS_FIELD_NUMBER: _ClassVar[int]
    DRAINING_MS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_BY_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    kind: str
    node_id: str
    instance_id: str
    plugin_id: str
    release_id: str
    from_runtime_instance_id: str
    candidate_runtime_instance_id: str
    rollback_of_operation_id: str
    generation: int
    stage: PluginOperationStage
    cancellable: bool
    deadline_unix_ms: int
    error_code: str
    error_detail: str
    staging_ms: int
    starting_ms: int
    validating_ms: int
    draining_ms: int
    config: _struct_pb2.Struct
    candidate: PluginRuntimeInstance
    active: PluginRuntimeInstance
    previous: PluginRuntimeInstance
    created_by: str
    created_at: str
    updated_at: str
    completed_at: str
    def __init__(self, operation_id: _Optional[str] = ..., kind: _Optional[str] = ..., node_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., release_id: _Optional[str] = ..., from_runtime_instance_id: _Optional[str] = ..., candidate_runtime_instance_id: _Optional[str] = ..., rollback_of_operation_id: _Optional[str] = ..., generation: _Optional[int] = ..., stage: _Optional[_Union[PluginOperationStage, str]] = ..., cancellable: bool = ..., deadline_unix_ms: _Optional[int] = ..., error_code: _Optional[str] = ..., error_detail: _Optional[str] = ..., staging_ms: _Optional[int] = ..., starting_ms: _Optional[int] = ..., validating_ms: _Optional[int] = ..., draining_ms: _Optional[int] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., candidate: _Optional[_Union[PluginRuntimeInstance, _Mapping]] = ..., active: _Optional[_Union[PluginRuntimeInstance, _Mapping]] = ..., previous: _Optional[_Union[PluginRuntimeInstance, _Mapping]] = ..., created_by: _Optional[str] = ..., created_at: _Optional[str] = ..., updated_at: _Optional[str] = ..., completed_at: _Optional[str] = ...) -> None: ...

class PluginDeploymentOperationList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PluginDeploymentOperation]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[PluginDeploymentOperation, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class CreatePluginDeploymentRequest(_message.Message):
    __slots__ = ("release_id", "config_id", "config")
    RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    release_id: str
    config_id: str
    config: _struct_pb2.Struct
    def __init__(self, release_id: _Optional[str] = ..., config_id: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class BatchDeployPluginsRequest(_message.Message):
    __slots__ = ("plugin_ids",)
    PLUGIN_IDS_FIELD_NUMBER: _ClassVar[int]
    plugin_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, plugin_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class PluginDeploymentRejection(_message.Message):
    __slots__ = ("plugin_id", "reason")
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    plugin_id: str
    reason: str
    def __init__(self, plugin_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class BatchDeployPluginsResponse(_message.Message):
    __slots__ = ("node_id", "operations", "already_ready", "rejected")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATIONS_FIELD_NUMBER: _ClassVar[int]
    ALREADY_READY_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    operations: _containers.RepeatedCompositeFieldContainer[PluginDeploymentOperation]
    already_ready: _containers.RepeatedScalarFieldContainer[str]
    rejected: _containers.RepeatedCompositeFieldContainer[PluginDeploymentRejection]
    def __init__(self, node_id: _Optional[str] = ..., operations: _Optional[_Iterable[_Union[PluginDeploymentOperation, _Mapping]]] = ..., already_ready: _Optional[_Iterable[str]] = ..., rejected: _Optional[_Iterable[_Union[PluginDeploymentRejection, _Mapping]]] = ...) -> None: ...

class SyncPluginReleasesRequest(_message.Message):
    __slots__ = ("dry_run",)
    DRY_RUN_FIELD_NUMBER: _ClassVar[int]
    dry_run: bool
    def __init__(self, dry_run: bool = ...) -> None: ...

class SyncPluginReleasesResponse(_message.Message):
    __slots__ = ("imported", "unchanged", "rejected", "total")
    IMPORTED_FIELD_NUMBER: _ClassVar[int]
    UNCHANGED_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    imported: _containers.RepeatedScalarFieldContainer[str]
    unchanged: _containers.RepeatedScalarFieldContainer[str]
    rejected: _containers.RepeatedScalarFieldContainer[str]
    total: int
    def __init__(self, imported: _Optional[_Iterable[str]] = ..., unchanged: _Optional[_Iterable[str]] = ..., rejected: _Optional[_Iterable[str]] = ..., total: _Optional[int] = ...) -> None: ...

class PluginSigner(_message.Message):
    __slots__ = ("signer_id", "display_name", "public_key", "created_by", "created_at", "revoked_at")
    SIGNER_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    CREATED_BY_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    REVOKED_AT_FIELD_NUMBER: _ClassVar[int]
    signer_id: str
    display_name: str
    public_key: str
    created_by: str
    created_at: str
    revoked_at: str
    def __init__(self, signer_id: _Optional[str] = ..., display_name: _Optional[str] = ..., public_key: _Optional[str] = ..., created_by: _Optional[str] = ..., created_at: _Optional[str] = ..., revoked_at: _Optional[str] = ...) -> None: ...

class PluginSignerList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PluginSigner]
    def __init__(self, items: _Optional[_Iterable[_Union[PluginSigner, _Mapping]]] = ...) -> None: ...

class ApprovePluginSignerRequest(_message.Message):
    __slots__ = ("display_name", "public_key")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    public_key: str
    def __init__(self, display_name: _Optional[str] = ..., public_key: _Optional[str] = ...) -> None: ...

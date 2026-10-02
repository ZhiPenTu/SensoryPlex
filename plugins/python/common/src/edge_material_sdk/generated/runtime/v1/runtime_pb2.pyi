from edge_material_sdk.generated.common.v1 import common_pb2 as _common_pb2
from edge_material_sdk.generated.material.v1 import material_pb2 as _material_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ProcessOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROCESS_OUTCOME_UNSPECIFIED: _ClassVar[ProcessOutcome]
    PROCESS_OUTCOME_OBSERVED: _ClassVar[ProcessOutcome]
    PROCESS_OUTCOME_NO_OBSERVATIONS: _ClassVar[ProcessOutcome]

class CapabilityState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAPABILITY_STATE_UNSPECIFIED: _ClassVar[CapabilityState]
    CAPABILITY_STATE_AVAILABLE: _ClassVar[CapabilityState]
    CAPABILITY_STATE_UNAVAILABLE: _ClassVar[CapabilityState]

class AcceleratorState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACCELERATOR_STATE_UNSPECIFIED: _ClassVar[AcceleratorState]
    ACCELERATOR_STATE_AVAILABLE: _ClassVar[AcceleratorState]
    ACCELERATOR_STATE_UNAVAILABLE: _ClassVar[AcceleratorState]
    ACCELERATOR_STATE_UNKNOWN: _ClassVar[AcceleratorState]
PROCESS_OUTCOME_UNSPECIFIED: ProcessOutcome
PROCESS_OUTCOME_OBSERVED: ProcessOutcome
PROCESS_OUTCOME_NO_OBSERVATIONS: ProcessOutcome
CAPABILITY_STATE_UNSPECIFIED: CapabilityState
CAPABILITY_STATE_AVAILABLE: CapabilityState
CAPABILITY_STATE_UNAVAILABLE: CapabilityState
ACCELERATOR_STATE_UNSPECIFIED: AcceleratorState
ACCELERATOR_STATE_AVAILABLE: AcceleratorState
ACCELERATOR_STATE_UNAVAILABLE: AcceleratorState
ACCELERATOR_STATE_UNKNOWN: AcceleratorState

class DescribeRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PayloadContract(_message.Message):
    __slots__ = ("modality", "schema_id", "schema_version", "schema_digest")
    MODALITY_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_ID_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_DIGEST_FIELD_NUMBER: _ClassVar[int]
    modality: str
    schema_id: str
    schema_version: str
    schema_digest: str
    def __init__(self, modality: _Optional[str] = ..., schema_id: _Optional[str] = ..., schema_version: _Optional[str] = ..., schema_digest: _Optional[str] = ...) -> None: ...

class PluginDescription(_message.Message):
    __slots__ = ("name", "version", "protocol", "consumes", "produces", "memory_kinds", "artifact_digest", "input_contracts", "output_contracts", "execution_modes", "max_concurrency", "max_batch_size", "ordering")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    CONSUMES_FIELD_NUMBER: _ClassVar[int]
    PRODUCES_FIELD_NUMBER: _ClassVar[int]
    MEMORY_KINDS_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    INPUT_CONTRACTS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_CONTRACTS_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_MODES_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    MAX_BATCH_SIZE_FIELD_NUMBER: _ClassVar[int]
    ORDERING_FIELD_NUMBER: _ClassVar[int]
    name: str
    version: str
    protocol: str
    consumes: _containers.RepeatedScalarFieldContainer[str]
    produces: _containers.RepeatedScalarFieldContainer[str]
    memory_kinds: _containers.RepeatedScalarFieldContainer[str]
    artifact_digest: str
    input_contracts: _containers.RepeatedCompositeFieldContainer[PayloadContract]
    output_contracts: _containers.RepeatedCompositeFieldContainer[PayloadContract]
    execution_modes: _containers.RepeatedScalarFieldContainer[str]
    max_concurrency: int
    max_batch_size: int
    ordering: str
    def __init__(self, name: _Optional[str] = ..., version: _Optional[str] = ..., protocol: _Optional[str] = ..., consumes: _Optional[_Iterable[str]] = ..., produces: _Optional[_Iterable[str]] = ..., memory_kinds: _Optional[_Iterable[str]] = ..., artifact_digest: _Optional[str] = ..., input_contracts: _Optional[_Iterable[_Union[PayloadContract, _Mapping]]] = ..., output_contracts: _Optional[_Iterable[_Union[PayloadContract, _Mapping]]] = ..., execution_modes: _Optional[_Iterable[str]] = ..., max_concurrency: _Optional[int] = ..., max_batch_size: _Optional[int] = ..., ordering: _Optional[str] = ...) -> None: ...

class ValidateConfigRequest(_message.Message):
    __slots__ = ("config",)
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    config: _struct_pb2.Struct
    def __init__(self, config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ValidationResult(_message.Message):
    __slots__ = ("valid", "field_errors")
    VALID_FIELD_NUMBER: _ClassVar[int]
    FIELD_ERRORS_FIELD_NUMBER: _ClassVar[int]
    valid: bool
    field_errors: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, valid: bool = ..., field_errors: _Optional[_Iterable[str]] = ...) -> None: ...

class StartRequest(_message.Message):
    __slots__ = ("config",)
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    config: _struct_pb2.Struct
    def __init__(self, config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class LifecycleResponse(_message.Message):
    __slots__ = ("state", "error")
    STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    state: str
    error: _common_pb2.ProcessingError
    def __init__(self, state: _Optional[str] = ..., error: _Optional[_Union[_common_pb2.ProcessingError, _Mapping]] = ...) -> None: ...

class PluginInput(_message.Message):
    __slots__ = ("buffer", "observation")
    BUFFER_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_FIELD_NUMBER: _ClassVar[int]
    buffer: _common_pb2.BufferDescriptor
    observation: _material_pb2.Observation
    def __init__(self, buffer: _Optional[_Union[_common_pb2.BufferDescriptor, _Mapping]] = ..., observation: _Optional[_Union[_material_pb2.Observation, _Mapping]] = ...) -> None: ...

class ProcessRequest(_message.Message):
    __slots__ = ("context", "inputs", "processor_release_id")
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    PROCESSOR_RELEASE_ID_FIELD_NUMBER: _ClassVar[int]
    context: _common_pb2.RequestContext
    inputs: _containers.RepeatedCompositeFieldContainer[PluginInput]
    processor_release_id: str
    def __init__(self, context: _Optional[_Union[_common_pb2.RequestContext, _Mapping]] = ..., inputs: _Optional[_Iterable[_Union[PluginInput, _Mapping]]] = ..., processor_release_id: _Optional[str] = ...) -> None: ...

class ProcessResponse(_message.Message):
    __slots__ = ("observations", "warnings", "error", "outcome", "outcome_reason")
    OBSERVATIONS_FIELD_NUMBER: _ClassVar[int]
    WARNINGS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_REASON_FIELD_NUMBER: _ClassVar[int]
    observations: _containers.RepeatedCompositeFieldContainer[_material_pb2.Observation]
    warnings: _containers.RepeatedScalarFieldContainer[str]
    error: _common_pb2.ProcessingError
    outcome: ProcessOutcome
    outcome_reason: str
    def __init__(self, observations: _Optional[_Iterable[_Union[_material_pb2.Observation, _Mapping]]] = ..., warnings: _Optional[_Iterable[str]] = ..., error: _Optional[_Union[_common_pb2.ProcessingError, _Mapping]] = ..., outcome: _Optional[_Union[ProcessOutcome, str]] = ..., outcome_reason: _Optional[str] = ...) -> None: ...

class CancelRequest(_message.Message):
    __slots__ = ("request_id",)
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    def __init__(self, request_id: _Optional[str] = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HealthResponse(_message.Message):
    __slots__ = ("state", "unavailable_capabilities")
    STATE_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    state: str
    unavailable_capabilities: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, state: _Optional[str] = ..., unavailable_capabilities: _Optional[_Iterable[str]] = ...) -> None: ...

class DrainRequest(_message.Message):
    __slots__ = ("grace_period_ms",)
    GRACE_PERIOD_MS_FIELD_NUMBER: _ClassVar[int]
    grace_period_ms: int
    def __init__(self, grace_period_ms: _Optional[int] = ...) -> None: ...

class StopRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HostResources(_message.Message):
    __slots__ = ("unified_memory_bytes", "total_memory_bytes", "logical_cores")
    UNIFIED_MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    TOTAL_MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    LOGICAL_CORES_FIELD_NUMBER: _ClassVar[int]
    unified_memory_bytes: int
    total_memory_bytes: int
    logical_cores: int
    def __init__(self, unified_memory_bytes: _Optional[int] = ..., total_memory_bytes: _Optional[int] = ..., logical_cores: _Optional[int] = ...) -> None: ...

class BackendCapability(_message.Message):
    __slots__ = ("backend", "platform", "runtime_version", "precisions", "memory_kinds", "max_concurrency", "state", "unavailable_reason")
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_VERSION_FIELD_NUMBER: _ClassVar[int]
    PRECISIONS_FIELD_NUMBER: _ClassVar[int]
    MEMORY_KINDS_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_REASON_FIELD_NUMBER: _ClassVar[int]
    backend: str
    platform: str
    runtime_version: str
    precisions: _containers.RepeatedScalarFieldContainer[str]
    memory_kinds: _containers.RepeatedScalarFieldContainer[str]
    max_concurrency: int
    state: CapabilityState
    unavailable_reason: str
    def __init__(self, backend: _Optional[str] = ..., platform: _Optional[str] = ..., runtime_version: _Optional[str] = ..., precisions: _Optional[_Iterable[str]] = ..., memory_kinds: _Optional[_Iterable[str]] = ..., max_concurrency: _Optional[int] = ..., state: _Optional[_Union[CapabilityState, str]] = ..., unavailable_reason: _Optional[str] = ...) -> None: ...

class HostAccelerator(_message.Message):
    __slots__ = ("accelerator", "platform", "state", "detection_source", "runtime_version", "evidence", "unavailable_reason")
    ACCELERATOR_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    DETECTION_SOURCE_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_VERSION_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_REASON_FIELD_NUMBER: _ClassVar[int]
    accelerator: str
    platform: str
    state: AcceleratorState
    detection_source: str
    runtime_version: str
    evidence: _containers.RepeatedScalarFieldContainer[str]
    unavailable_reason: str
    def __init__(self, accelerator: _Optional[str] = ..., platform: _Optional[str] = ..., state: _Optional[_Union[AcceleratorState, str]] = ..., detection_source: _Optional[str] = ..., runtime_version: _Optional[str] = ..., evidence: _Optional[_Iterable[str]] = ..., unavailable_reason: _Optional[str] = ...) -> None: ...

class ResidencyLimits(_message.Message):
    __slots__ = ("tier", "media_queue_capacity", "model_parallelism")
    TIER_FIELD_NUMBER: _ClassVar[int]
    MEDIA_QUEUE_CAPACITY_FIELD_NUMBER: _ClassVar[int]
    MODEL_PARALLELISM_FIELD_NUMBER: _ClassVar[int]
    tier: str
    media_queue_capacity: int
    model_parallelism: int
    def __init__(self, tier: _Optional[str] = ..., media_queue_capacity: _Optional[int] = ..., model_parallelism: _Optional[int] = ...) -> None: ...

class DescribeCapabilitiesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DescribeCapabilitiesResponse(_message.Message):
    __slots__ = ("platform", "host", "backends", "unavailable_capabilities", "admitted_memory_kinds", "host_accelerators", "residency")
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    HOST_FIELD_NUMBER: _ClassVar[int]
    BACKENDS_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    ADMITTED_MEMORY_KINDS_FIELD_NUMBER: _ClassVar[int]
    HOST_ACCELERATORS_FIELD_NUMBER: _ClassVar[int]
    RESIDENCY_FIELD_NUMBER: _ClassVar[int]
    platform: str
    host: HostResources
    backends: _containers.RepeatedCompositeFieldContainer[BackendCapability]
    unavailable_capabilities: _containers.RepeatedScalarFieldContainer[str]
    admitted_memory_kinds: _containers.RepeatedScalarFieldContainer[str]
    host_accelerators: _containers.RepeatedCompositeFieldContainer[HostAccelerator]
    residency: ResidencyLimits
    def __init__(self, platform: _Optional[str] = ..., host: _Optional[_Union[HostResources, _Mapping]] = ..., backends: _Optional[_Iterable[_Union[BackendCapability, _Mapping]]] = ..., unavailable_capabilities: _Optional[_Iterable[str]] = ..., admitted_memory_kinds: _Optional[_Iterable[str]] = ..., host_accelerators: _Optional[_Iterable[_Union[HostAccelerator, _Mapping]]] = ..., residency: _Optional[_Union[ResidencyLimits, _Mapping]] = ...) -> None: ...

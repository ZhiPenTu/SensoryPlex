from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class LoginRequest(_message.Message):
    __slots__ = ("username", "password")
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    username: str
    password: str
    def __init__(self, username: _Optional[str] = ..., password: _Optional[str] = ...) -> None: ...

class DemoAccount(_message.Message):
    __slots__ = ("enabled", "username", "password")
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    username: str
    password: str
    def __init__(self, enabled: bool = ..., username: _Optional[str] = ..., password: _Optional[str] = ...) -> None: ...

class Identity(_message.Message):
    __slots__ = ("principal", "display_name", "roles", "permissions", "csrf_token")
    PRINCIPAL_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ROLES_FIELD_NUMBER: _ClassVar[int]
    PERMISSIONS_FIELD_NUMBER: _ClassVar[int]
    CSRF_TOKEN_FIELD_NUMBER: _ClassVar[int]
    principal: str
    display_name: str
    roles: _containers.RepeatedScalarFieldContainer[str]
    permissions: _containers.RepeatedScalarFieldContainer[str]
    csrf_token: str
    def __init__(self, principal: _Optional[str] = ..., display_name: _Optional[str] = ..., roles: _Optional[_Iterable[str]] = ..., permissions: _Optional[_Iterable[str]] = ..., csrf_token: _Optional[str] = ...) -> None: ...

class ApiError(_message.Message):
    __slots__ = ("detail", "reason_code", "trace_id", "retryable")
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    detail: str
    reason_code: str
    trace_id: str
    retryable: bool
    def __init__(self, detail: _Optional[str] = ..., reason_code: _Optional[str] = ..., trace_id: _Optional[str] = ..., retryable: bool = ...) -> None: ...

class Capability(_message.Message):
    __slots__ = ("name", "available", "reason")
    NAME_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    name: str
    available: bool
    reason: str
    def __init__(self, name: _Optional[str] = ..., available: bool = ..., reason: _Optional[str] = ...) -> None: ...

class ConsoleStatus(_message.Message):
    __slots__ = ("capabilities", "schema_version")
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    capabilities: _containers.RepeatedCompositeFieldContainer[Capability]
    schema_version: str
    def __init__(self, capabilities: _Optional[_Iterable[_Union[Capability, _Mapping]]] = ..., schema_version: _Optional[str] = ...) -> None: ...

class CreateUpload(_message.Message):
    __slots__ = ("filename", "size_bytes", "content_type")
    FILENAME_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    filename: str
    size_bytes: int
    content_type: str
    def __init__(self, filename: _Optional[str] = ..., size_bytes: _Optional[int] = ..., content_type: _Optional[str] = ...) -> None: ...

class Upload(_message.Message):
    __slots__ = ("id", "filename", "size_bytes", "content_type", "state", "sha256", "created_at", "reason")
    ID_FIELD_NUMBER: _ClassVar[int]
    FILENAME_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    id: str
    filename: str
    size_bytes: int
    content_type: str
    state: str
    sha256: str
    created_at: str
    reason: str
    def __init__(self, id: _Optional[str] = ..., filename: _Optional[str] = ..., size_bytes: _Optional[int] = ..., content_type: _Optional[str] = ..., state: _Optional[str] = ..., sha256: _Optional[str] = ..., created_at: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class UploadList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[Upload]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[Upload, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class PluginEntry(_message.Message):
    __slots__ = ("id", "name", "version", "description", "digest", "trust", "state", "consumes", "produces", "config_schema", "reason")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    TRUST_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    CONSUMES_FIELD_NUMBER: _ClassVar[int]
    PRODUCES_FIELD_NUMBER: _ClassVar[int]
    CONFIG_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    version: str
    description: str
    digest: str
    trust: str
    state: str
    consumes: _containers.RepeatedScalarFieldContainer[str]
    produces: _containers.RepeatedScalarFieldContainer[str]
    config_schema: _struct_pb2.Struct
    reason: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., version: _Optional[str] = ..., description: _Optional[str] = ..., digest: _Optional[str] = ..., trust: _Optional[str] = ..., state: _Optional[str] = ..., consumes: _Optional[_Iterable[str]] = ..., produces: _Optional[_Iterable[str]] = ..., config_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., reason: _Optional[str] = ...) -> None: ...

class PluginList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PluginEntry]
    def __init__(self, items: _Optional[_Iterable[_Union[PluginEntry, _Mapping]]] = ...) -> None: ...

class SavePluginConfig(_message.Message):
    __slots__ = ("plugin_id", "name", "config")
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    plugin_id: str
    name: str
    config: _struct_pb2.Struct
    def __init__(self, plugin_id: _Optional[str] = ..., name: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PluginConfig(_message.Message):
    __slots__ = ("id", "plugin_id", "name", "revision", "config", "config_hash", "created_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    CONFIG_HASH_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    plugin_id: str
    name: str
    revision: int
    config: _struct_pb2.Struct
    config_hash: str
    created_at: str
    def __init__(self, id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., name: _Optional[str] = ..., revision: _Optional[int] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., config_hash: _Optional[str] = ..., created_at: _Optional[str] = ...) -> None: ...

class PluginConfigList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PluginConfig]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[PluginConfig, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class SavePipeline(_message.Message):
    __slots__ = ("name", "description", "plugin_id", "config_id")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    plugin_id: str
    config_id: str
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., plugin_id: _Optional[str] = ..., config_id: _Optional[str] = ...) -> None: ...

class Pipeline(_message.Message):
    __slots__ = ("id", "name", "description", "plugin_id", "config_id", "state", "revision", "created_at", "plugin_digest", "execution_mode", "orchestration_pipeline_id", "orchestration_revision", "graph_digest")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_DIGEST_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_MODE_FIELD_NUMBER: _ClassVar[int]
    ORCHESTRATION_PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    ORCHESTRATION_REVISION_FIELD_NUMBER: _ClassVar[int]
    GRAPH_DIGEST_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    description: str
    plugin_id: str
    config_id: str
    state: str
    revision: int
    created_at: str
    plugin_digest: str
    execution_mode: str
    orchestration_pipeline_id: str
    orchestration_revision: int
    graph_digest: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., plugin_id: _Optional[str] = ..., config_id: _Optional[str] = ..., state: _Optional[str] = ..., revision: _Optional[int] = ..., created_at: _Optional[str] = ..., plugin_digest: _Optional[str] = ..., execution_mode: _Optional[str] = ..., orchestration_pipeline_id: _Optional[str] = ..., orchestration_revision: _Optional[int] = ..., graph_digest: _Optional[str] = ...) -> None: ...

class PipelineList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[Pipeline]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[Pipeline, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class SaveJobDraft(_message.Message):
    __slots__ = ("asset_id", "pipeline_id", "name")
    ASSET_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    asset_id: str
    pipeline_id: str
    name: str
    def __init__(self, asset_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class JobDraft(_message.Message):
    __slots__ = ("id", "asset_id", "pipeline_id", "name", "state", "created_at", "reason", "execution_id", "run_id", "execution_mode", "pipeline_revision", "graph_digest", "modality_summary", "execution_state")
    ID_FIELD_NUMBER: _ClassVar[int]
    ASSET_ID_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_MODE_FIELD_NUMBER: _ClassVar[int]
    PIPELINE_REVISION_FIELD_NUMBER: _ClassVar[int]
    GRAPH_DIGEST_FIELD_NUMBER: _ClassVar[int]
    MODALITY_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_STATE_FIELD_NUMBER: _ClassVar[int]
    id: str
    asset_id: str
    pipeline_id: str
    name: str
    state: str
    created_at: str
    reason: str
    execution_id: str
    run_id: str
    execution_mode: str
    pipeline_revision: int
    graph_digest: str
    modality_summary: _struct_pb2.Struct
    execution_state: str
    def __init__(self, id: _Optional[str] = ..., asset_id: _Optional[str] = ..., pipeline_id: _Optional[str] = ..., name: _Optional[str] = ..., state: _Optional[str] = ..., created_at: _Optional[str] = ..., reason: _Optional[str] = ..., execution_id: _Optional[str] = ..., run_id: _Optional[str] = ..., execution_mode: _Optional[str] = ..., pipeline_revision: _Optional[int] = ..., graph_digest: _Optional[str] = ..., modality_summary: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., execution_state: _Optional[str] = ...) -> None: ...

class JobDraftList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[JobDraft]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[JobDraft, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class CreateToken(_message.Message):
    __slots__ = ("name", "scopes", "expires_in_days")
    NAME_FIELD_NUMBER: _ClassVar[int]
    SCOPES_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_IN_DAYS_FIELD_NUMBER: _ClassVar[int]
    name: str
    scopes: _containers.RepeatedScalarFieldContainer[str]
    expires_in_days: int
    def __init__(self, name: _Optional[str] = ..., scopes: _Optional[_Iterable[str]] = ..., expires_in_days: _Optional[int] = ...) -> None: ...

class AccessToken(_message.Message):
    __slots__ = ("id", "name", "scopes", "created_at", "expires_at", "revoked", "token")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SCOPES_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    REVOKED_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    scopes: _containers.RepeatedScalarFieldContainer[str]
    created_at: str
    expires_at: str
    revoked: bool
    token: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., scopes: _Optional[_Iterable[str]] = ..., created_at: _Optional[str] = ..., expires_at: _Optional[str] = ..., revoked: bool = ..., token: _Optional[str] = ...) -> None: ...

class TokenList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[AccessToken]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[AccessToken, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class AuditEvent(_message.Message):
    __slots__ = ("id", "actor", "action", "target", "created_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    actor: str
    action: str
    target: str
    created_at: str
    def __init__(self, id: _Optional[str] = ..., actor: _Optional[str] = ..., action: _Optional[str] = ..., target: _Optional[str] = ..., created_at: _Optional[str] = ...) -> None: ...

class AuditList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[AuditEvent]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[AuditEvent, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class CreateUser(_message.Message):
    __slots__ = ("username", "display_name", "password", "roles")
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    ROLES_FIELD_NUMBER: _ClassVar[int]
    username: str
    display_name: str
    password: str
    roles: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, username: _Optional[str] = ..., display_name: _Optional[str] = ..., password: _Optional[str] = ..., roles: _Optional[_Iterable[str]] = ...) -> None: ...

class UpdateUser(_message.Message):
    __slots__ = ("display_name", "roles", "disabled", "password")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ROLES_FIELD_NUMBER: _ClassVar[int]
    DISABLED_FIELD_NUMBER: _ClassVar[int]
    PASSWORD_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    roles: _containers.RepeatedScalarFieldContainer[str]
    disabled: bool
    password: str
    def __init__(self, display_name: _Optional[str] = ..., roles: _Optional[_Iterable[str]] = ..., disabled: bool = ..., password: _Optional[str] = ...) -> None: ...

class User(_message.Message):
    __slots__ = ("username", "display_name", "roles", "disabled")
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ROLES_FIELD_NUMBER: _ClassVar[int]
    DISABLED_FIELD_NUMBER: _ClassVar[int]
    username: str
    display_name: str
    roles: _containers.RepeatedScalarFieldContainer[str]
    disabled: bool
    def __init__(self, username: _Optional[str] = ..., display_name: _Optional[str] = ..., roles: _Optional[_Iterable[str]] = ..., disabled: bool = ...) -> None: ...

class UserList(_message.Message):
    __slots__ = ("items", "total")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[User]
    total: int
    def __init__(self, items: _Optional[_Iterable[_Union[User, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class EmptyResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

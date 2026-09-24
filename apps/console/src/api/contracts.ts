// Generated from proto/ by tools/generate_console_types.py. Do not edit.
export type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject;
export interface JsonObject { [key: string]: JsonValue; }
export interface LoginRequest {
  username: string;
  password: string;
}
export interface DemoAccount {
  enabled: boolean;
  username: string;
  password: string;
}
export interface Identity {
  principal: string;
  display_name: string;
  roles: string[];
  permissions: string[];
  csrf_token: string;
}
export interface ApiError {
  detail: string;
  reason_code: string;
  trace_id: string;
  retryable: boolean;
}
export interface Capability {
  name: string;
  available: boolean;
  reason: string;
}
export interface ConsoleStatus {
  capabilities: Capability[];
  schema_version: string;
}
export interface CreateUpload {
  filename: string;
  size_bytes: string;
  content_type: string;
}
export interface Upload {
  id: string;
  filename: string;
  size_bytes: string;
  content_type: string;
  state: string;
  sha256: string;
  created_at: string;
  reason: string;
}
export interface UploadList {
  items: Upload[];
  total: number;
}
export interface PluginEntry {
  id: string;
  name: string;
  version: string;
  description: string;
  digest: string;
  trust: string;
  state: string;
  consumes: string[];
  produces: string[];
  config_schema?: JsonObject;
  reason: string;
}
export interface PluginList {
  items: PluginEntry[];
}
export interface SavePluginConfig {
  plugin_id: string;
  name: string;
  config?: JsonObject;
}
export interface PluginConfig {
  id: string;
  plugin_id: string;
  name: string;
  revision: number;
  config?: JsonObject;
  config_hash: string;
  created_at: string;
}
export interface PluginConfigList {
  items: PluginConfig[];
  total: number;
}
export interface SavePipeline {
  name: string;
  description: string;
  plugin_id: string;
  config_id: string;
}
export interface Pipeline {
  id: string;
  name: string;
  description: string;
  plugin_id: string;
  config_id: string;
  state: string;
  revision: number;
  created_at: string;
  plugin_digest: string;
}
export interface PipelineList {
  items: Pipeline[];
  total: number;
}
export interface SaveJobDraft {
  asset_id: string;
  pipeline_id: string;
  name: string;
}
export interface JobDraft {
  id: string;
  asset_id: string;
  pipeline_id: string;
  name: string;
  state: string;
  created_at: string;
  reason: string;
}
export interface JobDraftList {
  items: JobDraft[];
  total: number;
}
export interface CreateToken {
  name: string;
  scopes: string[];
  expires_in_days: number;
}
export interface AccessToken {
  id: string;
  name: string;
  scopes: string[];
  created_at: string;
  expires_at: string;
  revoked: boolean;
  token: string;
}
export interface TokenList {
  items: AccessToken[];
  total: number;
}
export interface AuditEvent {
  id: string;
  actor: string;
  action: string;
  target: string;
  created_at: string;
}
export interface AuditList {
  items: AuditEvent[];
  total: number;
}
export interface CreateUser {
  username: string;
  display_name: string;
  password: string;
  roles: string[];
}
export interface UpdateUser {
  display_name: string;
  roles: string[];
  disabled: boolean;
  password?: string;
}
export interface User {
  username: string;
  display_name: string;
  roles: string[];
  disabled: boolean;
}
export interface UserList {
  items: User[];
  total: number;
}
export interface EmptyResponse {
}
export type ErrorCode = "ERROR_CODE_UNSPECIFIED" | "INVALID_INPUT" | "UNSUPPORTED_CAPABILITY" | "UNSUPPORTED_MEMORY_KIND" | "DEADLINE_EXCEEDED" | "RESOURCE_EXHAUSTED" | "TRANSIENT_BACKEND_FAILURE" | "DATA_POLICY_DENIED" | "INTERNAL_PLUGIN_ERROR";
export type ColorPrimaries = "COLOR_PRIMARIES_UNSPECIFIED" | "COLOR_PRIMARIES_BT709" | "COLOR_PRIMARIES_BT601" | "COLOR_PRIMARIES_BT2020";
export type TransferCharacteristics = "TRANSFER_CHARACTERISTICS_UNSPECIFIED" | "TRANSFER_CHARACTERISTICS_BT709" | "TRANSFER_CHARACTERISTICS_SRGB" | "TRANSFER_CHARACTERISTICS_SMPTE2084" | "TRANSFER_CHARACTERISTICS_ARIB_STD_B67";
export type MatrixCoefficients = "MATRIX_COEFFICIENTS_UNSPECIFIED" | "MATRIX_COEFFICIENTS_BT709" | "MATRIX_COEFFICIENTS_BT601" | "MATRIX_COEFFICIENTS_BT2020_NCL";
export interface TimeRange {
  start_ms: string;
  end_ms: string;
}
export interface RequestContext {
  request_id: string;
  trace_id: string;
  pipeline_run_id: string;
  stream_id: string;
  source_id: string;
  deadline_unix_ms: string;
  attempt: number;
  idempotency_key: string;
  privacy_policy?: PrivacyPolicy;
}
export interface PrivacyPolicy {
  data_egress: string;
  retain_derived_payload: boolean;
}
export interface ProcessingError {
  code: ErrorCode;
  reason_code: string;
  retryable: boolean;
  retry_after_ms: number;
}
export interface BufferDescriptor {
  buffer_id: string;
  kind: string;
  memory_kind: string;
  locator?: BufferLocator;
  format?: BufferFormat;
  stream_id: string;
  time_range?: TimeRange;
  lease?: BufferLease;
  content_hash: string;
}
export interface BufferLocator {
  handle: string;
  offset: string;
  length: string;
}
export interface BufferFormat {
  pixel_format: string;
  width: number;
  height: number;
  strides: number[];
  sample_rate: number;
  channels: number;
  display_rotation_deg?: number;
  pixel_aspect_ratio_num?: number;
  pixel_aspect_ratio_den?: number;
  source_bit_depth?: number;
  color_primaries: ColorPrimaries;
  transfer_characteristics: TransferCharacteristics;
  matrix_coefficients: MatrixCoefficients;
  sample_format: string;
}
export interface BufferLease {
  lease_id: string;
  expires_at_unix_ms: string;
  read_only: boolean;
}
export interface EventEnvelope {
  event_id: string;
  event_type: string;
  stream_id: string;
  trace_id: string;
  payload_ref: string;
  created_at_unix_ms: string;
  schema_version: number;
}
export interface Provenance {
  plugin: string;
  plugin_version: string;
  artifact_digest: string;
  model_release_id: string;
  model_id: string;
  model_version: string;
  config_hash: string;
  execution_backend: string;
  model_artifact_digest: string;
}
export interface Observation {
  observation_id: string;
  modality: string;
  stream_id: string;
  source_id: string;
  source_item_id: string;
  time_range?: TimeRange;
  payload?: JsonObject;
  confidence?: number;
  confidence_unavailable_reason: string;
  quality_state: string;
  quality_reasons: string[];
  provenance?: Provenance;
  content_hash: string;
  created_at_unix_ms: string;
  timing_source: string;
  timing_confidence?: number;
}
export interface SourceReference {
  asset_id: string;
  time_range?: TimeRange;
  content_hash: string;
}
export interface MaterialUnit {
  material_unit_id: string;
  stream_id: string;
  time_range?: TimeRange;
  status: string;
  revision: number;
  observations: Observation[];
  tags: string[];
  source_refs: SourceReference[];
  pipeline_version: string;
  pending_enrichments: string[];
  created_at_unix_ms: string;
  superseded: boolean;
}
export interface SearchRequest {
  query: string;
  stream_id: string;
  start_ms?: string;
  end_ms?: string;
  modalities: string[];
  tags: string[];
  min_confidence?: number;
  limit: number;
  mode: string;
}
export interface SearchHit {
  material_unit_id: string;
  revision: number;
  distance: number;
  embedding_id: string;
  vector_ref: string;
  observation_id: string;
}
export interface SearchResponse {
  materials: MaterialUnit[];
  mode: string;
  index_version: string;
  hits: SearchHit[];
  unindexed_hits: number;
  vector_index_key: string;
  unresolved_hits: number;
}
export interface GetMaterialRequest {
  material_unit_id: string;
  revision?: number;
}

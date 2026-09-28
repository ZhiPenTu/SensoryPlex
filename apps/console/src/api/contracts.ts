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
  execution_mode: string;
  orchestration_pipeline_id: string;
  orchestration_revision: number;
  graph_digest: string;
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
  execution_id: string;
  run_id: string;
  execution_mode: string;
  pipeline_revision: number;
  graph_digest: string;
  modality_summary?: JsonObject;
  execution_state: string;
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
  handoff_endpoint: string;
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
  execution_id: string;
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
export type CapabilityState = "CAPABILITY_STATE_UNSPECIFIED" | "CAPABILITY_STATE_AVAILABLE" | "CAPABILITY_STATE_UNAVAILABLE";
export type AcceleratorState = "ACCELERATOR_STATE_UNSPECIFIED" | "ACCELERATOR_STATE_AVAILABLE" | "ACCELERATOR_STATE_UNAVAILABLE" | "ACCELERATOR_STATE_UNKNOWN";
export interface DescribeRequest {
}
export interface PluginDescription {
  name: string;
  version: string;
  protocol: string;
  consumes: string[];
  produces: string[];
  memory_kinds: string[];
  artifact_digest: string;
}
export interface ValidateConfigRequest {
  config?: JsonObject;
}
export interface ValidationResult {
  valid: boolean;
  field_errors: string[];
}
export interface StartRequest {
  config?: JsonObject;
}
export interface LifecycleResponse {
  state: string;
  error?: ProcessingError;
}
export interface PluginInput {
  buffer?: BufferDescriptor;
  observation?: Observation;
}
export interface ProcessRequest {
  context?: RequestContext;
  inputs: PluginInput[];
  processor_release_id: string;
}
export interface ProcessResponse {
  observations: Observation[];
  warnings: string[];
  error?: ProcessingError;
}
export interface CancelRequest {
  request_id: string;
}
export interface HealthRequest {
}
export interface HealthResponse {
  state: string;
  unavailable_capabilities: string[];
}
export interface DrainRequest {
  grace_period_ms: number;
}
export interface StopRequest {
}
export interface HostResources {
  unified_memory_bytes: string;
  total_memory_bytes: string;
  logical_cores: number;
}
export interface BackendCapability {
  backend: string;
  platform: string;
  runtime_version: string;
  precisions: string[];
  memory_kinds: string[];
  max_concurrency: number;
  state: CapabilityState;
  unavailable_reason: string;
}
export interface HostAccelerator {
  accelerator: string;
  platform: string;
  state: AcceleratorState;
  detection_source: string;
  runtime_version: string;
  evidence: string[];
  unavailable_reason: string;
}
export interface ResidencyLimits {
  tier: string;
  media_queue_capacity: string;
  model_parallelism: string;
}
export interface DescribeCapabilitiesRequest {
}
export interface DescribeCapabilitiesResponse {
  platform: string;
  host?: HostResources;
  backends: BackendCapability[];
  unavailable_capabilities: string[];
  admitted_memory_kinds: string[];
  host_accelerators: HostAccelerator[];
  residency?: ResidencyLimits;
}
export type NodeStatus = "NODE_STATUS_UNSPECIFIED" | "NODE_STATUS_CANDIDATE" | "NODE_STATUS_ENROLLING" | "NODE_STATUS_READY" | "NODE_STATUS_DRAINING" | "NODE_STATUS_OFFLINE" | "NODE_STATUS_REVOKED";
export type PluginInstanceState = "PLUGIN_INSTANCE_STATE_UNSPECIFIED" | "PLUGIN_INSTANCE_STATE_PLANNED" | "PLUGIN_INSTANCE_STATE_INSTALLING" | "PLUGIN_INSTANCE_STATE_READY" | "PLUGIN_INSTANCE_STATE_DEGRADED" | "PLUGIN_INSTANCE_STATE_DRAINING" | "PLUGIN_INSTANCE_STATE_STOPPED" | "PLUGIN_INSTANCE_STATE_FAILED" | "PLUGIN_INSTANCE_STATE_ROLLED_BACK" | "PLUGIN_INSTANCE_STATE_UNINSTALLED";
export type DeploymentAction = "DEPLOYMENT_ACTION_UNSPECIFIED" | "DEPLOYMENT_ACTION_INSTALL" | "DEPLOYMENT_ACTION_START" | "DEPLOYMENT_ACTION_STOP" | "DEPLOYMENT_ACTION_UNINSTALL" | "DEPLOYMENT_ACTION_ROLLBACK" | "DEPLOYMENT_ACTION_DRAIN" | "DEPLOYMENT_ACTION_TASK_PROCESS" | "DEPLOYMENT_ACTION_STAGE_RELEASE" | "DEPLOYMENT_ACTION_RECONCILE";
export type PluginOperationStage = "PLUGIN_OPERATION_STAGE_UNSPECIFIED" | "PLUGIN_OPERATION_STAGE_ACCEPTED" | "PLUGIN_OPERATION_STAGE_STAGING" | "PLUGIN_OPERATION_STAGE_STARTING" | "PLUGIN_OPERATION_STAGE_VALIDATING" | "PLUGIN_OPERATION_STAGE_CANDIDATE_READY" | "PLUGIN_OPERATION_STAGE_CUTTING_OVER" | "PLUGIN_OPERATION_STAGE_DRAINING_OLD" | "PLUGIN_OPERATION_STAGE_SUCCEEDED" | "PLUGIN_OPERATION_STAGE_FAILED" | "PLUGIN_OPERATION_STAGE_CANCELLED";
export type PluginRuntimeRole = "PLUGIN_RUNTIME_ROLE_UNSPECIFIED" | "PLUGIN_RUNTIME_ROLE_CANDIDATE" | "PLUGIN_RUNTIME_ROLE_ACTIVE" | "PLUGIN_RUNTIME_ROLE_PREVIOUS" | "PLUGIN_RUNTIME_ROLE_FAILED";
export type PluginRuntimeState = "PLUGIN_RUNTIME_STATE_UNSPECIFIED" | "PLUGIN_RUNTIME_STATE_PLANNED" | "PLUGIN_RUNTIME_STATE_STAGED" | "PLUGIN_RUNTIME_STATE_STARTING" | "PLUGIN_RUNTIME_STATE_VALIDATING" | "PLUGIN_RUNTIME_STATE_CANDIDATE_READY" | "PLUGIN_RUNTIME_STATE_ACTIVE" | "PLUGIN_RUNTIME_STATE_DRAINING" | "PLUGIN_RUNTIME_STATE_STOPPED" | "PLUGIN_RUNTIME_STATE_FAILED" | "PLUGIN_RUNTIME_STATE_UNINSTALLED";
export interface NodeCapabilityProfile {
  platform: string;
  arch: string;
  cpu_cores: number;
  memory_bytes: string;
  unified_memory_bytes: string;
  accelerators: HostAccelerator[];
  supported_artifacts: string[];
  labels: Record<string, string>;
}
export interface NodeInfo {
  node_id: string;
  display_name: string;
  status: NodeStatus;
  status_reason: string;
  capabilities?: NodeCapabilityProfile;
  is_co_located: boolean;
  last_heartbeat_at: string;
  enrolled_at: string;
  instances: PluginInstance[];
}
export interface NodeList {
  items: NodeInfo[];
  total: number;
}
export interface PluginInstance {
  instance_id: string;
  node_id: string;
  plugin_id: string;
  plugin_version: string;
  artifact_digest: string;
  previous_digest: string;
  desired_state: string;
  actual_state: string;
  config_hash: string;
  config?: JsonObject;
  error_code: string;
  error_detail: string;
  created_at: string;
  updated_at: string;
  active_runtime_instance_id: string;
  active_release_id: string;
  previous_runtime_instance_id: string;
  generation: string;
  endpoint: string;
}
export interface PluginInstanceList {
  items: PluginInstance[];
  total: number;
}
export interface EnrollmentToken {
  token: string;
  node_id: string;
  expires_at: string;
  created_at: string;
}
export interface CreateEnrollmentTokenRequest {
  node_id: string;
  expires_in_minutes: number;
}
export interface EnrollNodeRequest {
  enrollment_token: string;
  node_id: string;
  display_name: string;
  is_co_located: boolean;
  capabilities?: NodeCapabilityProfile;
}
export interface EnrollNodeResponse {
  success: boolean;
  node_id: string;
  status: NodeStatus;
  session_token: string;
  message: string;
}
export interface NodeHeartbeatRequest {
  node_id: string;
  session_token: string;
  timestamp_unix_ms: string;
  available_memory_bytes: string;
  current_concurrency: number;
  running_instance_ids: string[];
  runtime_observations: PluginRuntimeObservation[];
}
export interface PluginRuntimeObservation {
  runtime_instance_id: string;
  operation_id: string;
  generation: string;
  observed_state: string;
  endpoint: string;
  supervisor_id: string;
  supervisor_managed: boolean;
  unit_loaded: boolean;
  last_exit_code: number;
  observed_at: string;
  reconciliation: string;
  detail: string;
}
export interface NodeHeartbeatResponse {
  status: NodeStatus;
  heartbeat_interval_ms: number;
  pending_intents: DeploymentIntent[];
  reconciliation_required: string[];
}
export interface DeploymentIntent {
  intent_id: string;
  instance_id: string;
  node_id: string;
  plugin_id: string;
  plugin_version: string;
  action: DeploymentAction;
  artifact_digest: string;
  rollback_digest: string;
  config?: JsonObject;
  created_at: string;
  deadline_unix_ms: string;
  operation_id: string;
  generation: string;
  release_id: string;
  bundle_digest: string;
  runtime_instance_id: string;
  grace_period_ms: string;
  config_hash: string;
}
export interface ReportDeploymentRequest {
  intent_id: string;
  instance_id: string;
  node_id: string;
  action: DeploymentAction;
  success: boolean;
  actual_state: string;
  error_code: string;
  error_detail: string;
  operation_id: string;
  generation: string;
  release_id: string;
  runtime_instance_id: string;
  stage: PluginOperationStage;
  verified_plugin_id: string;
  verified_artifact_digest: string;
  endpoint: string;
  supervisor_id: string;
  staging_ms: string;
  starting_ms: string;
  validating_ms: string;
  draining_ms: string;
}
export interface PreflightRequest {
  node_id: string;
  plugin_id: string;
  config_id: string;
  requires_data_locality: boolean;
  data_plane_node_id: string;
  config?: JsonObject;
}
export interface PreflightResponse {
  eligible: boolean;
  reason_code: string;
  detail: string;
  matched_capabilities: string[];
  missing_capabilities: string[];
}
export interface TaskAssignment {
  assignment_id: string;
  job_id: string;
  target_node_id: string;
  actual_node_id: string;
  state: string;
  reassignment_reason: string;
  data_locality_checked: boolean;
  deadline_unix_ms: string;
  created_at: string;
}
export interface DeregisterNodeRequest {
  node_id: string;
  session_token: string;
  reason: string;
}
export interface DeregisterNodeResponse {
  success: boolean;
  node_id: string;
  status: NodeStatus;
  message: string;
}
export interface PluginRelease {
  release_id: string;
  plugin_id: string;
  plugin_version: string;
  platform: string;
  arch: string;
  form: string;
  artifact_digest: string;
  bundle_digest: string;
  manifest_digest: string;
  config_schema_digest: string;
  sbom_digest: string;
  bundle_bytes: string;
  entrypoint?: JsonObject;
  runtime_requirements?: JsonObject;
  trust: string;
  authenticated: boolean;
  authentication_method: string;
  signature_status: string;
  sbom_components: number;
  declared_memory_bytes: string;
  declared_cpu_millicores: string;
  default_deadline_ms: string;
  published_at: string;
  created_by: string;
}
export interface PluginReleaseList {
  items: PluginRelease[];
  total: number;
}
export interface PluginRuntimeInstance {
  runtime_instance_id: string;
  instance_id: string;
  node_id: string;
  plugin_id: string;
  release_id: string;
  artifact_digest: string;
  bundle_digest: string;
  generation: string;
  role: PluginRuntimeRole;
  state: PluginRuntimeState;
  endpoint: string;
  supervisor_id: string;
  unit_name: string;
  install_dir: string;
  verified_plugin_id: string;
  verified_artifact_digest: string;
  launch_ms: string;
  drain_ms: string;
  error_code: string;
  error_detail: string;
  created_at: string;
  updated_at: string;
}
export interface PluginDeploymentOperation {
  operation_id: string;
  kind: string;
  node_id: string;
  instance_id: string;
  plugin_id: string;
  release_id: string;
  from_runtime_instance_id: string;
  candidate_runtime_instance_id: string;
  rollback_of_operation_id: string;
  generation: string;
  stage: PluginOperationStage;
  cancellable: boolean;
  deadline_unix_ms: string;
  error_code: string;
  error_detail: string;
  staging_ms: string;
  starting_ms: string;
  validating_ms: string;
  draining_ms: string;
  config?: JsonObject;
  candidate?: PluginRuntimeInstance;
  active?: PluginRuntimeInstance;
  previous?: PluginRuntimeInstance;
  created_by: string;
  created_at: string;
  updated_at: string;
  completed_at: string;
}
export interface PluginDeploymentOperationList {
  items: PluginDeploymentOperation[];
  total: number;
}
export interface CreatePluginDeploymentRequest {
  release_id: string;
  config_id: string;
  config?: JsonObject;
}
export interface BatchDeployPluginsRequest {
  plugin_ids: string[];
}
export interface PluginDeploymentRejection {
  plugin_id: string;
  reason: string;
}
export interface BatchDeployPluginsResponse {
  node_id: string;
  operations: PluginDeploymentOperation[];
  already_ready: string[];
  rejected: PluginDeploymentRejection[];
}
export interface SyncPluginReleasesRequest {
  dry_run: boolean;
}
export interface SyncPluginReleasesResponse {
  imported: string[];
  unchanged: string[];
  rejected: string[];
  total: number;
}

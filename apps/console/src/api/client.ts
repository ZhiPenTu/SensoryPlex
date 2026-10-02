import type { ApiError, Upload } from './contracts';

let csrf = '';
export function setCsrf(value: string) {
    csrf = value;
}

const messages: Record<string, string> = {
    cannot_remove_own_administration: '不能停用当前管理员或移除自己的管理权限。',
    browser_session_required: '请使用浏览器登录后管理访问凭据。',
    authentication_required: '会话已失效，请重新登录。',
    invalid_credentials: '用户名或密码不正确。',
    login_rate_limited: '登录尝试过多，请在 10 分钟后重试。',
    permission_denied: '你没有执行此操作的权限。',
    csrf_required: '会话验证失效，请刷新后重试。',
    metadata_store_unavailable: '数据服务暂不可用，请稍后重试。',
    runtime_task_service_not_attached: '任务执行服务尚未接入，已保存的草稿不会自动运行。',
    runtime_plugin_installer_not_attached: '插件安装执行器尚未接入。',
    runtime_pipeline_validation_not_attached: '运行时方案校验尚未接入，暂时不能发布。',
    plugin_config_invalid: '插件配置不符合字段约束。',
    plugin_config_limits_exceeded: '插件配置超过允许范围。',
    upload_container_signature_mismatch: '文件内容与选择的视频格式不符。',
    upload_size_exceeded: '文件超过上传容量限制。',
    upload_length_mismatch: '文件未完整上传，请重试。',
    upload_capacity_exhausted: '有其他文件正在上传，请稍后重试。',
    pipeline_referenced_by_drafts: '此方案仍被任务草稿引用，请先归档相关任务。',
    invalid_multimodal_audio_overlap: '音频重叠须小于音频切段长度，并在 0–59000 毫秒范围内。',
    record_already_exists: '记录已存在，请使用其他名称。',
    password_length_12_to_256_required: '密码长度需要为 12–256 位。',
    token_scope_exceeds_grant: '凭据权限不能超出你的业务权限。',
    storage_unavailable: '文件存储暂时不可用。',
    blob_unavailable: '原文件暂不可读取。',
    material_not_found: '素材或指定版本不存在，或当前账户无权访问。',
    material_source_not_found: '找不到可访问的原片来源。',
    material_source_unmapped: '原片尚未关联。需要先由媒体目录登记该来源与上传文件的对应关系。',
    material_source_mismatch: '来源摘要与原片不一致，已停止回看。',
    material_source_time_invalid: '来源时间区间与原片时长不一致，已停止定位。',
    upload_incomplete: '原片尚未完整上传。',
    invalid_time_range: '时间范围无效，结束时间需要晚于开始时间。',
    invalid_confidence: '最低置信度需要在 0 到 1 之间。',
    semantic_search_unavailable: '语义检索尚未接入：本节点未配置向量检索面。',
    semantic_index_unreachable: '向量检索面暂时不可达，请稍后重试。',
    semantic_index_unauthenticated: '向量检索面拒绝了网关的访问：令牌不一致，需要运维改配置。',
    semantic_filters_not_supported: '语义检索暂不支持筛选条件，请只用查询文字与条数。',
    data_locality_violation:
        '数据本地性约束：该插件读取共享内存句柄，不能安装到局域网远端节点，必须部署在同机数据面节点。',
    node_not_found: '目标节点不存在或尚未注册。',
    node_offline: '目标节点当前处于离线状态。',
    node_draining: '目标节点正在排空中，不接受新部署。',
    node_revoked: '目标节点凭据已被管理员撤销，禁止调度。',
    accelerator_not_available: '目标节点缺少插件所要求的硬件加速器。',
    unsupported_platform: '目标节点的操作系统或架构不满足插件要求。',
    container_runtime_unsupported: '目标节点不支持容器运行环境。',
    native_runtime_unsupported: '目标节点不支持原生运行时环境。',
    insufficient_cpu: '目标节点 CPU 核心数低于插件最低要求。',
    insufficient_memory: '目标节点物理内存低于插件最低要求。',
    digest_mismatch: '插件制品摘要校验失败。',
    no_previous_digest_for_rollback: '该插件实例无历史版本摘要，无法执行回滚。',
    // ── ADR-030 插件热部署 ─────────────────────────────────────────────────
    plugin_release_not_found: '受控制品仓里找不到这个 release 描述符。',
    node_agent_not_enrolled: '本机安装 Agent 尚未登记，请先启动并登记 Node Agent。',
    plugin_configuration_required: '缺少有效的插件配置，请先保存参数方案（如本机模型目录）再装配。',
    deployed_plugin_configuration_not_publishable:
        '当前运行配置不能直接用于处理方案，请在插件中心保存兼容配置并升级插件。',
    plugin_deployment_in_progress: '该插件正在部署，请等待当前操作完成。',
    plugin_release_not_authenticated: '这个 release 不是受控制品仓发布的首方已认证制品，禁止激活。',
    plugin_release_platform_mismatch: 'release 的平台/架构与目标节点不一致，装不上去。',
    plugin_release_plugin_mismatch: '所选 release 与目标插件身份不一致。',
    plugin_release_content_conflict: '同一插件版本在制品仓里已存在不同的 bundle 摘要，拒绝覆盖。',
    release_repository_unavailable: '受控制品仓不可访问：请确认构建机已写出 .data/releases。',
    release_bundle_unavailable: '制品仓里的 bundle 文件已丢失或被移动，无法交付给 Agent。',
    release_not_entitled_for_this_agent: '该节点当前没有匹配的部署意图，不允许下载这个 bundle。',
    upgrade_headroom_insufficient:
        '升级余量不足：本机无法同时容纳旧版本与候选版本，已拒绝；不会停止旧版本，也不会降级成停机更新。',
    plugin_not_active_for_upgrade: '该插件尚未激活，无法升级；请先执行首次部署。',
    plugin_already_active: '该插件槽位已有运行中的实例，请改用升级。',
    candidate_node_not_admitted: '目标节点仍在候选中，尚未被管理员接纳，不能接收部署。',
    plugin_deployment_operation_not_found: '部署操作不存在或已被清理。',
    plugin_deployment_operation_already_closed: '这次部署已经结算，不能再取消。',
    plugin_deployment_operation_not_settled: '这次部署还没有结算，暂时不能回滚。',
    plugin_deployment_cancel_window_closed: '已经进入切换阶段，取消窗口关闭：请改用回滚。',
    no_previous_runtime_instance_for_rollback: '该槽位没有上一版本运行实例，无法回滚。',
    deployment_operation_missing: 'Agent 回报里缺少部署操作身份，控制面拒绝推进。',
    deployment_intent_not_found: '找不到对应的部署意图。',
    duplicate_deployment_report: '同一次意图被重复回报，已拒绝（不会重复执行）。',
    stale_deployment_report: '回报来自过期或伪造的部署意图，已拒绝。',
    fencing_token_mismatch: '回报的代次（generation）与当前操作不一致，已拒绝。',
    deployment_report_node_mismatch: '回报节点与部署意图所属节点不一致，已拒绝。',
    invalid_deployment_stage: '回报里的部署阶段取值无效。',
    operation_deadline_exceeded: '部署操作已超出总时限（默认 5 分钟），已失败并保留旧版本。',
    candidate_not_ready_for_cutover: '候选实例尚未就绪，不能切换 active 指针。',
    generation_cas_failed: '槽位世代已被更新的操作推进，本次切换被拒绝。',
    candidate_artifact_digest_mismatch: '候选进程自报的制品摘要与意图不一致，拒绝切换。',
    candidate_plugin_identity_mismatch: '候选进程自报的插件身份与意图不一致，拒绝切换。',
    candidate_config_invalid: '候选实例拒绝了这次配置：请修正配置后重试。',
    candidate_start_failed: '候选实例启动失败，旧版本继续服务。',
    candidate_start_timeout: '候选实例未在时限内就绪，已显式失败，旧版本继续服务。',
    plugin_endpoint_file_timeout: '候选实例未在时限内写出 loopback endpoint 文件，已显式失败。',
    plugin_endpoint_file_invalid: '候选实例写出的 endpoint 文件不可解析，已显式失败。',
    drain_target_unknown: 'Agent 本机没有这个运行实例的台账，无法排空。',
    drain_timeout: '旧版本未在受限 grace period 内排空，已显式失败。',
    drain_failed: '排空旧版本失败。',
    reconciliation_required:
        'Agent 对账发现平台服务状态与控制面不一致：不猜成功、不删制品，需要人工确认。',
};

export class RequestError extends Error {
    constructor(
        public status: number,
        public reason: string,
        public trace = '',
    ) {
        super(messages[reason] || `操作未完成：${reason}`);
    }
}

export async function api<T>(path: string, init: RequestInit = {}, timeoutMs = 15000): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && typeof init.body === 'string') headers.set('Content-Type', 'application/json');
    if (csrf) headers.set('X-CSRF-Token', csrf);
    const response = await fetch(path, {
        ...init,
        headers,
        credentials: 'same-origin',
        signal: init.signal
            ? AbortSignal.any([init.signal, AbortSignal.timeout(timeoutMs)])
            : AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) {
        const error: Partial<ApiError> = await response.json().catch(() => ({}));
        if (response.status === 401) window.dispatchEvent(new Event('session-expired'));
        throw new RequestError(
            response.status,
            error.reason_code || `http_${response.status}`,
            error.trace_id,
        );
    }
    return response.json() as Promise<T>;
}

export const post = <T>(path: string, body: unknown = {}) =>
    api<T>(path, { method: 'POST', body: JSON.stringify(body) });

export function uploadFile(
    id: string,
    file: File,
    progress: (percent: number) => void,
): Promise<Upload> {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', `/v1/uploads/${encodeURIComponent(id)}/content`);
        xhr.setRequestHeader('X-CSRF-Token', csrf);
        xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
        xhr.timeout = 900000;
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) progress(Math.round((e.loaded / e.total) * 100));
        };
        xhr.onerror = () => reject(new Error('上传连接中断，可重新选择文件重试。'));
        xhr.ontimeout = () => reject(new Error('上传超时，请重试。'));
        xhr.onload = () => {
            try {
                const body = JSON.parse(xhr.responseText);
                if (xhr.status >= 200 && xhr.status < 300) resolve(body);
                else
                    reject(
                        new RequestError(
                            xhr.status,
                            body.reason_code || 'upload_failed',
                            body.trace_id,
                        ),
                    );
            } catch {
                reject(new Error('上传响应无效。'));
            }
        };
        xhr.send(file);
    });
}

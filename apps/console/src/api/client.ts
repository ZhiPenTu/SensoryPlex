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
    record_already_exists: '记录已存在，请使用其他名称。',
    password_length_12_to_256_required: '密码长度需要为 12–256 位。',
    token_scope_exceeds_grant: '凭据权限不能超出你的业务权限。',
    storage_unavailable: '文件存储暂时不可用。',
    blob_unavailable: '原文件暂不可读取。',
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

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && typeof init.body === 'string') headers.set('Content-Type', 'application/json');
    if (csrf) headers.set('X-CSRF-Token', csrf);
    const response = await fetch(path, {
        ...init,
        headers,
        credentials: 'same-origin',
        signal: init.signal
            ? AbortSignal.any([init.signal, AbortSignal.timeout(15000)])
            : AbortSignal.timeout(15000),
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

import { CircleAlert, FolderOpen, LoaderCircle, X } from 'lucide-react';
import { useEffect, useRef, type ReactNode } from 'react';
import { RequestError } from './api/client';

export function Heading({
    eyebrow,
    title,
    description,
    action,
}: {
    eyebrow: string;
    title: string;
    description: string;
    action?: ReactNode;
}) {
    return (
        <div className="page-heading">
            <div>
                <span className="eyebrow">{eyebrow}</span>
                <h1>{title}</h1>
                <p>{description}</p>
            </div>
            {action}
        </div>
    );
}
export function Loading() {
    return (
        <div className="empty">
            <LoaderCircle className="spin" size={26} />
            <p>正在读取…</p>
        </div>
    );
}
export function ErrorNotice({ error }: { error: unknown }) {
    if (!error) return null;
    return (
        <div className="notice danger" role="alert">
            <CircleAlert size={18} />
            <div>
                {error instanceof Error ? error.message : '请求未完成。'}
                {error instanceof RequestError && error.trace ? (
                    <small>追踪编号 {error.trace}</small>
                ) : null}
            </div>
        </div>
    );
}
export function Notice({ children }: { children: ReactNode }) {
    return (
        <div className="notice">
            <CircleAlert size={18} />
            <div>{children}</div>
        </div>
    );
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
    return (
        <div className="empty">
            <span className="empty-icon">
                <FolderOpen size={30} strokeWidth={1.3} />
            </span>
            <h3>{title}</h3>
            <p>{children}</p>
        </div>
    );
}
const labels: Record<string, string> = {
    pending: '等待上传',
    awaiting_admission: '待媒体准入',
    draft: '草稿',
    processing: '处理中',
    completed: '已完成',
    archived: '已归档',
    source_available: '源码可用',
    unverified: '未验证来源',
    fast_ready: '基础素材',
    partial: '部分结果',
    enriched: '已补全',
    failed: '失败',
    NODE_STATUS_CANDIDATE: '待接纳',
    NODE_STATUS_ENROLLING: '注册中',
    NODE_STATUS_READY: '可调度',
    NODE_STATUS_DRAINING: '排空中',
    NODE_STATUS_OFFLINE: '离线',
    NODE_STATUS_REVOKED: '已撤销',
    PLUGIN_INSTANCE_STATE_PLANNED: '已下发',
    PLUGIN_INSTANCE_STATE_INSTALLING: '安装中',
    PLUGIN_INSTANCE_STATE_READY: 'ready',
    PLUGIN_INSTANCE_STATE_DEGRADED: '降级',
    PLUGIN_INSTANCE_STATE_DRAINING: '排空中',
    PLUGIN_INSTANCE_STATE_STOPPED: '已停止',
    PLUGIN_INSTANCE_STATE_FAILED: '失败',
    PLUGIN_INSTANCE_STATE_ROLLED_BACK: '已回滚',
    PLUGIN_INSTANCE_STATE_UNINSTALLED: '已卸载',
};
export function Badge({ state }: { state: string }) {
    const tone =
        state === 'archived' ||
        state === 'NODE_STATUS_OFFLINE' ||
        state === 'NODE_STATUS_REVOKED' ||
        state === 'PLUGIN_INSTANCE_STATE_STOPPED' ||
        state === 'PLUGIN_INSTANCE_STATE_UNINSTALLED'
            ? 'muted'
            : state === 'source_available' ||
                state === 'fast_ready' ||
                state === 'enriched' ||
                state === 'NODE_STATUS_READY' ||
                state === 'PLUGIN_INSTANCE_STATE_READY'
              ? 'good'
              : state === 'failed' ||
                  state === 'NODE_STATUS_CANDIDATE' ||
                  state === 'PLUGIN_INSTANCE_STATE_FAILED' ||
                  state === 'PLUGIN_INSTANCE_STATE_DEGRADED'
                ? 'warning'
                : '';
    return <span className={`badge ${tone}`}>{labels[state] || state}</span>;
}
export function Pager({
    offset,
    total,
    onChange,
}: {
    offset: number;
    total: number;
    onChange: (offset: number) => void;
}) {
    return (
        <div className="pager">
            <span>共 {total} 条</span>
            <button disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - 20))}>
                上一页
            </button>
            <button disabled={offset + 20 >= total} onClick={() => onChange(offset + 20)}>
                下一页
            </button>
        </div>
    );
}
export function Modal({
    title,
    children,
    onClose,
}: {
    title: string;
    children: ReactNode;
    onClose: () => void;
}) {
    const ref = useRef<HTMLElement>(null);
    useEffect(() => {
        const previous = document.activeElement as HTMLElement | null;
        ref.current?.focus();
        return () => previous?.focus();
    }, []);
    return (
        <div
            className="modal-backdrop"
            onClick={(e) => {
                if (e.target === e.currentTarget) onClose();
            }}
        >
            <section
                ref={ref}
                tabIndex={-1}
                role="dialog"
                aria-modal="true"
                aria-label={title}
                className="modal"
                onKeyDown={(e) => {
                    if (e.key === 'Escape') onClose();
                    if (e.key === 'Tab') {
                        const targets = Array.from(
                            ref.current?.querySelectorAll<HTMLElement>(
                                'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]',
                            ) || [],
                        );
                        const first = targets[0],
                            last = targets[targets.length - 1];
                        if (
                            e.shiftKey &&
                            (document.activeElement === first ||
                                document.activeElement === ref.current)
                        ) {
                            e.preventDefault();
                            last?.focus();
                        } else if (!e.shiftKey && document.activeElement === last) {
                            e.preventDefault();
                            first?.focus();
                        }
                    }
                }}
            >
                <header>
                    <h2>{title}</h2>
                    <button aria-label="关闭" onClick={onClose}>
                        <X size={20} />
                    </button>
                </header>
                {children}
            </section>
        </div>
    );
}
export const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
export const bytes = (value: string) => {
    const n = Number(value);
    return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1024).toFixed(1)} KB`;
};
export const time = (value: string) => `${(Number(value) / 1000).toFixed(3)}s`;

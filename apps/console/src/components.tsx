import React, { type ReactNode } from 'react';
import {
    Alert,
    Empty as AntEmpty,
    Modal as AntModal,
    Pagination,
    Spin,
    Tag,
    Typography,
    Card,
} from 'antd';
import {
    CheckCircleFilled,
    ClockCircleFilled,
    CloseCircleFilled,
    ExclamationCircleFilled,
    SyncOutlined,
} from '@ant-design/icons';
import { RequestError } from './api/client';

const { Title, Paragraph, Text } = Typography;

/**
 * 统一页面头部组件：极简、沉稳
 */
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
        <div style={{ marginBottom: 20 }}>
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'flex-start',
                    flexWrap: 'wrap',
                    gap: 16,
                }}
            >
                <div>
                    {eyebrow ? (
                        <div style={{ marginBottom: 4 }}>
                            <span
                                style={{
                                    fontSize: 11,
                                    fontWeight: 650,
                                    letterSpacing: '1px',
                                    color: '#64748b',
                                    textTransform: 'uppercase',
                                    display: 'inline-block',
                                }}
                            >
                                {eyebrow}
                            </span>
                        </div>
                    ) : null}
                    <Title
                        level={2}
                        style={{
                            margin: 0,
                            fontWeight: 700,
                            letterSpacing: '-0.4px',
                            color: '#0f172a',
                            fontSize: 22,
                        }}
                    >
                        {title}
                    </Title>
                    <Paragraph
                        type="secondary"
                        style={{ margin: '4px 0 0', fontSize: 13, color: '#475569' }}
                    >
                        {description}
                    </Paragraph>
                </div>
                {action ? (
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>{action}</div>
                ) : null}
            </div>
        </div>
    );
}

/**
 * 统一加载状态
 */
export function Loading({ tip = '正在读取…' }: { tip?: string }) {
    return (
        <div style={{ padding: '48px 0', textAlign: 'center' }}>
            <Spin tip={tip} size="large" />
        </div>
    );
}

/**
 * 统一错误提示：柔和不刺眼
 */
export function ErrorNotice({ error }: { error: unknown }) {
    if (!error) return null;
    const message = error instanceof Error ? error.message : '请求未完成。';
    const trace = error instanceof RequestError && error.trace ? error.trace : null;

    return (
        <Alert
            type="error"
            showIcon
            style={{
                marginBottom: 16,
                borderRadius: 6,
                background: '#fef2f2',
                border: '1px solid #fecaca',
            }}
            message={
                <div>
                    <span style={{ color: '#991b1b', fontSize: 13, fontWeight: 500 }}>
                        {message}
                    </span>
                    {trace ? (
                        <div style={{ marginTop: 4 }}>
                            <Tag
                                style={{
                                    fontFamily: 'monospace',
                                    fontSize: 11,
                                    background: '#fee2e2',
                                    border: '1px solid #fca5a5',
                                    color: '#991b1b',
                                }}
                            >
                                追踪编号: {trace}
                            </Tag>
                        </div>
                    ) : null}
                </div>
            }
        />
    );
}

/**
 * 统一普通提示通知：精致灰蓝调
 */
export function Notice({ children }: { children: ReactNode }) {
    return (
        <Alert
            type="info"
            showIcon
            style={{
                marginBottom: 16,
                borderRadius: 6,
                background: '#f8fafc',
                border: '1px solid #e2e8f0',
            }}
            message={<span style={{ fontSize: 13, color: '#334155' }}>{children}</span>}
        />
    );
}

/**
 * 统一空状态
 */
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
    return (
        <AntEmpty
            image={AntEmpty.PRESENTED_IMAGE_SIMPLE}
            description={
                <div>
                    <Text
                        strong
                        style={{
                            fontSize: 14,
                            display: 'block',
                            marginBottom: 4,
                            color: '#334155',
                        }}
                    >
                        {title}
                    </Text>
                    {children ? (
                        <Text type="secondary" style={{ fontSize: 12, color: '#64748b' }}>
                            {children}
                        </Text>
                    ) : null}
                </div>
            }
            style={{ padding: '36px 0' }}
        />
    );
}

/**
 * 现代企业级精致状态 Tag 配色系统：
 * 采用微底色 + 深饱和文字 + 细微边框，杜绝刺眼大块亮色
 */
interface StatusStyle {
    label: string;
    bg: string;
    text: string;
    border: string;
    icon?: React.ReactNode;
}

const statusConfig: Record<string, StatusStyle> = {
    // 成功 / 可调度 / 就绪 (Emerald)
    published: {
        label: '已发布',
        bg: '#ecfdf5',
        text: '#047857',
        border: '#a7f3d0',
        icon: <CheckCircleFilled />,
    },
    completed: {
        label: '已完成',
        bg: '#ecfdf5',
        text: '#047857',
        border: '#a7f3d0',
        icon: <CheckCircleFilled />,
    },
    source_available: {
        label: '源码可用',
        bg: '#ecfdf5',
        text: '#047857',
        border: '#a7f3d0',
        icon: <CheckCircleFilled />,
    },
    enriched: {
        label: '已补全',
        bg: '#ecfdf5',
        text: '#047857',
        border: '#a7f3d0',
        icon: <CheckCircleFilled />,
    },
    NODE_STATUS_READY: {
        label: '可调度',
        bg: '#ecfdf5',
        text: '#047857',
        border: '#a7f3d0',
        icon: <CheckCircleFilled />,
    },
    PLUGIN_INSTANCE_STATE_READY: {
        label: '就绪',
        bg: '#ecfdf5',
        text: '#047857',
        border: '#a7f3d0',
        icon: <CheckCircleFilled />,
    },

    // 运行中 / 处理中 / 索引 (Sky Blue)
    processing: {
        label: '处理中',
        bg: '#eff6ff',
        text: '#1d4ed8',
        border: '#bfdbfe',
        icon: <SyncOutlined spin />,
    },
    fast_ready: { label: '基础素材', bg: '#eff6ff', text: '#1d4ed8', border: '#bfdbfe' },
    NODE_STATUS_ENROLLING: {
        label: '注册中',
        bg: '#eff6ff',
        text: '#1d4ed8',
        border: '#bfdbfe',
        icon: <SyncOutlined spin />,
    },
    PLUGIN_INSTANCE_STATE_PLANNED: {
        label: '已下发',
        bg: '#eff6ff',
        text: '#1d4ed8',
        border: '#bfdbfe',
    },
    PLUGIN_INSTANCE_STATE_INSTALLING: {
        label: '安装中',
        bg: '#eff6ff',
        text: '#1d4ed8',
        border: '#bfdbfe',
        icon: <SyncOutlined spin />,
    },

    // 警告 / 待办 / 审批 (Warm Amber)
    pending: {
        label: '等待上传',
        bg: '#fffbeb',
        text: '#b45309',
        border: '#fde68a',
        icon: <ClockCircleFilled />,
    },
    awaiting_admission: {
        label: '待媒体准入',
        bg: '#fffbeb',
        text: '#b45309',
        border: '#fde68a',
        icon: <ClockCircleFilled />,
    },
    unverified: {
        label: '未验证来源',
        bg: '#fffbeb',
        text: '#b45309',
        border: '#fde68a',
        icon: <ExclamationCircleFilled />,
    },
    partial: { label: '部分结果', bg: '#fffbeb', text: '#b45309', border: '#fde68a' },
    NODE_STATUS_CANDIDATE: {
        label: '待接纳',
        bg: '#fffbeb',
        text: '#b45309',
        border: '#fde68a',
        icon: <ClockCircleFilled />,
    },
    NODE_STATUS_DRAINING: {
        label: '排空中',
        bg: '#fffbeb',
        text: '#b45309',
        border: '#fde68a',
        icon: <ClockCircleFilled />,
    },
    PLUGIN_INSTANCE_STATE_DEGRADED: {
        label: '降级',
        bg: '#fffbeb',
        text: '#b45309',
        border: '#fde68a',
        icon: <ExclamationCircleFilled />,
    },
    PLUGIN_INSTANCE_STATE_DRAINING: {
        label: '排空中',
        bg: '#fffbeb',
        text: '#b45309',
        border: '#fde68a',
    },

    // 失败 / 错误 / 撤销 (Rose Red)
    failed: {
        label: '失败',
        bg: '#fef2f2',
        text: '#b91c1c',
        border: '#fecaca',
        icon: <CloseCircleFilled />,
    },
    rejected: {
        label: '已拒绝',
        bg: '#fef2f2',
        text: '#b91c1c',
        border: '#fecaca',
        icon: <CloseCircleFilled />,
    },
    NODE_STATUS_REVOKED: {
        label: '已撤销',
        bg: '#fef2f2',
        text: '#b91c1c',
        border: '#fecaca',
        icon: <CloseCircleFilled />,
    },
    PLUGIN_INSTANCE_STATE_FAILED: {
        label: '失败',
        bg: '#fef2f2',
        text: '#b91c1c',
        border: '#fecaca',
        icon: <CloseCircleFilled />,
    },

    // 中性 / 归档 / 离线 (Slate Neutral)
    draft: { label: '草稿', bg: '#f8fafc', text: '#475569', border: '#e2e8f0' },
    archived: { label: '已归档', bg: '#f8fafc', text: '#64748b', border: '#e2e8f0' },
    NODE_STATUS_OFFLINE: {
        label: '离线',
        bg: '#f8fafc',
        text: '#64748b',
        border: '#e2e8f0',
        icon: <CloseCircleFilled />,
    },
    PLUGIN_INSTANCE_STATE_STOPPED: {
        label: '已停止',
        bg: '#f8fafc',
        text: '#64748b',
        border: '#e2e8f0',
    },
    PLUGIN_INSTANCE_STATE_ROLLED_BACK: {
        label: '已回滚',
        bg: '#f8fafc',
        text: '#64748b',
        border: '#e2e8f0',
    },
    PLUGIN_INSTANCE_STATE_UNINSTALLED: {
        label: '已卸载',
        bg: '#f8fafc',
        text: '#64748b',
        border: '#e2e8f0',
    },
};

/**
 * 统一状态徽标：优雅细腻
 */
export function Badge({ state }: { state: string }) {
    const conf = statusConfig[state] || {
        label: state,
        bg: '#f8fafc',
        text: '#475569',
        border: '#e2e8f0',
    };
    return (
        <span
            style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                background: conf.bg,
                color: conf.text,
                border: `1px solid ${conf.border}`,
                padding: '2px 8px',
                borderRadius: 4,
                fontSize: 12,
                fontWeight: 500,
                lineHeight: '18px',
                whiteSpace: 'nowrap',
            }}
        >
            {conf.icon ? (
                <span style={{ fontSize: 11, display: 'inline-flex' }}>{conf.icon}</span>
            ) : null}
            <span>{conf.label}</span>
        </span>
    );
}

/**
 * 统一分页组件
 */
export function Pager({
    offset,
    total,
    onChange,
    pageSize = 20,
}: {
    offset: number;
    total: number;
    onChange: (offset: number) => void;
    pageSize?: number;
}) {
    const current = Math.floor(offset / pageSize) + 1;
    return (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
            <Pagination
                current={current}
                pageSize={pageSize}
                total={total}
                showSizeChanger={false}
                showTotal={(t) => `共 ${t} 条记录`}
                onChange={(page) => onChange((page - 1) * pageSize)}
            />
        </div>
    );
}

/**
 * 统一模态框包装
 */
export function Modal({
    title,
    children,
    onClose,
    width = 620,
}: {
    title: string;
    children: ReactNode;
    onClose: () => void;
    width?: number | string;
}) {
    return (
        <AntModal
            title={<span style={{ fontWeight: 650, color: '#0f172a' }}>{title}</span>}
            open
            footer={null}
            onCancel={onClose}
            width={width}
            destroyOnClose
            centered
        >
            <div style={{ paddingTop: 8 }}>{children}</div>
        </AntModal>
    );
}

/**
 * 现代企业级指标卡片组件：纯白底、深灰字、小巧图标容器、告别玩具化大彩色
 */
export function StatSummary({
    title,
    value,
    prefix,
    tag,
}: {
    title: string;
    value: number | string;
    prefix?: ReactNode;
    tag?: ReactNode;
    color?: string; // 保留向后兼容
}) {
    return (
        <Card
            size="small"
            style={{
                borderRadius: 8,
                border: '1px solid #e2e8f0',
                background: '#ffffff',
                boxShadow: '0 1px 2px 0 rgba(15, 23, 42, 0.03)',
            }}
            bodyStyle={{ padding: '14px 18px' }}
        >
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 8,
                }}
            >
                <span style={{ fontSize: 12, fontWeight: 500, color: '#64748b' }}>{title}</span>
                {prefix ? (
                    <div
                        style={{
                            width: 26,
                            height: 26,
                            borderRadius: 6,
                            background: '#f8fafc',
                            border: '1px solid #e2e8f0',
                            color: '#475569',
                            display: 'grid',
                            placeItems: 'center',
                            fontSize: 13,
                        }}
                    >
                        {prefix}
                    </div>
                ) : null}
            </div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                <span
                    style={{
                        color: '#0f172a',
                        fontWeight: 700,
                        fontSize: typeof value === 'number' || String(value).length <= 4 ? 24 : 18,
                        letterSpacing: '-0.5px',
                        fontFamily:
                            typeof value === 'string' && value.includes('_')
                                ? 'monospace'
                                : 'inherit',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                    }}
                >
                    {value}
                </span>
                {tag ? <span style={{ marginLeft: 'auto' }}>{tag}</span> : null}
            </div>
        </Card>
    );
}

export const date = (value: string) => {
    if (!value) return '-';
    try {
        return new Date(value).toLocaleString('zh-CN', { hour12: false });
    } catch {
        return value;
    }
};

export const bytes = (value: string | number) => {
    const n = Number(value);
    if (isNaN(n) || n === 0) return '0 B';
    if (n > 1073741824) return `${(n / 1073741824).toFixed(2)} GiB`;
    if (n > 1048576) return `${(n / 1048576).toFixed(1)} MB`;
    return `${(n / 1024).toFixed(1)} KB`;
};

export const time = (value: string | number) => {
    const n = Number(value);
    if (isNaN(n)) return '0.000s';
    return `${(n / 1000).toFixed(3)}s`;
};

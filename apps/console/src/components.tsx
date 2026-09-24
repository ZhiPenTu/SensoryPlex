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
    Statistic,
} from 'antd';
import {
    CheckCircleOutlined,
    ClockCircleOutlined,
    CloseCircleOutlined,
    ExclamationCircleOutlined,
    SyncOutlined,
} from '@ant-design/icons';
import { RequestError } from './api/client';

const { Title, Paragraph, Text } = Typography;

/**
 * 统一页面头部卡片组件
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
        <div className="page-header-wrap" style={{ marginBottom: 20 }}>
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
                                    fontWeight: 700,
                                    letterSpacing: '1.5px',
                                    color: '#1668dc',
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
                        style={{ margin: 0, fontWeight: 650, letterSpacing: '-0.5px' }}
                    >
                        {title}
                    </Title>
                    <Paragraph type="secondary" style={{ margin: '4px 0 0', fontSize: 13 }}>
                        {description}
                    </Paragraph>
                </div>
                {action ? (
                    <div
                        className="page-header-action"
                        style={{ display: 'flex', gap: 8, alignItems: 'center' }}
                    >
                        {action}
                    </div>
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
 * 统一错误提示
 */
export function ErrorNotice({ error }: { error: unknown }) {
    if (!error) return null;
    const message = error instanceof Error ? error.message : '请求未完成。';
    const trace = error instanceof RequestError && error.trace ? error.trace : null;

    return (
        <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16, borderRadius: 8 }}
            message={
                <div>
                    <span>{message}</span>
                    {trace ? (
                        <div style={{ marginTop: 4 }}>
                            <Tag color="error" style={{ fontFamily: 'monospace', fontSize: 11 }}>
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
 * 统一普通提示通知
 */
export function Notice({ children }: { children: ReactNode }) {
    return (
        <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16, borderRadius: 8 }}
            message={<span style={{ fontSize: 13 }}>{children}</span>}
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
                    <Text strong style={{ fontSize: 15, display: 'block', marginBottom: 4 }}>
                        {title}
                    </Text>
                    {children ? (
                        <Text type="secondary" style={{ fontSize: 13 }}>
                            {children}
                        </Text>
                    ) : null}
                </div>
            }
            style={{ padding: '36px 0' }}
        />
    );
}

const statusMap: Record<string, { label: string; color: string; icon?: React.ReactNode }> = {
    // 资产与素材状态
    pending: { label: '等待上传', color: 'default', icon: <ClockCircleOutlined /> },
    awaiting_admission: { label: '待媒体准入', color: 'warning', icon: <ClockCircleOutlined /> },
    draft: { label: '草稿', color: 'default', icon: <ClockCircleOutlined /> },
    published: { label: '已发布', color: 'success', icon: <CheckCircleOutlined /> },
    processing: { label: '处理中', color: 'processing', icon: <SyncOutlined spin /> },
    completed: { label: '已完成', color: 'success', icon: <CheckCircleOutlined /> },
    archived: { label: '已归档', color: 'default' },
    source_available: { label: '源码可用', color: 'cyan', icon: <CheckCircleOutlined /> },
    unverified: { label: '未验证来源', color: 'warning', icon: <ExclamationCircleOutlined /> },
    fast_ready: { label: '基础素材', color: 'blue', icon: <CheckCircleOutlined /> },
    partial: { label: '部分结果', color: 'warning', icon: <ClockCircleOutlined /> },
    enriched: { label: '已补全', color: 'success', icon: <CheckCircleOutlined /> },
    failed: { label: '失败', color: 'error', icon: <CloseCircleOutlined /> },
    rejected: { label: '已拒绝', color: 'error', icon: <CloseCircleOutlined /> },

    // 节点拓扑状态
    NODE_STATUS_CANDIDATE: { label: '待接纳', color: 'warning', icon: <ClockCircleOutlined /> },
    NODE_STATUS_ENROLLING: { label: '注册中', color: 'processing', icon: <SyncOutlined spin /> },
    NODE_STATUS_READY: { label: '可调度', color: 'success', icon: <CheckCircleOutlined /> },
    NODE_STATUS_DRAINING: { label: '排空中', color: 'warning', icon: <ClockCircleOutlined /> },
    NODE_STATUS_OFFLINE: { label: '离线', color: 'default', icon: <CloseCircleOutlined /> },
    NODE_STATUS_REVOKED: { label: '已撤销', color: 'error', icon: <CloseCircleOutlined /> },

    // 插件实例状态
    PLUGIN_INSTANCE_STATE_PLANNED: {
        label: '已下发',
        color: 'processing',
        icon: <ClockCircleOutlined />,
    },
    PLUGIN_INSTANCE_STATE_INSTALLING: {
        label: '安装中',
        color: 'processing',
        icon: <SyncOutlined spin />,
    },
    PLUGIN_INSTANCE_STATE_READY: { label: '就绪', color: 'success', icon: <CheckCircleOutlined /> },
    PLUGIN_INSTANCE_STATE_DEGRADED: {
        label: '降级',
        color: 'warning',
        icon: <ExclamationCircleOutlined />,
    },
    PLUGIN_INSTANCE_STATE_DRAINING: {
        label: '排空中',
        color: 'warning',
        icon: <ClockCircleOutlined />,
    },
    PLUGIN_INSTANCE_STATE_STOPPED: {
        label: '已停止',
        color: 'default',
        icon: <CloseCircleOutlined />,
    },
    PLUGIN_INSTANCE_STATE_FAILED: { label: '失败', color: 'error', icon: <CloseCircleOutlined /> },
    PLUGIN_INSTANCE_STATE_ROLLED_BACK: { label: '已回滚', color: 'default' },
    PLUGIN_INSTANCE_STATE_UNINSTALLED: { label: '已卸载', color: 'default' },
};

/**
 * 统一状态徽标
 */
export function Badge({ state }: { state: string }) {
    const conf = statusMap[state] || { label: state, color: 'default' };
    return (
        <Tag
            color={conf.color}
            icon={conf.icon}
            style={{
                borderRadius: 4,
                padding: '2px 8px',
                fontSize: 12,
                fontWeight: 500,
                display: 'inline-flex',
                alignItems: 'center',
                gap: 4,
            }}
        >
            {conf.label}
        </Tag>
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
            title={<span style={{ fontWeight: 600 }}>{title}</span>}
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
 * 概览指标卡片组件
 */
export function StatSummary({
    title,
    value,
    prefix,
    suffix,
    color,
}: {
    title: string;
    value: number | string;
    prefix?: ReactNode;
    suffix?: ReactNode;
    color?: string;
}) {
    return (
        <Card
            size="small"
            style={{ borderRadius: 8, boxShadow: '0 1px 3px 0 rgba(15, 23, 42, 0.04)' }}
        >
            <Statistic
                title={<span style={{ fontSize: 13, color: '#64748b' }}>{title}</span>}
                value={value}
                prefix={prefix}
                suffix={suffix}
                valueStyle={{ color: color || '#0f172a', fontWeight: 650, fontSize: 24 }}
            />
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

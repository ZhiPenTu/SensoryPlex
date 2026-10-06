import { useMemo } from 'react';
import { Card, Space, Tag, Tooltip, Typography } from 'antd';
import {
    CheckCircleFilled,
    ClockCircleFilled,
    CloseCircleFilled,
    ExclamationCircleFilled,
    FieldTimeOutlined,
    SyncOutlined,
} from '@ant-design/icons';
import type { JsonObject } from '../api/contracts';

const { Text } = Typography;

export type GanttTask = {
    task_id: string;
    node_id: string;
    attempt: number;
    max_attempts: number;
    required: boolean;
    state: string;
    reason_code?: string;
    error_detail?: string;
    output_ref?: string;
    created_at?: string;
    updated_at?: string;
};

export type GanttReceipt = {
    task_id: string;
    attempt: number;
    plugin_id: string;
    input_count: number;
    output_count: number;
    reason_code?: string;
    receipt_digest: string;
    started_at?: string;
    completed_at?: string;
};

interface ExecutionGanttChartProps {
    execution: {
        execution_id: string;
        run_id: string;
        state: string;
        pipeline_revision: number;
        graph_digest: string;
        created_at?: string;
        completed_at?: string;
        modality_summary?: JsonObject;
    };
    tasks: GanttTask[];
    receipts: GanttReceipt[];
}

function parseMs(isoStr?: string): number {
    if (!isoStr) return 0;
    const t = new Date(isoStr).getTime();
    return isNaN(t) ? 0 : t;
}

const STATE_CONFIG: Record<
    string,
    { color: string; bg: string; border: string; label: string; icon: React.ReactNode }
> = {
    succeeded: {
        color: '#10b981',
        bg: '#ecfdf5',
        border: '#a7f3d0',
        label: '已完成',
        icon: <CheckCircleFilled style={{ color: '#10b981' }} />,
    },
    running: {
        color: '#0284c7',
        bg: '#f0f9ff',
        border: '#bae6fd',
        label: '运行中',
        icon: <SyncOutlined spin style={{ color: '#0284c7' }} />,
    },
    assigned: {
        color: '#0284c7',
        bg: '#f0f9ff',
        border: '#bae6fd',
        label: '已认领',
        icon: <SyncOutlined style={{ color: '#0284c7' }} />,
    },
    retry_wait: {
        color: '#f59e0b',
        bg: '#fffbeb',
        border: '#fde68a',
        label: '等待重试',
        icon: <ClockCircleFilled style={{ color: '#f59e0b' }} />,
    },
    failed: {
        color: '#ef4444',
        bg: '#fef2f2',
        border: '#fecaca',
        label: '执行失败',
        icon: <CloseCircleFilled style={{ color: '#ef4444' }} />,
    },
    blocked: {
        color: '#64748b',
        bg: '#f8fafc',
        border: '#cbd5e1',
        label: '上游阻断',
        icon: <ExclamationCircleFilled style={{ color: '#64748b' }} />,
    },
    pending: {
        color: '#94a3b8',
        bg: '#f8fafc',
        border: '#e2e8f0',
        label: '就绪等待',
        icon: <ClockCircleFilled style={{ color: '#94a3b8' }} />,
    },
    ready: {
        color: '#3b82f6',
        bg: '#eff6ff',
        border: '#bfdbfe',
        label: '就绪唤醒',
        icon: <ClockCircleFilled style={{ color: '#3b82f6' }} />,
    },
};

export default function ExecutionGanttChart({ execution, tasks, receipts }: ExecutionGanttChartProps) {
    const { minTime, totalDurationMs, rows } = useMemo(() => {
        const receiptMap = new Map<string, GanttReceipt>();
        for (const r of receipts) {
            receiptMap.set(`${r.task_id}:${r.attempt}`, r);
            if (!receiptMap.has(r.task_id)) {
                receiptMap.set(r.task_id, r);
            }
        }

        let minT = Infinity;
        let maxT = -Infinity;

        const baseStart = parseMs(execution.created_at);
        if (baseStart > 0) minT = Math.min(minT, baseStart);

        const taskRows = tasks.map((task) => {
            const receipt = receiptMap.get(`${task.task_id}:${task.attempt}`) || receiptMap.get(task.task_id);
            const created = parseMs(task.created_at);
            const updated = parseMs(task.updated_at);
            const started = receipt ? parseMs(receipt.started_at) : created;
            const completed = receipt ? parseMs(receipt.completed_at) : (task.state === 'succeeded' || task.state === 'failed' ? updated : 0);

            const taskStart = started > 0 ? started : (created > 0 ? created : baseStart);
            const taskEnd = completed > 0 ? completed : (updated > 0 ? updated : taskStart + 100);

            if (taskStart > 0 && taskStart < minT) minT = taskStart;
            if (taskEnd > 0 && taskEnd > maxT) maxT = taskEnd;

            return {
                task,
                receipt,
                taskStart,
                taskEnd,
                durationMs: Math.max(1, taskEnd - taskStart),
            };
        });

        if (!isFinite(minT)) minT = Date.now() - 1000;
        if (!isFinite(maxT) || maxT <= minT) maxT = minT + 1000;

        const dur = Math.max(500, maxT - minT);

        return {
            minTime: minT,
            maxTime: maxT,
            totalDurationMs: dur,
            rows: taskRows,
        };
    }, [execution, tasks, receipts]);

    const formatOffset = (ms: number) => {
        if (ms < 1000) return `${ms}ms`;
        return `${(ms / 1000).toFixed(2)}s`;
    };

    return (
        <Card
            size="small"
            title={
                <Space>
                    <FieldTimeOutlined style={{ color: '#0284c7' }} />
                    <span style={{ fontWeight: 600 }}>全景执行甘特时序图</span>
                    <Tag color="cyan">跨度: {formatOffset(totalDurationMs)}</Tag>
                </Space>
            }
            bodyStyle={{ padding: '16px 20px' }}
        >
            <div style={{ position: 'relative', width: '100%', overflowX: 'auto' }}>
                {/* 时间轴刻度线 */}
                <div
                    style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        borderBottom: '1px solid #e2e8f0',
                        paddingBottom: 6,
                        marginBottom: 12,
                        fontSize: 11,
                        color: '#64748b',
                    }}
                >
                    <span>+0.0s (起点)</span>
                    <span>+{formatOffset(totalDurationMs * 0.25)}</span>
                    <span>+{formatOffset(totalDurationMs * 0.5)}</span>
                    <span>+{formatOffset(totalDurationMs * 0.75)}</span>
                    <span>+{formatOffset(totalDurationMs)} (终点)</span>
                </div>

                {/* 任务甘特行 */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    {rows.map(({ task, receipt, taskStart, durationMs }) => {
                        const stateCfg = STATE_CONFIG[task.state] || STATE_CONFIG.pending;
                        const leftPercent = Math.max(
                            0,
                            Math.min(95, ((taskStart - minTime) / totalDurationMs) * 100),
                        );
                        const rawWidthPercent = ((durationMs) / totalDurationMs) * 100;
                        const widthPercent = Math.max(4, Math.min(100 - leftPercent, rawWidthPercent));

                        const tooltipContent = (
                            <div style={{ fontSize: 12, lineHeight: 1.5 }}>
                                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                                    {task.node_id} (尝试: {task.attempt}/{task.max_attempts})
                                </div>
                                <div>状态: {stateCfg.label} ({task.state})</div>
                                <div>类型: {task.required ? '必需快路径' : '可选慢路径'}</div>
                                <div>耗时: {formatOffset(durationMs)}</div>
                                {receipt ? (
                                    <>
                                        <div>插件: {receipt.plugin_id}</div>
                                        <div>输入 / 输出: {receipt.input_count} / {receipt.output_count}</div>
                                        <div style={{ fontSize: 10, color: '#94a3b8' }}>
                                            回执摘要: {receipt.receipt_digest.slice(0, 16)}...
                                        </div>
                                    </>
                                ) : null}
                                {task.reason_code ? (
                                    <div style={{ color: '#f87171' }}>原因: {task.reason_code}</div>
                                ) : null}
                            </div>
                        );

                        return (
                            <div
                                key={`${task.task_id}:${task.attempt}`}
                                style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    height: 34,
                                    fontSize: 12,
                                }}
                            >
                                {/* 左侧任务标签 */}
                                <div
                                    style={{
                                        width: 170,
                                        flexShrink: 0,
                                        paddingRight: 12,
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                        display: 'flex',
                                        alignItems: 'center',
                                        gap: 6,
                                    }}
                                >
                                    {stateCfg.icon}
                                    <Text strong style={{ fontSize: 12 }}>
                                        {task.node_id}
                                    </Text>
                                    <Tag
                                        style={{ fontSize: 10, marginInlineEnd: 0, padding: '0 4px' }}
                                        color={task.required ? 'blue' : 'default'}
                                    >
                                        {task.required ? '必需' : '慢路径'}
                                    </Tag>
                                </div>

                                {/* 右侧时间跨度槽 */}
                                <div
                                    style={{
                                        flex: 1,
                                        position: 'relative',
                                        height: '100%',
                                        background: '#f8fafc',
                                        borderRadius: 6,
                                        display: 'flex',
                                        alignItems: 'center',
                                    }}
                                >
                                    <Tooltip title={tooltipContent} placement="top">
                                        <div
                                            style={{
                                                position: 'absolute',
                                                left: `${leftPercent}%`,
                                                width: `${widthPercent}%`,
                                                height: 24,
                                                background: stateCfg.bg,
                                                border: `1px solid ${stateCfg.border}`,
                                                borderLeft: `4px solid ${stateCfg.color}`,
                                                borderRadius: 4,
                                                display: 'flex',
                                                alignItems: 'center',
                                                padding: '0 8px',
                                                cursor: 'pointer',
                                                boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
                                                overflow: 'hidden',
                                                transition: 'all 0.2s ease',
                                            }}
                                        >
                                            <span
                                                style={{
                                                    fontSize: 11,
                                                    fontWeight: 600,
                                                    color: stateCfg.color,
                                                    whiteSpace: 'nowrap',
                                                }}
                                            >
                                                {formatOffset(durationMs)}
                                            </span>
                                        </div>
                                    </Tooltip>
                                </div>
                            </div>
                        );
                    })}
                </div>
            </div>
        </Card>
    );
}

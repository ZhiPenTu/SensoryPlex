import { useMemo } from 'react';
import { Card, Col, Row, Space, Tag, Typography } from 'antd';
import {
    CheckCircleFilled,
    ClockCircleFilled,
    CloseCircleFilled,
    ClusterOutlined,
    CompressOutlined,
    EyeOutlined,
    ForkOutlined,
    SoundOutlined,
    SyncOutlined,
} from '@ant-design/icons';
import type { GanttReceipt, GanttTask } from './ExecutionGanttChart';

const { Text } = Typography;

interface ExecutionTopologySwimlaneProps {
    tasks: GanttTask[];
    receipts: GanttReceipt[];
}

type StageDef = {
    key: string;
    title: string;
    description: string;
    icon: React.ReactNode;
    predicate: (taskId: string) => boolean;
};

const STAGES: StageDef[] = [
    {
        key: 'input',
        title: '0. 数据面输入',
        description: '原片定位与数据面 Buffer 租约',
        icon: <CompressOutlined style={{ color: '#6366f1' }} />,
        predicate: (id) => id.includes('source') || id.includes('demux') || id.includes('input'),
    },
    {
        key: 'extract',
        title: '1. L1 判别快路径',
        description: 'OCR 像素定位与 ASR 音频切片',
        icon: <EyeOutlined style={{ color: '#0ea5e9' }} />,
        predicate: (id) =>
            id.includes('ocr') ||
            id.includes('asr') ||
            id.includes('extract') ||
            id === 'rapidocr' ||
            id === 'whisper',
    },
    {
        key: 'fusion',
        title: '2. 时间轴融合',
        description: '1秒连续网格与事实追加入库',
        icon: <ForkOutlined style={{ color: '#10b981' }} />,
        predicate: (id) => id.includes('timeline') || id.includes('fusion'),
    },
    {
        key: 'enrichment',
        title: '3. L2 延迟补全慢路径',
        description: 'VLM 场景描述与异步向量索引',
        icon: <SoundOutlined style={{ color: '#f59e0b' }} />,
        predicate: (id) =>
            id.includes('vlm') || id.includes('enrich') || id.includes('slow') || id === 'moondream',
    },
];

const STATE_COLOR: Record<string, { color: string; border: string; bg: string; icon: React.ReactNode }> = {
    succeeded: {
        color: '#10b981',
        border: '#a7f3d0',
        bg: '#ecfdf5',
        icon: <CheckCircleFilled style={{ color: '#10b981' }} />,
    },
    running: {
        color: '#0284c7',
        border: '#bae6fd',
        bg: '#f0f9ff',
        icon: <SyncOutlined spin style={{ color: '#0284c7' }} />,
    },
    assigned: {
        color: '#0284c7',
        border: '#bae6fd',
        bg: '#f0f9ff',
        icon: <SyncOutlined style={{ color: '#0284c7' }} />,
    },
    retry_wait: {
        color: '#f59e0b',
        border: '#fde68a',
        bg: '#fffbeb',
        icon: <ClockCircleFilled style={{ color: '#f59e0b' }} />,
    },
    failed: {
        color: '#ef4444',
        border: '#fecaca',
        bg: '#fef2f2',
        icon: <CloseCircleFilled style={{ color: '#ef4444' }} />,
    },
    blocked: {
        color: '#64748b',
        border: '#cbd5e1',
        bg: '#f8fafc',
        icon: <ClockCircleFilled style={{ color: '#64748b' }} />,
    },
    pending: {
        color: '#94a3b8',
        border: '#e2e8f0',
        bg: '#f8fafc',
        icon: <ClockCircleFilled style={{ color: '#94a3b8' }} />,
    },
    ready: {
        color: '#3b82f6',
        border: '#bfdbfe',
        bg: '#eff6ff',
        icon: <ClockCircleFilled style={{ color: '#3b82f6' }} />,
    },
};

export default function ExecutionTopologySwimlane({ tasks, receipts }: ExecutionTopologySwimlaneProps) {
    const stageGroups = useMemo(() => {
        const receiptMap = new Map<string, GanttReceipt>();
        for (const r of receipts) {
            receiptMap.set(`${r.task_id}:${r.attempt}`, r);
            if (!receiptMap.has(r.task_id)) receiptMap.set(r.task_id, r);
        }

        const groups: Record<string, Array<{ task: GanttTask; receipt?: GanttReceipt }>> = {
            input: [],
            extract: [],
            fusion: [],
            enrichment: [],
            other: [],
        };

        for (const task of tasks) {
            const receipt = receiptMap.get(`${task.task_id}:${task.attempt}`) || receiptMap.get(task.task_id);
            let matched = false;
            for (const stage of STAGES) {
                if (stage.predicate(task.node_id.toLowerCase())) {
                    groups[stage.key].push({ task, receipt });
                    matched = true;
                    break;
                }
            }
            if (!matched) {
                groups.other.push({ task, receipt });
            }
        }
        return groups;
    }, [tasks, receipts]);

    return (
        <Card
            size="small"
            title={
                <Space>
                    <ClusterOutlined style={{ color: '#6366f1' }} />
                    <span style={{ fontWeight: 600 }}>DAG 执行拓扑阶段泳道流</span>
                </Space>
            }
            bodyStyle={{ padding: '16px 20px', background: '#fafbfc' }}
        >
            <Row gutter={[16, 16]}>
                {STAGES.map((stage) => {
                    const items = stageGroups[stage.key] || [];
                    return (
                        <Col key={stage.key} xs={24} sm={12} lg={6}>
                            <div
                                style={{
                                    background: '#ffffff',
                                    border: '1px solid #e2e8f0',
                                    borderRadius: 8,
                                    padding: '12px 14px',
                                    height: '100%',
                                    minHeight: 220,
                                    display: 'flex',
                                    flexDirection: 'column',
                                    boxShadow: '0 1px 3px rgba(0,0,0,0.03)',
                                }}
                            >
                                {/* 泳道头部 */}
                                <div style={{ borderBottom: '1px solid #f1f5f9', paddingBottom: 8, marginBottom: 10 }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                        <Space size={6}>
                                            {stage.icon}
                                            <Text strong style={{ fontSize: 13 }}>
                                                {stage.title}
                                            </Text>
                                        </Space>
                                        <Tag color={items.length ? 'blue' : 'default'} style={{ marginInlineEnd: 0 }}>
                                            {items.length} 任务
                                        </Tag>
                                    </div>
                                    <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
                                        {stage.description}
                                    </div>
                                </div>

                                {/* 任务卡片列表 */}
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 8, flex: 1 }}>
                                    {items.length === 0 ? (
                                        <div
                                            style={{
                                                fontSize: 12,
                                                color: '#94a3b8',
                                                fontStyle: 'italic',
                                                margin: 'auto 0',
                                                textAlign: 'center',
                                            }}
                                        >
                                            当前方案未配置该阶段节点
                                        </div>
                                    ) : (
                                        items.map(({ task, receipt }) => {
                                            const sc = STATE_COLOR[task.state] || STATE_COLOR.pending;
                                            return (
                                                <div
                                                    key={`${task.task_id}:${task.attempt}`}
                                                    style={{
                                                        background: sc.bg,
                                                        border: `1px solid ${sc.border}`,
                                                        borderRadius: 6,
                                                        padding: '8px 10px',
                                                        boxShadow: '0 1px 2px rgba(0,0,0,0.02)',
                                                    }}
                                                >
                                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                                        <Space size={4}>
                                                            {sc.icon}
                                                            <Text strong style={{ fontSize: 12 }}>
                                                                {task.node_id}
                                                            </Text>
                                                        </Space>
                                                        <Tag
                                                            style={{ fontSize: 10, marginInlineEnd: 0, padding: '0 3px' }}
                                                            color={task.required ? 'blue' : 'default'}
                                                        >
                                                            {task.required ? '必需' : '慢路径'}
                                                        </Tag>
                                                    </div>

                                                    <div style={{ fontSize: 11, color: '#64748b', marginTop: 4, display: 'flex', justifyContent: 'space-between' }}>
                                                        <span>尝试: {task.attempt}/{task.max_attempts}</span>
                                                        <span style={{ fontWeight: 500, color: sc.color }}>{task.state}</span>
                                                    </div>

                                                    {receipt ? (
                                                        <div style={{ fontSize: 10, color: '#475569', marginTop: 4, background: '#ffffff', padding: '2px 6px', borderRadius: 4, border: '1px solid #f1f5f9' }}>
                                                            IO: {receipt.input_count} / {receipt.output_count} · {receipt.plugin_id}
                                                        </div>
                                                    ) : null}

                                                    {task.reason_code ? (
                                                        <div style={{ fontSize: 10, color: '#ef4444', marginTop: 3 }}>
                                                            {task.reason_code}
                                                        </div>
                                                    ) : null}
                                                </div>
                                            );
                                        })
                                    )}
                                </div>
                            </div>
                        </Col>
                    );
                })}
            </Row>
        </Card>
    );
}

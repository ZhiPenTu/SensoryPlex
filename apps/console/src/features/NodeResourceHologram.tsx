import { useMemo } from 'react';
import { Card, Col, Progress, Row, Space, Tag, Typography } from 'antd';
import {
    AppstoreOutlined,
    ClusterOutlined,
    DashboardOutlined,
    HddOutlined,
    InfoCircleOutlined,
    ThunderboltOutlined,
} from '@ant-design/icons';
import type { NodeInfo } from '../api/contracts';

const { Text } = Typography;

interface NodeResourceHologramProps {
    nodes: NodeInfo[];
}

export function formatBytes(bytes?: string | number): string {
    const n = Number(bytes || 0);
    if (!n) return '0 GB';
    const gb = n / (1024 * 1024 * 1024);
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    const mb = n / (1024 * 1024);
    return `${mb.toFixed(0)} MB`;
}

export default function NodeResourceHologram({ nodes }: NodeResourceHologramProps) {
    const {
        totalCores,
        totalMemoryBytes,
        readyCount,
        coLocatedCount,
        totalSlots,
        activeSlots,
        instanceCount,
    } = useMemo(() => {
        let cores = 0;
        let mem = 0;
        let ready = 0;
        let coLocated = 0;
        let slots = 0;
        let active = 0;
        let instances = 0;

        for (const node of nodes) {
            if (node.status === 'NODE_STATUS_READY') ready++;
            if (node.is_co_located) coLocated++;
            const c = Number(node.capabilities?.cpu_cores || 0);
            cores += c;
            mem += Number(node.capabilities?.memory_bytes || 0);
            // 每个 CPU 核心默认承载 2 个并发任务槽位（最低保证 2 槽位）
            slots += Math.max(2, c * 2);
            active += node.instances?.length || 0;
            instances += node.instances?.length || 0;
        }

        return {
            totalCores: cores,
            totalMemoryBytes: mem,
            readyCount: ready,
            coLocatedCount: coLocated,
            totalSlots: slots,
            activeSlots: active,
            instanceCount: instances,
        };
    }, [nodes]);

    const loadPercent = totalSlots > 0 ? Math.round((activeSlots / totalSlots) * 100) : 0;
    const healthPercent = nodes.length > 0 ? Math.round((readyCount / nodes.length) * 100) : 100;

    return (
        <Card
            className="node-overview-card"
            style={{
                marginBottom: 16,
                borderRadius: 10,
                border: '1px solid var(--sp-border, #e2e8f0)',
                background: '#ffffff',
                boxShadow: '0 1px 3px rgba(15, 23, 42, 0.04)',
            }}
            styles={{ body: { padding: '16px 20px' } }}
        >
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 16,
                    flexWrap: 'wrap',
                    gap: 10,
                }}
            >
                <Space size={10} align="center">
                    <div
                        style={{
                            width: 32,
                            height: 32,
                            borderRadius: 8,
                            background: '#eff6ff',
                            border: '1px solid #dbeafe',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            color: '#2563eb',
                            fontSize: 16,
                        }}
                    >
                        <DashboardOutlined />
                    </div>
                    <div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <span style={{ fontSize: 15, fontWeight: 700, color: '#0f172a' }}>
                                集群算力全息负载看板
                            </span>
                            <Tag
                                color="blue"
                                bordered={false}
                                style={{
                                    fontSize: 11,
                                    padding: '1px 8px',
                                    borderRadius: 4,
                                    background: '#eff6ff',
                                    color: '#1d4ed8',
                                    fontWeight: 500,
                                }}
                            >
                                ADR-029 P3 拓扑观测面
                            </Tag>
                        </div>
                    </div>
                </Space>

                <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 12 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span
                            style={{
                                width: 8,
                                height: 8,
                                borderRadius: '50%',
                                background: healthPercent === 100 ? '#10b981' : '#f59e0b',
                                display: 'inline-block',
                            }}
                        />
                        <span style={{ color: '#64748b' }}>集群在线率:</span>
                        <Text
                            strong
                            style={{ color: healthPercent === 100 ? '#059669' : '#d97706' }}
                        >
                            {healthPercent}%
                        </Text>
                        <span style={{ color: '#94a3b8' }}>
                            ({readyCount} / {nodes.length} 在线)
                        </span>
                    </div>
                    <span style={{ color: '#cbd5e1' }}>|</span>
                    <span style={{ color: '#94a3b8' }}>心跳每 4 秒实时同步</span>
                </div>
            </div>

            {/* 核心指标 4 联卡片 */}
            <Row gutter={[12, 12]}>
                <Col xs={24} sm={12} lg={6}>
                    <div className="node-metric-box">
                        <div className="node-metric-top">
                            <span className="node-metric-label">健康拓扑节点</span>
                            <div
                                className="node-metric-icon"
                                style={{ color: '#2563eb', background: '#eff6ff' }}
                            >
                                <ClusterOutlined />
                            </div>
                        </div>
                        <div className="node-metric-value">
                            <span className="node-metric-number" style={{ color: '#2563eb' }}>
                                {readyCount}
                            </span>
                            <span className="node-metric-total">/ {nodes.length} 台</span>
                        </div>
                        <div className="node-metric-footer">
                            <span style={{ color: '#059669', fontWeight: 500 }}>
                                {coLocatedCount} 个同机数据面
                            </span>
                            <span style={{ color: '#94a3b8' }}>·</span>
                            <span style={{ color: '#64748b' }}>
                                {Math.max(0, nodes.length - coLocatedCount)} 远程节点
                            </span>
                        </div>
                    </div>
                </Col>

                <Col xs={24} sm={12} lg={6}>
                    <div className="node-metric-box">
                        <div className="node-metric-top">
                            <span className="node-metric-label">总算力核心 (CPU)</span>
                            <div
                                className="node-metric-icon"
                                style={{ color: '#0284c7', background: '#f0f9ff' }}
                            >
                                <ThunderboltOutlined />
                            </div>
                        </div>
                        <div className="node-metric-value">
                            <span className="node-metric-number">{totalCores}</span>
                            <span className="node-metric-total">Cores</span>
                        </div>
                        <div className="node-metric-footer">
                            <span style={{ color: '#64748b' }}>
                                支持 Metal / CUDA / CoreML 加速
                            </span>
                        </div>
                    </div>
                </Col>

                <Col xs={24} sm={12} lg={6}>
                    <div className="node-metric-box">
                        <div className="node-metric-top">
                            <span className="node-metric-label">统一/物理内存池</span>
                            <div
                                className="node-metric-icon"
                                style={{ color: '#059669', background: '#f0fdf4' }}
                            >
                                <HddOutlined />
                            </div>
                        </div>
                        <div className="node-metric-value">
                            <span className="node-metric-number">
                                {formatBytes(totalMemoryBytes)}
                            </span>
                        </div>
                        <div className="node-metric-footer">
                            <span style={{ color: '#059669' }}>动态门禁与水位保护已启用</span>
                        </div>
                    </div>
                </Col>

                <Col xs={24} sm={12} lg={6}>
                    <div className="node-metric-box">
                        <div className="node-metric-top">
                            <span className="node-metric-label">在飞任务槽位与负载</span>
                            <div
                                className="node-metric-icon"
                                style={{ color: '#d97706', background: '#fffbeb' }}
                            >
                                <AppstoreOutlined />
                            </div>
                        </div>
                        <div className="node-metric-value">
                            <span
                                className="node-metric-number"
                                style={{ color: activeSlots > 0 ? '#2563eb' : '#0f172a' }}
                            >
                                {activeSlots}
                            </span>
                            <span className="node-metric-total">/ {totalSlots} 槽位</span>
                        </div>
                        <div style={{ marginTop: 6 }}>
                            <Progress
                                percent={loadPercent}
                                size="small"
                                showInfo={false}
                                strokeColor={
                                    loadPercent > 80
                                        ? '#dc2626'
                                        : loadPercent > 50
                                          ? '#d97706'
                                          : '#2563eb'
                                }
                                trailColor="#e2e8f0"
                                style={{ margin: 0 }}
                            />
                            <div
                                style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    fontSize: 11,
                                    color: '#64748b',
                                    marginTop: 3,
                                }}
                            >
                                <span>{loadPercent}% 槽位占用</span>
                                <span>{instanceCount} 个模型实例</span>
                            </div>
                        </div>
                    </div>
                </Col>
            </Row>

            <div
                style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '8px 12px',
                    background: '#f8fafc',
                    borderRadius: 6,
                    border: '1px solid #f1f5f9',
                    fontSize: 12,
                    color: '#475569',
                    marginTop: 14,
                }}
            >
                <InfoCircleOutlined style={{ color: '#2563eb', flexShrink: 0 }} />
                <span>
                    <strong>架构隔离与调度原则：</strong>管理面 (Control Plane) 与执行面 (Worker)
                    物理隔离；通过双向心跳与动态准入，任务仅下发至可调度的同机或受控局域网节点。
                </span>
            </div>
        </Card>
    );
}

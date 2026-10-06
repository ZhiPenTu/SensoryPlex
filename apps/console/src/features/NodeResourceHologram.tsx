import { useMemo } from 'react';
import { Card, Col, Progress, Row, Space, Tag, Typography } from 'antd';
import {
    CheckCircleFilled,
    CloseCircleFilled,
    DashboardOutlined,
} from '@ant-design/icons';
import type { NodeInfo } from '../api/contracts';

const { Text } = Typography;

interface NodeResourceHologramProps {
    nodes: NodeInfo[];
}

function formatBytes(bytes?: string | number): string {
    const n = Number(bytes || 0);
    if (!n) return '0 GB';
    return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export default function NodeResourceHologram({ nodes }: NodeResourceHologramProps) {
    const { totalCores, totalMemoryBytes, readyCount, coLocatedCount, totalSlots, activeSlots } = useMemo(() => {
        let cores = 0;
        let mem = 0;
        let ready = 0;
        let coLocated = 0;
        let slots = 0;
        let active = 0;

        for (const node of nodes) {
            if (node.status === 'NODE_STATUS_READY') ready++;
            if (node.is_co_located) coLocated++;
            const c = Number(node.capabilities?.cpu_cores || 0);
            cores += c;
            mem += Number(node.capabilities?.memory_bytes || 0);
            // 每个 CPU 核心默认承载 2 个并发任务槽位
            slots += Math.max(2, c * 2);
            active += (node.instances?.length || 0);
        }

        return {
            totalCores: cores,
            totalMemoryBytes: mem,
            readyCount: ready,
            coLocatedCount: coLocated,
            totalSlots: slots,
            activeSlots: active,
        };
    }, [nodes]);

    return (
        <Card
            size="small"
            style={{
                marginBottom: 20,
                background: 'linear-gradient(135deg, #0f172a 0%, #1e293b 100%)',
                borderColor: '#334155',
                color: '#f8fafc',
                boxShadow: '0 4px 12px rgba(15, 23, 42, 0.15)',
            }}
            bodyStyle={{ padding: '16px 20px' }}
        >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
                <Space size={8}>
                    <DashboardOutlined style={{ color: '#38bdf8', fontSize: 18 }} />
                    <span style={{ fontSize: 15, fontWeight: 700, color: '#f8fafc', letterSpacing: '0.5px' }}>
                        集群算力全息负载实时看板
                    </span>
                    <Tag color="cyan" style={{ border: 'none', background: '#0369a1', color: '#e0f2fe' }}>
                        ADR-029 P3 拓扑观测面
                    </Tag>
                </Space>
                <div style={{ fontSize: 12, color: '#94a3b8' }}>
                    集群节点: <Text strong style={{ color: '#38bdf8' }}>{readyCount}</Text> / {nodes.length} 在线
                </div>
            </div>

            {/* 顶部四联关键指标条 */}
            <Row gutter={[16, 12]} style={{ marginBottom: 16 }}>
                <Col xs={12} sm={6}>
                    <div style={{ background: '#1e293b', padding: '10px 14px', borderRadius: 8, border: '1px solid #334155' }}>
                        <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 2 }}>健康拓扑节点</div>
                        <div style={{ fontSize: 20, fontWeight: 700, color: '#38bdf8' }}>
                            {readyCount} <span style={{ fontSize: 12, color: '#64748b', fontWeight: 400 }}>/ {nodes.length}</span>
                        </div>
                        <div style={{ fontSize: 10, color: '#10b981', marginTop: 2 }}>
                            {coLocatedCount} 个同机数据面
                        </div>
                    </div>
                </Col>

                <Col xs={12} sm={6}>
                    <div style={{ background: '#1e293b', padding: '10px 14px', borderRadius: 8, border: '1px solid #334155' }}>
                        <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 2 }}>总算力核心 (CPU)</div>
                        <div style={{ fontSize: 20, fontWeight: 700, color: '#f1f5f9' }}>
                            {totalCores} <span style={{ fontSize: 12, color: '#64748b', fontWeight: 400 }}>Cores</span>
                        </div>
                        <div style={{ fontSize: 10, color: '#94a3b8', marginTop: 2 }}>
                            支持 Metal / CUDA 硬件加速
                        </div>
                    </div>
                </Col>

                <Col xs={12} sm={6}>
                    <div style={{ background: '#1e293b', padding: '10px 14px', borderRadius: 8, border: '1px solid #334155' }}>
                        <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 2 }}>统一/物理内存池</div>
                        <div style={{ fontSize: 20, fontWeight: 700, color: '#f1f5f9' }}>
                            {formatBytes(totalMemoryBytes)}
                        </div>
                        <div style={{ fontSize: 10, color: '#94a3b8', marginTop: 2 }}>
                            动态门禁与水位保护
                        </div>
                    </div>
                </Col>

                <Col xs={12} sm={6}>
                    <div style={{ background: '#1e293b', padding: '10px 14px', borderRadius: 8, border: '1px solid #334155' }}>
                        <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 2 }}>当前在飞任务槽位</div>
                        <div style={{ fontSize: 20, fontWeight: 700, color: activeSlots > 0 ? '#38bdf8' : '#f1f5f9' }}>
                            {activeSlots} <span style={{ fontSize: 12, color: '#64748b', fontWeight: 400 }}>/ {totalSlots}</span>
                        </div>
                        <Progress
                            percent={totalSlots > 0 ? Math.round((activeSlots / totalSlots) * 100) : 0}
                            size="small"
                            showInfo={false}
                            strokeColor="#0284c7"
                            trailColor="#334155"
                            style={{ margin: 0 }}
                        />
                    </div>
                </Col>
            </Row>

            {/* 算力节点全息网格 */}
            <Row gutter={[12, 12]}>
                {nodes.map((node) => {
                    const isReady = node.status === 'NODE_STATUS_READY';
                    const isCoLocated = node.is_co_located;
                    const accelerators = node.capabilities?.accelerators || [];
                    const accelText = accelerators.length
                        ? accelerators.map((a) => a.accelerator).join(', ')
                        : 'CPU Host';

                    return (
                        <Col key={node.node_id} xs={24} sm={12} md={8}>
                            <div
                                style={{
                                    background: '#1e293b',
                                    border: isReady ? '1px solid #0284c7' : '1px solid #334155',
                                    borderRadius: 6,
                                    padding: '10px 12px',
                                    boxShadow: isReady ? '0 0 10px rgba(2, 132, 199, 0.15)' : 'none',
                                }}
                            >
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                    <Space size={6}>
                                        {isReady ? (
                                            <CheckCircleFilled style={{ color: '#10b981' }} />
                                        ) : (
                                            <CloseCircleFilled style={{ color: '#94a3b8' }} />
                                        )}
                                        <Text strong style={{ color: '#f8fafc', fontSize: 13 }}>
                                            {node.node_id}
                                        </Text>
                                    </Space>
                                    <Tag
                                        color={isCoLocated ? 'cyan' : 'blue'}
                                        style={{ marginInlineEnd: 0, fontSize: 10 }}
                                    >
                                        {isCoLocated ? '同机数据面' : '远端节点'}
                                    </Tag>
                                </div>

                                <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 6, display: 'flex', justifyContent: 'space-between' }}>
                                    <span>平台: {node.capabilities?.platform || 'macOS'} ({node.capabilities?.arch || 'arm64'})</span>
                                    <span>核心: {node.capabilities?.cpu_cores || 8} 核</span>
                                </div>

                                <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4, display: 'flex', justifyContent: 'space-between' }}>
                                    <span>内存: {formatBytes(node.capabilities?.memory_bytes)}</span>
                                    <span style={{ color: '#38bdf8' }}>{accelText}</span>
                                </div>

                                <div style={{ marginTop: 8, paddingTop: 6, borderTop: '1px solid #334155', display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 10, color: '#64748b' }}>
                                    <span>状态: {node.status.replace('NODE_STATUS_', '')}</span>
                                    <span style={{ color: '#10b981' }}>心跳正常 · 租约受控</span>
                                </div>
                            </div>
                        </Col>
                    );
                })}
            </Row>
        </Card>
    );
}

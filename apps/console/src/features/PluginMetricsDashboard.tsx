import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
    Card,
    Col,
    Progress,
    Radio,
    Row,
    Space,
    Table,
    Tag,
    Typography,
} from 'antd';
import {
    BarChartOutlined,
    CheckCircleFilled,
    CloseCircleFilled,
    FieldTimeOutlined,
    PieChartOutlined,
    RollbackOutlined,
    ThunderboltOutlined,
} from '@ant-design/icons';
import { ErrorNotice, Loading, StatSummary, time } from '../components';

const { Text } = Typography;

interface ParsedMetric {
    name: string;
    labels: Record<string, string>;
    value: number;
}

/** 简易且鲁棒的 Prometheus 文本解析器 */
function parsePrometheusText(text: string): ParsedMetric[] {
    const results: ParsedMetric[] = [];
    if (!text) return results;

    const lines = text.split('\n');
    for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line || line.startsWith('#')) continue;

        const match = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+(.+)$/);
        if (!match) continue;

        const name = match[1];
        const rawLabels = match[2] || '';
        const rawVal = parseFloat(match[3]);
        if (isNaN(rawVal)) continue;

        const labels: Record<string, string> = {};
        if (rawLabels) {
            const labelRegex = /([a-zA-Z_0-9]+)="([^"\\]*(?:\\.[^"\\]*)*)"/g;
            let m: RegExpExecArray | null;
            while ((m = labelRegex.exec(rawLabels)) !== null) {
                labels[m[1]] = m[2].replace(/\\"/g, '"').replace(/\\\\/g, '\\');
            }
        }

        results.push({ name, labels, value: rawVal });
    }
    return results;
}

export default function PluginMetricsDashboard() {
    const [windowHours, setWindowHours] = useState<number>(24);

    const metricsQuery = useQuery({
        queryKey: ['plugin-deployment-metrics', windowHours],
        queryFn: async ({ signal }) => {
            const res = await fetch(
                `/admin/v1/plugin-deployments/metrics?window_hours=${windowHours}`,
                { signal },
            );
            if (!res.ok) {
                throw new Error(`无法获取指标数据: HTTP ${res.status}`);
            }
            return res.text();
        },
        refetchInterval: 10000,
    });

    const parsed = useMemo(() => {
        return parsePrometheusText(metricsQuery.data || '');
    }, [metricsQuery.data]);

    // 聚合统计
    const aggregated = useMemo(() => {
        let totalOps = 0;
        let succeededOps = 0;
        let failedOps = 0;
        let provisionCount = 0;
        let upgradeCount = 0;
        let rollbackCount = 0;

        const phaseDurations: Record<string, { sumMs: number; count: number; maxMs: number }> = {
            staging: { sumMs: 0, count: 0, maxMs: 0 },
            starting: { sumMs: 0, count: 0, maxMs: 0 },
            validating: { sumMs: 0, count: 0, maxMs: 0 },
            draining: { sumMs: 0, count: 0, maxMs: 0 },
        };

        const failureReasons: Record<string, number> = {};

        for (const item of parsed) {
            if (item.name === 'sensoryplex_plugin_deployment_operations_total') {
                totalOps += item.value;
                const kind = item.labels.kind;
                const stage = item.labels.stage;
                const reason = item.labels.reason;

                if (kind === 'provision') provisionCount += item.value;
                if (kind === 'upgrade') upgradeCount += item.value;
                if (kind === 'rollback') rollbackCount += item.value;

                if (stage === 'succeeded') succeededOps += item.value;
                if (stage === 'failed') {
                    failedOps += item.value;
                    if (reason && reason !== 'none') {
                        failureReasons[reason] = (failureReasons[reason] || 0) + item.value;
                    }
                }
            } else if (item.name.startsWith('sensoryplex_plugin_deployment_phase_duration_ms_')) {
                const phase = item.labels.phase;
                if (phase && phaseDurations[phase]) {
                    if (item.name.endsWith('_sum')) {
                        phaseDurations[phase].sumMs += item.value;
                    } else if (item.name.endsWith('_count')) {
                        phaseDurations[phase].count += item.value;
                    } else if (item.name.endsWith('_max')) {
                        phaseDurations[phase].maxMs = Math.max(phaseDurations[phase].maxMs, item.value);
                    }
                }
            }
        }

        const successRate = totalOps > 0 ? ((succeededOps / totalOps) * 100).toFixed(1) : '100.0';

        return {
            totalOps,
            succeededOps,
            failedOps,
            successRate,
            provisionCount,
            upgradeCount,
            rollbackCount,
            phaseDurations,
            failureReasons,
        };
    }, [parsed]);

    const phaseList = [
        {
            key: 'staging',
            name: '取制品/离线安装 (Staging)',
            data: aggregated.phaseDurations.staging,
            color: '#3b82f6',
        },
        {
            key: 'starting',
            name: '平台服务托管 (Starting)',
            data: aggregated.phaseDurations.starting,
            color: '#6366f1',
        },
        {
            key: 'validating',
            name: '候选实例验证 (Validating)',
            data: aggregated.phaseDurations.validating,
            color: '#8b5cf6',
        },
        {
            key: 'draining',
            name: '排空旧版本 (Draining)',
            data: aggregated.phaseDurations.draining,
            color: '#eab308',
        },
    ];

    const failureReasonRows = Object.entries(aggregated.failureReasons).map(([reason, count]) => ({
        reason,
        count,
    }));

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {/* 顶栏控制 */}
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    background: '#f8fafc',
                    padding: '8px 12px',
                    borderRadius: 6,
                    border: '1px solid #e2e8f0',
                }}
            >
                <Space size={8}>
                    <BarChartOutlined style={{ color: '#2563eb' }} />
                    <Text strong style={{ fontSize: 13 }}>
                        热部署效能与质量大盘 (Prometheus Metrics)
                    </Text>
                </Space>

                <Space size={10}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                        统计时间窗口:
                    </Text>
                    <Radio.Group
                        size="small"
                        value={windowHours}
                        onChange={(e) => setWindowHours(e.target.value)}
                    >
                        <Radio.Button value={6}>最近 6 小时</Radio.Button>
                        <Radio.Button value={24}>最近 24 小时</Radio.Button>
                        <Radio.Button value={168}>最近 7 天</Radio.Button>
                        <Radio.Button value={720}>最近 30 天</Radio.Button>
                    </Radio.Group>
                </Space>
            </div>

            <ErrorNotice error={metricsQuery.error} />

            {metricsQuery.isPending ? (
                <Loading tip="正在聚合 Prometheus 部署指标…" />
            ) : (
                <>
                    {/* 核心 SLA 指标卡片 */}
                    <Row gutter={[8, 8]}>
                        <Col xs={24} sm={6}>
                            <StatSummary
                                title="总部署操作数"
                                value={aggregated.totalOps}
                                prefix={<ThunderboltOutlined style={{ color: '#2563eb' }} />}
                            />
                        </Col>
                        <Col xs={24} sm={6}>
                            <StatSummary
                                title="发布成功率"
                                value={`${aggregated.successRate}%`}
                                prefix={<CheckCircleFilled style={{ color: '#059669' }} />}
                                color="#059669"
                            />
                        </Col>
                        <Col xs={24} sm={6}>
                            <StatSummary
                                title="失败操作数"
                                value={aggregated.failedOps}
                                prefix={<CloseCircleFilled style={{ color: aggregated.failedOps > 0 ? '#dc2626' : '#94a3b8' }} />}
                                color={aggregated.failedOps > 0 ? '#dc2626' : '#94a3b8'}
                            />
                        </Col>
                        <Col xs={24} sm={6}>
                            <StatSummary
                                title="反向回滚次数"
                                value={aggregated.rollbackCount}
                                prefix={<RollbackOutlined style={{ color: aggregated.rollbackCount > 0 ? '#d97706' : '#94a3b8' }} />}
                                color={aggregated.rollbackCount > 0 ? '#d97706' : '#94a3b8'}
                            />
                        </Col>
                    </Row>

                    {/* 操作类别构成与耗时分布 */}
                    <Row gutter={[10, 10]}>
                        {/* 左侧：操作类别构成 */}
                        <Col xs={24} md={8}>
                            <Card
                                size="small"
                                title={
                                    <Space size={6}>
                                        <PieChartOutlined style={{ color: '#2563eb' }} />
                                        <span style={{ fontSize: 13 }}>操作类型构成</span>
                                    </Space>
                                }
                                style={{ borderRadius: 8, height: '100%' }}
                            >
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: '8px 0' }}>
                                    <div>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                                            <span>首次装配 (Provision):</span>
                                            <span className="mono">{aggregated.provisionCount} 次</span>
                                        </div>
                                        <Progress
                                            percent={
                                                aggregated.totalOps > 0
                                                    ? Math.round((aggregated.provisionCount / aggregated.totalOps) * 100)
                                                    : 0
                                            }
                                            strokeColor="#3b82f6"
                                            size="small"
                                        />
                                    </div>

                                    <div>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                                            <span>蓝绿升级 (Upgrade):</span>
                                            <span className="mono">{aggregated.upgradeCount} 次</span>
                                        </div>
                                        <Progress
                                            percent={
                                                aggregated.totalOps > 0
                                                    ? Math.round((aggregated.upgradeCount / aggregated.totalOps) * 100)
                                                    : 0
                                            }
                                            strokeColor="#10b981"
                                            size="small"
                                        />
                                    </div>

                                    <div>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                                            <span>安全回滚 (Rollback):</span>
                                            <span className="mono">{aggregated.rollbackCount} 次</span>
                                        </div>
                                        <Progress
                                            percent={
                                                aggregated.totalOps > 0
                                                    ? Math.round((aggregated.rollbackCount / aggregated.totalOps) * 100)
                                                    : 0
                                            }
                                            strokeColor="#f59e0b"
                                            size="small"
                                        />
                                    </div>
                                </div>
                            </Card>
                        </Col>

                        {/* 右侧：阶段耗时分布瀑布 */}
                        <Col xs={24} md={16}>
                            <Card
                                size="small"
                                title={
                                    <Space size={6}>
                                        <FieldTimeOutlined style={{ color: '#059669' }} />
                                        <span style={{ fontSize: 13 }}>各阶段平均与最大耗时 (Phase Durations)</span>
                                    </Space>
                                }
                                style={{ borderRadius: 8, height: '100%' }}
                            >
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                                    {phaseList.map((p) => {
                                        const avgMs =
                                            p.data.count > 0 ? Math.round(p.data.sumMs / p.data.count) : 0;
                                        return (
                                            <div
                                                key={p.key}
                                                style={{
                                                    background: '#f8fafc',
                                                    padding: '8px 12px',
                                                    borderRadius: 6,
                                                    border: '1px solid #e2e8f0',
                                                }}
                                            >
                                                <div
                                                    style={{
                                                        display: 'flex',
                                                        justifyContent: 'space-between',
                                                        alignItems: 'center',
                                                        marginBottom: 4,
                                                    }}
                                                >
                                                    <Text strong style={{ fontSize: 12 }}>
                                                        {p.name}
                                                    </Text>
                                                    <Space size={12}>
                                                        <Text type="secondary" style={{ fontSize: 11 }}>
                                                            样本数: {p.data.count}
                                                        </Text>
                                                        <span style={{ fontSize: 12 }}>
                                                            平均: <Text strong>{time(avgMs)}</Text>
                                                        </span>
                                                        <span style={{ fontSize: 12 }}>
                                                            峰值 Max: <Text strong style={{ color: '#d97706' }}>{time(p.data.maxMs)}</Text>
                                                        </span>
                                                    </Space>
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            </Card>
                        </Col>
                    </Row>

                    {/* 失败归因表 */}
                    {failureReasonRows.length > 0 ? (
                        <Card
                            size="small"
                            title={
                                <Space size={6}>
                                    <CloseCircleFilled style={{ color: '#dc2626' }} />
                                    <span style={{ fontSize: 13 }}>失败根本原因排行 (Failure Reasons)</span>
                                </Space>
                            }
                            style={{ borderRadius: 8 }}
                        >
                            <Table
                                rowKey="reason"
                                columns={[
                                    {
                                        title: '错误归因码 (Reason Code)',
                                        dataIndex: 'reason',
                                        render: (r: string) => (
                                            <Tag color="red" style={{ fontFamily: 'monospace' }}>
                                                {r}
                                            </Tag>
                                        ),
                                    },
                                    {
                                        title: '发生次数',
                                        dataIndex: 'count',
                                        width: 120,
                                        render: (cnt: number) => <Text strong>{cnt} 次</Text>,
                                    },
                                ]}
                                dataSource={failureReasonRows}
                                pagination={false}
                                size="small"
                            />
                        </Card>
                    ) : null}
                </>
            )}
        </div>
    );
}

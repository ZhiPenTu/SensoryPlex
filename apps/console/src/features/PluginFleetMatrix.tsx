import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Button,
    Card,
    Col,
    Popconfirm,
    Row,
    Space,
    Table,
    Tag,
    Tooltip,
    Typography,
    message,
} from 'antd';
import {
    AppstoreOutlined,
    CheckCircleFilled,
    CloudDownloadOutlined,
    CloudUploadOutlined,
    ClusterOutlined,
    ExclamationCircleFilled,
    LoadingOutlined,
    ReloadOutlined,
    SettingOutlined,
    ThunderboltOutlined,
    WarningFilled,
} from '@ant-design/icons';
import { api, post } from '../api/client';
import type {
    BatchDeployPluginsResponse,
    NodeInfo,
    NodeList,
    PluginDeploymentOperation,
    PluginDeploymentOperationList,
    PluginEntry,
    PluginList,
    PluginRelease,
    PluginReleaseList,
} from '../api/contracts';
import { ErrorNotice, Loading, StatSummary } from '../components';
import { usePermission } from '../session';

const { Text } = Typography;

interface PluginFleetMatrixProps {
    onSelectDeploy?: (nodeId: string, plugin: PluginEntry) => void;
    onViewDeployment?: (op: PluginDeploymentOperation) => void;
    onSwitchToDeployments?: () => void;
}

export default function PluginFleetMatrix({
    onSelectDeploy,
    onViewDeployment,
    onSwitchToDeployments,
}: PluginFleetMatrixProps) {
    const cache = useQueryClient();
    const canManage = usePermission('plugins:manage');
    const [batchAligning, setBatchAligning] = useState(false);

    // 1. 节点列表
    const nodes = useQuery({
        queryKey: ['nodes'],
        queryFn: ({ signal }) => api<NodeList>('/admin/v1/nodes?limit=100', { signal }),
        refetchInterval: 3000,
    });

    // 2. 插件规范目录
    const catalog = useQuery({
        queryKey: ['catalog'],
        queryFn: ({ signal }) => api<PluginList>('/admin/v1/catalog', { signal }),
    });

    // 3. 发布版本列表
    const releases = useQuery({
        queryKey: ['plugin-releases'],
        queryFn: ({ signal }) =>
            api<PluginReleaseList>('/admin/v1/plugin-releases?limit=100', { signal }),
    });

    // 4. 进行中的部署操作列表
    const operations = useQuery({
        queryKey: ['plugin-deployments'],
        queryFn: ({ signal }) =>
            api<PluginDeploymentOperationList>('/admin/v1/plugin-deployments?limit=50', { signal }),
        refetchInterval: 2500,
    });

    // 一键装配本机基础插件
    const batchDeploy = useMutation({
        mutationFn: async () => {
            await post('/admin/v1/plugin-releases:sync', {});
            return post<BatchDeployPluginsResponse>(
                '/admin/v1/nodes/local-host/plugins:batch-deploy',
                {},
            );
        },
        onSuccess: (data) => {
            message.success(
                `一键装配指令已下发：${data.operations.length} 个操作已受理，${data.already_ready.length} 个已就绪`,
            );
            void cache.invalidateQueries({ queryKey: ['nodes'] });
            void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
            if (onSwitchToDeployments && data.operations.length > 0) {
                onSwitchToDeployments();
            }
        },
    });

    // 单个升级/部署 Mutation
    const quickUpgrade = useMutation({
        mutationFn: ({
            nodeId,
            pluginId,
            releaseId,
            isUpgrade,
        }: {
            nodeId: string;
            pluginId: string;
            releaseId: string;
            isUpgrade: boolean;
        }) =>
            post(
                `/admin/v1/nodes/${nodeId}/plugins/${pluginId}:${isUpgrade ? 'upgrade' : 'provision'}`,
                { release_id: releaseId },
            ),
        onSuccess: (_data, variables) => {
            message.success(
                `已成功对节点 ${variables.nodeId} 下发 ${variables.isUpgrade ? '蓝绿升级' : '部署'} 指令`,
            );
            void cache.invalidateQueries({ queryKey: ['nodes'] });
            void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
            if (onSwitchToDeployments) {
                onSwitchToDeployments();
            }
        },
    });

    const activeNodes = (nodes.data?.items || []).filter(
        (n) => n.status !== 'NODE_STATUS_CANDIDATE',
    );
    const catalogItems = catalog.data?.items || [];
    const releaseItems = releases.data?.items || [];
    const opsItems = operations.data?.items || [];

    // 计算每个 (node_id, plugin_id) 当前是否有在执行中的操作
    const activeOpMap = useMemo(() => {
        const map = new Map<string, PluginDeploymentOperation>();
        const inFlightStages = new Set([
            'PLUGIN_OPERATION_STAGE_ACCEPTED',
            'PLUGIN_OPERATION_STAGE_STAGING',
            'PLUGIN_OPERATION_STAGE_STARTING',
            'PLUGIN_OPERATION_STAGE_VALIDATING',
            'PLUGIN_OPERATION_STAGE_CANDIDATE_READY',
            'PLUGIN_OPERATION_STAGE_CUTTING_OVER',
            'PLUGIN_OPERATION_STAGE_DRAINING_OLD',
        ]);
        for (const op of opsItems) {
            if (inFlightStages.has(op.stage)) {
                map.set(`${op.node_id}:${op.plugin_id}`, op);
            }
        }
        return map;
    }, [opsItems]);

    // 计算每个 (platform, arch, plugin_id) 下最新已认证的 release
    const latestReleaseMap = useMemo(() => {
        const map = new Map<string, PluginRelease>();
        for (const rel of releaseItems) {
            if (!rel.authenticated || rel.trust !== 'first_party') continue;
            const key = `${rel.platform}:${rel.arch}:${rel.plugin_id}`;
            const existing = map.get(key);
            if (!existing || new Date(rel.published_at) > new Date(existing.published_at)) {
                map.set(key, rel);
            }
        }
        return map;
    }, [releaseItems]);

    // 统计总体指标
    const totalSlots = activeNodes.length * catalogItems.length;
    let installedSlotsCount = 0;
    let driftCount = 0;

    activeNodes.forEach((node) => {
        const nodePlatform = node.capabilities?.platform || 'darwin';
        const nodeArch = node.capabilities?.arch || 'arm64';
        catalogItems.forEach((plugin) => {
            const inst = node.instances?.find((i) => i.plugin_id === plugin.id);
            if (inst && inst.active_runtime_instance_id && inst.actual_state === 'ready') {
                installedSlotsCount++;
                const latest = latestReleaseMap.get(`${nodePlatform}:${nodeArch}:${plugin.id}`);
                if (latest && inst.active_release_id && inst.active_release_id !== latest.release_id) {
                    driftCount++;
                }
            }
        });
    });

    // 批量对齐滞后节点
    const handleBatchAlignDrift = async () => {
        setBatchAligning(true);
        let triggered = 0;
        try {
            for (const node of activeNodes) {
                const nodePlatform = node.capabilities?.platform || 'darwin';
                const nodeArch = node.capabilities?.arch || 'arm64';
                for (const plugin of catalogItems) {
                    const inst = node.instances?.find((i) => i.plugin_id === plugin.id);
                    if (inst && inst.active_runtime_instance_id && inst.actual_state === 'ready') {
                        const latest = latestReleaseMap.get(`${nodePlatform}:${nodeArch}:${plugin.id}`);
                        if (
                            latest &&
                            inst.active_release_id &&
                            inst.active_release_id !== latest.release_id &&
                            !activeOpMap.has(`${node.node_id}:${plugin.id}`)
                        ) {
                            await post(
                                `/admin/v1/nodes/${node.node_id}/plugins/${plugin.id}:upgrade`,
                                { release_id: latest.release_id },
                            );
                            triggered++;
                        }
                    }
                }
            }
            if (triggered > 0) {
                message.success(`已为 ${triggered} 个版本漂移槽位自动发起蓝绿升级`);
                void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
                void cache.invalidateQueries({ queryKey: ['nodes'] });
                if (onSwitchToDeployments) onSwitchToDeployments();
            } else {
                message.info('当前所有已装配节点版本均已对齐最新，无待升级项。');
            }
        } catch (err: unknown) {
            message.error(`批量对齐发生错误: ${String(err)}`);
        } finally {
            setBatchAligning(false);
        }
    };

    // 构建表格列：第一列为节点基础信息，后续每一列为一种插件
    const columns = [
        {
            title: '计算节点 (Compute Node)',
            key: 'node_info',
            fixed: 'left' as const,
            width: 250,
            render: (_: unknown, node: NodeInfo) => {
                const isReady = node.status === 'NODE_STATUS_READY';
                const isColocated = node.is_co_located;
                const platform = node.capabilities?.platform || 'darwin';
                const arch = node.capabilities?.arch || 'arm64';
                const accelerators = (node.capabilities?.accelerators || [])
                    .map((a) => a.accelerator)
                    .join(', ');

                return (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                        <Space size={6}>
                            <Text strong style={{ fontSize: 13 }}>
                                {node.display_name || node.node_id}
                            </Text>
                            <Tag color={isReady ? 'success' : 'default'} style={{ margin: 0, fontSize: 10 }}>
                                {isReady ? '在线就绪' : node.status}
                            </Tag>
                            {isColocated ? (
                                <Tag color="blue" style={{ margin: 0, fontSize: 10 }}>
                                    同机底座
                                </Tag>
                            ) : null}
                        </Space>
                        <span className="mono" style={{ fontSize: 11, color: '#64748b' }}>
                            {node.node_id}
                        </span>
                        <Space size={4} wrap style={{ fontSize: 11, color: '#94a3b8' }}>
                            <span>
                                {platform}-{arch}
                            </span>
                            {accelerators ? <span>· {accelerators}</span> : null}
                        </Space>
                    </div>
                );
            },
        },
        ...catalogItems.map((plugin) => ({
            title: (
                <div style={{ textAlign: 'center' as const }}>
                    <div style={{ fontWeight: 600, fontSize: 12.5 }}>{plugin.name}</div>
                    <span className="mono" style={{ fontSize: 10, color: '#64748b' }}>
                        {plugin.id.replace('org.sensoryplex.', '')}
                    </span>
                </div>
            ),
            key: plugin.id,
            width: 200,
            render: (_: unknown, node: NodeInfo) => {
                const nodePlatform = node.capabilities?.platform || 'darwin';
                const nodeArch = node.capabilities?.arch || 'arm64';
                const inst = node.instances?.find((i) => i.plugin_id === plugin.id);
                const activeOp = activeOpMap.get(`${node.node_id}:${plugin.id}`);
                const latestRelease = latestReleaseMap.get(`${nodePlatform}:${nodeArch}:${plugin.id}`);

                // 1. 如果该槽位当前正有操作在运行
                if (activeOp) {
                    return (
                        <Card
                            size="small"
                            style={{
                                background: '#eff6ff',
                                borderColor: '#93c5fd',
                                borderRadius: 6,
                                textAlign: 'center',
                            }}
                            bodyStyle={{ padding: '8px 6px' }}
                        >
                            <Space orientation="vertical" size={2} style={{ width: '100%' }}>
                                <Space size={4}>
                                    <LoadingOutlined style={{ color: '#2563eb' }} />
                                    <Text strong style={{ fontSize: 12, color: '#1d4ed8' }}>
                                        {activeOp.kind === 'rollback'
                                            ? '回滚中'
                                            : activeOp.kind === 'upgrade'
                                              ? '蓝绿升级中'
                                              : '部署中'}
                                    </Text>
                                </Space>
                                <Tag color="blue" style={{ margin: '2px 0', fontSize: 10 }}>
                                    第 {activeOp.generation} 代
                                </Tag>
                                {onViewDeployment ? (
                                    <a
                                        onClick={() => onViewDeployment(activeOp)}
                                        style={{ fontSize: 11, display: 'block', marginTop: 2 }}
                                    >
                                        查看流水线 →
                                    </a>
                                ) : null}
                            </Space>
                        </Card>
                    );
                }

                // 2. 如果该槽位已安装且在运行中
                if (inst && inst.active_runtime_instance_id && inst.actual_state === 'ready') {
                    const isDrifted =
                        latestRelease &&
                        inst.active_release_id &&
                        inst.active_release_id !== latestRelease.release_id;

                    return (
                        <Card
                            size="small"
                            style={{
                                background: isDrifted ? '#fffbeb' : '#f0fdf4',
                                borderColor: isDrifted ? '#fcd34d' : '#86efac',
                                borderRadius: 6,
                            }}
                            bodyStyle={{ padding: '6px 8px' }}
                        >
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                    <Space size={4}>
                                        {isDrifted ? (
                                            <Tooltip title="存在更高版本的已认证 release 可升级">
                                                <WarningFilled style={{ color: '#d97706', fontSize: 12 }} />
                                            </Tooltip>
                                        ) : (
                                            <CheckCircleFilled style={{ color: '#16a34a', fontSize: 12 }} />
                                        )}
                                        <Text strong style={{ fontSize: 12 }}>
                                            {inst.plugin_version || 'v1.0.0'}
                                        </Text>
                                    </Space>
                                    <Tag color={isDrifted ? 'orange' : 'green'} style={{ margin: 0, fontSize: 10 }}>
                                        第 {inst.generation} 代
                                    </Tag>
                                </div>

                                <div style={{ fontSize: 10.5, color: '#64748b' }}>
                                    端点: <span className="mono">{inst.endpoint || '端口未上报'}</span>
                                </div>

                                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 4, marginTop: 4 }}>
                                    {isDrifted && latestRelease && canManage ? (
                                        <Popconfirm
                                            title="对齐并升级到最新版本？"
                                            description={`将创建第 ${Number(inst.generation || 0) + 1} 代候选实例并走 ADR-030 蓝绿切换。`}
                                            onConfirm={() =>
                                                quickUpgrade.mutate({
                                                    nodeId: node.node_id,
                                                    pluginId: plugin.id,
                                                    releaseId: latestRelease.release_id,
                                                    isUpgrade: true,
                                                })
                                            }
                                        >
                                            <Button
                                                size="small"
                                                type="primary"
                                                style={{ fontSize: 11, height: 22, padding: '0 6px', background: '#d97706' }}
                                                loading={quickUpgrade.isPending}
                                            >
                                                升级至新版
                                            </Button>
                                        </Popconfirm>
                                    ) : null}

                                    {onSelectDeploy ? (
                                        <Button
                                            size="small"
                                            icon={<SettingOutlined />}
                                            style={{ fontSize: 11, height: 22, padding: '0 6px' }}
                                            onClick={() => onSelectDeploy(node.node_id, plugin)}
                                        >
                                            配置
                                        </Button>
                                    ) : null}
                                </div>
                            </div>
                        </Card>
                    );
                }

                // 3. 如果未安装或处于失败
                if (inst && inst.error_code) {
                    return (
                        <Card
                            size="small"
                            style={{ background: '#fef2f2', borderColor: '#fca5a5', borderRadius: 6 }}
                            bodyStyle={{ padding: '6px 8px' }}
                        >
                            <Space orientation="vertical" size={2} style={{ width: '100%' }}>
                                <Space size={4}>
                                    <ExclamationCircleFilled style={{ color: '#dc2626' }} />
                                    <Text style={{ fontSize: 11, color: '#991b1b', fontWeight: 600 }}>
                                        {inst.error_code}
                                    </Text>
                                </Space>
                                <span style={{ fontSize: 10, color: '#7f1d1d' }}>
                                    {inst.error_detail ? `${inst.error_detail.slice(0, 30)}…` : '安装异常'}
                                </span>
                                {onSelectDeploy && canManage ? (
                                    <Button
                                        size="small"
                                        danger
                                        style={{ fontSize: 10.5, height: 20, padding: '0 6px', marginTop: 2 }}
                                        onClick={() => onSelectDeploy(node.node_id, plugin)}
                                    >
                                        重新尝试部署
                                    </Button>
                                ) : null}
                            </Space>
                        </Card>
                    );
                }

                // 4. 完全未安装状态
                return (
                    <div
                        style={{
                            padding: '10px 8px',
                            background: '#f8fafc',
                            border: '1px dashed #e2e8f0',
                            borderRadius: 6,
                            textAlign: 'center',
                        }}
                    >
                        <Text type="secondary" style={{ fontSize: 11, display: 'block', marginBottom: 4 }}>
                            未装配
                        </Text>
                        {latestRelease && canManage ? (
                            <Popconfirm
                                title={`向 ${node.node_id} 装配 ${plugin.name}？`}
                                description="将创建候选实例并走 ADR-030 离线安装与候选验证。"
                                onConfirm={() =>
                                    quickUpgrade.mutate({
                                        nodeId: node.node_id,
                                        pluginId: plugin.id,
                                        releaseId: latestRelease.release_id,
                                        isUpgrade: false,
                                    })
                                }
                            >
                                <Button
                                    size="small"
                                    icon={<CloudDownloadOutlined />}
                                    style={{ fontSize: 11, height: 22, padding: '0 8px' }}
                                    loading={quickUpgrade.isPending}
                                >
                                    快速装配
                                </Button>
                            </Popconfirm>
                        ) : (
                            <span style={{ fontSize: 10, color: '#94a3b8' }}>暂无已认证 release</span>
                        )}
                    </div>
                );
            },
        })),
    ];

    return (
        <div>
            {/* 顶层统计大盘 */}
            <Row gutter={[8, 8]} style={{ marginBottom: 12 }}>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="集群接入计算节点"
                        value={activeNodes.length}
                        prefix={<ClusterOutlined style={{ color: '#1668dc' }} />}
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="已装配插件槽位"
                        value={`${installedSlotsCount} / ${totalSlots}`}
                        prefix={<AppstoreOutlined style={{ color: '#059669' }} />}
                        color="#059669"
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="版本滞后 / 漂移槽位"
                        value={driftCount}
                        prefix={<WarningFilled style={{ color: driftCount > 0 ? '#d97706' : '#94a3b8' }} />}
                        color={driftCount > 0 ? '#d97706' : '#94a3b8'}
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="进行中的热部署"
                        value={activeOpMap.size}
                        prefix={<LoadingOutlined style={{ color: activeOpMap.size > 0 ? '#2563eb' : '#94a3b8' }} />}
                        color={activeOpMap.size > 0 ? '#2563eb' : '#94a3b8'}
                    />
                </Col>
            </Row>

            {/* 操作工具栏 */}
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 10,
                    padding: '8px 12px',
                    background: '#f8fafc',
                    borderRadius: 6,
                    border: '1px solid #e2e8f0',
                }}
            >
                <Space size={8} wrap>
                    <Text strong style={{ fontSize: 13 }}>
                        集群节点 × 插件拓扑矩阵 (Fleet Matrix)
                    </Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                        实时展示各算力节点上的插件代际、动态端点与最新 release 匹配状况。
                    </Text>
                </Space>

                <Space size={8} wrap>
                    <Button
                        size="small"
                        icon={<ReloadOutlined />}
                        onClick={() => {
                            void cache.invalidateQueries({ queryKey: ['nodes'] });
                            void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
                        }}
                    >
                        刷新矩阵
                    </Button>

                    {driftCount > 0 && canManage ? (
                        <Popconfirm
                            title={`确定一键对齐全部 ${driftCount} 个版本滞后槽位？`}
                            description="系统将为各个节点自动创建蓝绿升级操作（不停止旧版本，候选验证就绪后再原子切换）。"
                            onConfirm={handleBatchAlignDrift}
                            okText="立即对齐"
                            cancelText="取消"
                        >
                            <Button
                                size="small"
                                type="primary"
                                icon={<CloudUploadOutlined />}
                                loading={batchAligning}
                                style={{ background: '#d97706', borderColor: '#d97706' }}
                            >
                                一键对齐滞后版本 ({driftCount})
                            </Button>
                        </Popconfirm>
                    ) : null}

                    {canManage ? (
                        <Popconfirm
                            title="确定向同机数据面节点 (local-host) 一键装配全部基础处理插件（VLM、ASR、OCR、Embedding）吗？"
                            onConfirm={() => batchDeploy.mutate()}
                            okText="立即装配"
                            cancelText="取消"
                        >
                            <Button
                                size="small"
                                type="primary"
                                icon={<ThunderboltOutlined />}
                                loading={batchDeploy.isPending}
                            >
                                一键装配本机基础插件
                            </Button>
                        </Popconfirm>
                    ) : null}
                </Space>
            </div>

            <ErrorNotice error={nodes.error || catalog.error || releases.error || operations.error} />

            {/* 矩阵表格 */}
            {nodes.isPending ? (
                <Loading tip="正在生成集群插件矩阵…" />
            ) : (
                <Table
                    rowKey="node_id"
                    columns={columns}
                    dataSource={activeNodes}
                    pagination={false}
                    size="middle"
                    scroll={{ x: 250 + catalogItems.length * 200 }}
                    bordered
                />
            )}
        </div>
    );
}

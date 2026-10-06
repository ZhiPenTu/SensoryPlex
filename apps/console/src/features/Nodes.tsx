import { useState, useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Alert,
    Button,
    Card,
    Col,
    Form,
    Input,
    InputNumber,
    Popconfirm,
    Radio,
    Row,
    Space,
    Tag,
    Tooltip,
    Typography,
    message,
} from 'antd';
import {
    ClusterOutlined,
    HddOutlined,
    AppstoreOutlined,
    KeyOutlined,
    ClearOutlined,
    CopyOutlined,
    CheckOutlined,
    ThunderboltOutlined,
    StopOutlined,
    DisconnectOutlined,
    DeleteOutlined,
    SafetyCertificateOutlined,
    ApiOutlined,
    SearchOutlined,
    SyncOutlined,
} from '@ant-design/icons';
import { api, post } from '../api/client';
import type { EnrollmentToken, NodeInfo, NodeList } from '../api/contracts';
import { Badge, date, Empty, ErrorNotice, Heading, Loading, Modal } from '../components';
import PluginPruneModal from './PluginPruneModal';
import NodeResourceHologram, { formatBytes } from './NodeResourceHologram';

const { Text } = Typography;

function getPluginMeta(pluginId: string) {
    if (pluginId.includes('ocr')) {
        return {
            name: 'RapidOCR 文本识别',
            short: 'RapidOCR',
            modality: 'OCR',
            icon: '🔤',
            color: 'cyan',
        };
    }
    if (pluginId.includes('asr') || pluginId.includes('whisper')) {
        return {
            name: 'Whisper MLX 语音识别',
            short: 'Whisper MLX',
            modality: 'ASR',
            icon: '🎙️',
            color: 'geekblue',
        };
    }
    if (pluginId.includes('vlm') || pluginId.includes('moondream')) {
        return {
            name: 'Moondream VLM 视觉理解',
            short: 'Moondream VLM',
            modality: 'VLM',
            icon: '👁️',
            color: 'purple',
        };
    }
    if (pluginId.includes('embed') || pluginId.includes('bge')) {
        return {
            name: 'BGE 向量嵌入',
            short: 'BGE 向量嵌入',
            modality: '向量',
            icon: '🧬',
            color: 'blue',
        };
    }
    const parts = pluginId.split('.');
    const rawShort = parts[parts.length - 1] || pluginId;
    return {
        name: rawShort,
        short: rawShort,
        modality: '插件',
        icon: '⚙️',
        color: 'default',
    };
}

function getInstanceStateMeta(state: string) {
    const s = (state || '').toLowerCase();
    if (s.includes('ready')) {
        return { label: '就绪', color: '#059669', bg: '#ecfdf5', border: '#a7f3d0' };
    }
    if (
        s.includes('installing') ||
        s.includes('staging') ||
        s.includes('starting') ||
        s.includes('planned')
    ) {
        return { label: '安装中', color: '#2563eb', bg: '#eff6ff', border: '#bfdbfe' };
    }
    if (s.includes('fail') || s.includes('error') || s.includes('degraded')) {
        return { label: '异常', color: '#dc2626', bg: '#fef2f2', border: '#fecaca' };
    }
    if (s.includes('stop') || s.includes('uninstalled')) {
        return { label: '已停止', color: '#64748b', bg: '#f8fafc', border: '#e2e8f0' };
    }
    if (s.includes('drain')) {
        return { label: '排空中', color: '#d97706', bg: '#fffbeb', border: '#fde68a' };
    }
    return {
        label: state.replace('PLUGIN_INSTANCE_STATE_', ''),
        color: '#475569',
        bg: '#f8fafc',
        border: '#e2e8f0',
    };
}

export default function Nodes() {
    const cache = useQueryClient();
    const [tokenModalOpen, setTokenModalOpen] = useState(false);
    const [createdToken, setCreatedToken] = useState<EnrollmentToken | null>(null);
    const [copiedToken, setCopiedToken] = useState(false);
    const [copiedCmd, setCopiedCmd] = useState(false);
    const [copiedNodeId, setCopiedNodeId] = useState<string | null>(null);
    const [pruneModalNodeId, setPruneModalNodeId] = useState<string | null>(null);
    const [searchQuery, setSearchQuery] = useState('');
    const [statusFilter, setStatusFilter] = useState<'ALL' | 'READY' | 'CO_LOCATED' | 'ABNORMAL'>(
        'ALL',
    );
    const [form] = Form.useForm();

    const nodesQuery = useQuery({
        queryKey: ['nodes'],
        queryFn: ({ signal }) => api<NodeList>('/admin/v1/nodes?limit=100', { signal }),
        refetchInterval: 4000,
    });

    const createTokenMutation = useMutation({
        mutationFn: (values: { node_id: string; expires_in_minutes: number }) =>
            post<EnrollmentToken>('/admin/v1/nodes/enrollment-tokens', {
                node_id: values.node_id,
                expires_in_minutes: Number(values.expires_in_minutes || 60),
            }),
        onSuccess: (data) => {
            setCreatedToken(data);
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const acceptMutation = useMutation({
        mutationFn: (nodeId: string) => post(`/admin/v1/nodes/${nodeId}:accept`),
        onSuccess: () => {
            message.success('已接纳该节点进入集群');
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const rejectMutation = useMutation({
        mutationFn: (nodeId: string) => post(`/admin/v1/nodes/${nodeId}:reject`),
        onSuccess: () => {
            message.info('已拒绝该节点入网申请');
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const drainMutation = useMutation({
        mutationFn: (nodeId: string) => post(`/admin/v1/nodes/${nodeId}:drain`),
        onSuccess: () => {
            message.success('已将节点置为排空中状态');
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const revokeMutation = useMutation({
        mutationFn: (nodeId: string) => post(`/admin/v1/nodes/${nodeId}:revoke`),
        onSuccess: () => {
            message.warning('已撤销该节点权限');
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const deleteMutation = useMutation({
        mutationFn: (nodeId: string) => api(`/admin/v1/nodes/${nodeId}`, { method: 'DELETE' }),
        onSuccess: () => {
            message.success('已移除节点记录');
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const purgeMutation = useMutation({
        mutationFn: () => post('/admin/v1/nodes:purge-stale'),
        onSuccess: () => {
            message.success('已清理所有离线与失效测试节点');
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const copyText = (text: string, type: 'token' | 'cmd' | 'node', id?: string) => {
        void navigator.clipboard.writeText(text);
        if (type === 'token') {
            setCopiedToken(true);
            setTimeout(() => setCopiedToken(false), 2000);
        } else if (type === 'cmd') {
            setCopiedCmd(true);
            setTimeout(() => setCopiedCmd(false), 2000);
        } else if (type === 'node' && id) {
            setCopiedNodeId(id);
            setTimeout(() => setCopiedNodeId(null), 2000);
        }
        message.success('已复制到剪贴板');
    };

    const acceleratorText = (node: NodeInfo) =>
        node.capabilities?.accelerators?.length
            ? node.capabilities.accelerators
                  .map((a) => `${a.accelerator} (${a.runtime_version || '可用'})`)
                  .join(', ')
            : 'CPU Host 原生';

    const candidates =
        nodesQuery.data?.items?.filter((n) => n.status === 'NODE_STATUS_CANDIDATE') || [];
    const activeNodes =
        nodesQuery.data?.items?.filter((n) => n.status !== 'NODE_STATUS_CANDIDATE') || [];
    const readyCount = activeNodes.filter((node) => node.status === 'NODE_STATUS_READY').length;
    const coLocatedCount = activeNodes.filter((node) => node.is_co_located).length;
    const abnormalCount = activeNodes.filter((node) => node.status !== 'NODE_STATUS_READY').length;

    // 搜索与状态过滤
    const filteredNodes = useMemo(() => {
        return activeNodes.filter((node) => {
            // 状态筛选
            if (statusFilter === 'READY' && node.status !== 'NODE_STATUS_READY') return false;
            if (statusFilter === 'CO_LOCATED' && !node.is_co_located) return false;
            if (statusFilter === 'ABNORMAL' && node.status === 'NODE_STATUS_READY') return false;

            // 搜索过滤
            if (searchQuery.trim()) {
                const q = searchQuery.toLowerCase().trim();
                const matchId = node.node_id.toLowerCase().includes(q);
                const matchName = (node.display_name || '').toLowerCase().includes(q);
                const matchPlatform = (node.capabilities?.platform || '').toLowerCase().includes(q);
                const matchArch = (node.capabilities?.arch || '').toLowerCase().includes(q);
                const matchPlugin = (node.instances || []).some((inst) =>
                    inst.plugin_id.toLowerCase().includes(q),
                );
                return matchId || matchName || matchPlatform || matchArch || matchPlugin;
            }

            return true;
        });
    }, [activeNodes, statusFilter, searchQuery]);

    return (
        <div>
            <Heading
                eyebrow="LAN Worker Topology"
                title="节点拓扑管理"
                description="统一纳管同机与局域网分布式算力 Worker，隔离管理面与执行面，保障端侧就地多模态处理性能。"
                action={
                    <Space size={8}>
                        <Popconfirm
                            title="确定清理所有已离线或已撤销的旧测试节点记录吗？"
                            onConfirm={() => purgeMutation.mutate()}
                            okText="清理"
                            cancelText="取消"
                        >
                            <Button icon={<ClearOutlined />} loading={purgeMutation.isPending}>
                                清理离线节点
                            </Button>
                        </Popconfirm>

                        <Button
                            type="primary"
                            icon={<KeyOutlined />}
                            onClick={() => {
                                setCreatedToken(null);
                                form.resetFields();
                                setTokenModalOpen(true);
                            }}
                        >
                            生成接入令牌
                        </Button>
                    </Space>
                }
            />

            <ErrorNotice
                error={
                    nodesQuery.error ||
                    acceptMutation.error ||
                    rejectMutation.error ||
                    drainMutation.error ||
                    revokeMutation.error ||
                    deleteMutation.error ||
                    purgeMutation.error
                }
            />

            {/* 集群算力全息负载看板 (ADR-029 P3) */}
            <NodeResourceHologram nodes={activeNodes} />

            {/* 待准入审批候选节点专区 */}
            {candidates.length > 0 ? (
                <Card
                    title={
                        <Space size={8}>
                            <SafetyCertificateOutlined style={{ color: '#d97706', fontSize: 16 }} />
                            <span style={{ fontWeight: 650, fontSize: 15, color: '#92400e' }}>
                                待准入审批候选节点
                            </span>
                            <Tag color="warning">{candidates.length} 台待审批</Tag>
                        </Space>
                    }
                    style={{
                        marginBottom: 16,
                        border: '1px solid #fde68a',
                        borderLeft: '4px solid #d97706',
                        background: '#fffbeb',
                        borderRadius: 8,
                    }}
                    styles={{ body: { padding: '12px 18px' } }}
                >
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {candidates.map((node) => (
                            <div
                                key={node.node_id}
                                style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    background: '#ffffff',
                                    padding: '12px 16px',
                                    borderRadius: 6,
                                    border: '1px solid #fef3c7',
                                    flexWrap: 'wrap',
                                    gap: 12,
                                }}
                            >
                                <Space size={12} align="center">
                                    <div
                                        style={{
                                            width: 36,
                                            height: 36,
                                            borderRadius: 8,
                                            background: '#fef3c7',
                                            display: 'flex',
                                            alignItems: 'center',
                                            justifyContent: 'center',
                                            color: '#d97706',
                                            fontSize: 16,
                                        }}
                                    >
                                        <ClusterOutlined />
                                    </div>
                                    <div>
                                        <Space size={8}>
                                            <Text strong style={{ fontSize: 14 }}>
                                                {node.node_id}
                                            </Text>
                                            <Tag color="warning" style={{ margin: 0 }}>
                                                待准入审批
                                            </Tag>
                                            {node.is_co_located ? (
                                                <Tag color="cyan" style={{ margin: 0 }}>
                                                    同机节点
                                                </Tag>
                                            ) : (
                                                <Tag color="blue" style={{ margin: 0 }}>
                                                    局域网节点
                                                </Tag>
                                            )}
                                        </Space>
                                        <div
                                            style={{ fontSize: 12, color: '#64748b', marginTop: 3 }}
                                        >
                                            架构: {node.capabilities?.platform || 'linux'} (
                                            {node.capabilities?.arch || 'x86_64'}) · 核心:{' '}
                                            {node.capabilities?.cpu_cores || 0} 核 · 内存:{' '}
                                            {formatBytes(node.capabilities?.memory_bytes)} · 算力:{' '}
                                            {acceleratorText(node)}
                                        </div>
                                    </div>
                                </Space>

                                <Space size={8}>
                                    <Button
                                        type="primary"
                                        size="small"
                                        style={{ background: '#059669', borderColor: '#059669' }}
                                        loading={acceptMutation.isPending}
                                        onClick={() => acceptMutation.mutate(node.node_id)}
                                    >
                                        批准接入
                                    </Button>
                                    <Popconfirm
                                        title="确定拒绝该节点接入申请？"
                                        onConfirm={() => rejectMutation.mutate(node.node_id)}
                                        okText="拒绝"
                                        cancelText="取消"
                                    >
                                        <Button
                                            size="small"
                                            danger
                                            loading={rejectMutation.isPending}
                                        >
                                            拒绝
                                        </Button>
                                    </Popconfirm>
                                </Space>
                            </div>
                        ))}
                    </div>
                </Card>
            ) : null}

            {/* 正式活跃节点拓扑卡片 */}
            <Card
                style={{
                    borderRadius: 10,
                    border: '1px solid var(--sp-border, #e2e8f0)',
                    background: '#ffffff',
                    boxShadow: '0 1px 3px rgba(15, 23, 42, 0.04)',
                }}
                styles={{ body: { padding: '16px 20px' } }}
            >
                {/* 视图顶栏与过滤控件 */}
                <div
                    style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        flexWrap: 'wrap',
                        gap: 12,
                        marginBottom: 16,
                        paddingBottom: 14,
                        borderBottom: '1px solid #f1f5f9',
                    }}
                >
                    <Space size={10} align="center">
                        <ApiOutlined style={{ color: '#2563eb', fontSize: 17 }} />
                        <span style={{ fontWeight: 700, fontSize: 15, color: '#0f172a' }}>
                            集群算力拓扑矩阵
                        </span>
                        <Tag
                            color="default"
                            style={{
                                background: '#f1f5f9',
                                border: '1px solid #e2e8f0',
                                color: '#475569',
                                borderRadius: 10,
                                fontSize: 11,
                                padding: '0 8px',
                            }}
                        >
                            共 {activeNodes.length} 台
                        </Tag>
                    </Space>

                    <div
                        style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}
                    >
                        <Radio.Group
                            size="small"
                            value={statusFilter}
                            onChange={(e) => setStatusFilter(e.target.value)}
                            buttonStyle="solid"
                        >
                            <Radio.Button value="ALL">全部 ({activeNodes.length})</Radio.Button>
                            <Radio.Button value="READY">可调度 ({readyCount})</Radio.Button>
                            <Radio.Button value="CO_LOCATED">同机 ({coLocatedCount})</Radio.Button>
                            {abnormalCount > 0 ? (
                                <Radio.Button value="ABNORMAL">
                                    异常/排空 ({abnormalCount})
                                </Radio.Button>
                            ) : null}
                        </Radio.Group>

                        <Input
                            size="small"
                            placeholder="过滤节点标识、平台或模型…"
                            prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
                            allowClear
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                            style={{ width: 200 }}
                        />

                        <Button
                            size="small"
                            icon={<SyncOutlined spin={nodesQuery.isFetching} />}
                            onClick={() => void nodesQuery.refetch()}
                        >
                            刷新
                        </Button>
                    </div>
                </div>

                {nodesQuery.isPending ? (
                    <Loading tip="正在扫描网络节点拓扑…" />
                ) : filteredNodes.length ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                        {filteredNodes.map((node) => {
                            const isReady = node.status === 'NODE_STATUS_READY';
                            const isDraining = node.status === 'NODE_STATUS_DRAINING';
                            const isRevoked = node.status === 'NODE_STATUS_REVOKED';
                            const isOffline = node.status === 'NODE_STATUS_OFFLINE';
                            const accelerators = node.capabilities?.accelerators || [];

                            const borderAccentColor = isReady
                                ? '#10b981'
                                : isDraining
                                  ? '#f59e0b'
                                  : isRevoked || isOffline
                                    ? '#ef4444'
                                    : '#94a3b8';

                            return (
                                <div
                                    key={node.node_id}
                                    className="node-item-card"
                                    style={{
                                        borderLeft: `4px solid ${borderAccentColor}`,
                                        padding: '16px 18px',
                                    }}
                                >
                                    {/* 节点头：身份、状态与操作按钮 */}
                                    <div
                                        style={{
                                            display: 'flex',
                                            justifyContent: 'space-between',
                                            alignItems: 'flex-start',
                                            flexWrap: 'wrap',
                                            gap: 12,
                                            marginBottom: 14,
                                        }}
                                    >
                                        <div>
                                            <div
                                                style={{
                                                    display: 'flex',
                                                    alignItems: 'center',
                                                    gap: 8,
                                                    flexWrap: 'wrap',
                                                }}
                                            >
                                                <div
                                                    style={{
                                                        width: 28,
                                                        height: 28,
                                                        borderRadius: 6,
                                                        background: isReady ? '#ecfdf5' : '#f8fafc',
                                                        border: `1px solid ${isReady ? '#a7f3d0' : '#e2e8f0'}`,
                                                        display: 'flex',
                                                        alignItems: 'center',
                                                        justifyContent: 'center',
                                                        color: isReady ? '#059669' : '#64748b',
                                                        fontSize: 14,
                                                    }}
                                                >
                                                    <ClusterOutlined />
                                                </div>

                                                <Text
                                                    strong
                                                    style={{ fontSize: 15, color: '#0f172a' }}
                                                >
                                                    {node.node_id}
                                                </Text>

                                                <Tooltip title="点击复制节点标识">
                                                    <Button
                                                        type="text"
                                                        size="small"
                                                        icon={
                                                            copiedNodeId === node.node_id ? (
                                                                <CheckOutlined
                                                                    style={{
                                                                        color: '#059669',
                                                                        fontSize: 12,
                                                                    }}
                                                                />
                                                            ) : (
                                                                <CopyOutlined
                                                                    style={{
                                                                        color: '#94a3b8',
                                                                        fontSize: 12,
                                                                    }}
                                                                />
                                                            )
                                                        }
                                                        onClick={() =>
                                                            copyText(
                                                                node.node_id,
                                                                'node',
                                                                node.node_id,
                                                            )
                                                        }
                                                        style={{ padding: '0 4px', height: 22 }}
                                                    />
                                                </Tooltip>

                                                <Badge state={node.status} />

                                                {node.is_co_located ? (
                                                    <Tag
                                                        color="cyan"
                                                        style={{
                                                            margin: 0,
                                                            border: '1px solid #a5f3fc',
                                                            background: '#ecfeff',
                                                            color: '#0891b2',
                                                            fontWeight: 500,
                                                            borderRadius: 4,
                                                        }}
                                                    >
                                                        同机执行面 · 零拷贝共享内存
                                                    </Tag>
                                                ) : (
                                                    <Tag
                                                        color="blue"
                                                        style={{
                                                            margin: 0,
                                                            border: '1px solid #bfdbfe',
                                                            background: '#eff6ff',
                                                            color: '#2563eb',
                                                            fontWeight: 500,
                                                            borderRadius: 4,
                                                        }}
                                                    >
                                                        局域网节点 · gRPC 远程调用
                                                    </Tag>
                                                )}

                                                <Tag
                                                    style={{
                                                        margin: 0,
                                                        background: '#f8fafc',
                                                        border: '1px solid #e2e8f0',
                                                        color: '#475569',
                                                        borderRadius: 4,
                                                    }}
                                                >
                                                    {node.capabilities?.platform || 'macos'} ·{' '}
                                                    {node.capabilities?.arch || 'arm64'}
                                                </Tag>
                                            </div>

                                            <div
                                                style={{
                                                    fontSize: 12,
                                                    color: '#64748b',
                                                    marginTop: 5,
                                                    display: 'flex',
                                                    gap: 12,
                                                    alignItems: 'center',
                                                    flexWrap: 'wrap',
                                                }}
                                            >
                                                <span>
                                                    节点别名:{' '}
                                                    <Text strong style={{ color: '#334155' }}>
                                                        {node.display_name || node.node_id}
                                                    </Text>
                                                </span>
                                                <span style={{ color: '#cbd5e1' }}>·</span>
                                                <span>
                                                    最后心跳:{' '}
                                                    <Text style={{ color: '#334155' }}>
                                                        {date(node.last_heartbeat_at)}
                                                    </Text>
                                                </span>
                                                <span style={{ color: '#cbd5e1' }}>·</span>
                                                <span style={{ color: '#059669' }}>
                                                    ● 租约受控正常
                                                </span>
                                                {node.enrolled_at ? (
                                                    <>
                                                        <span style={{ color: '#cbd5e1' }}>·</span>
                                                        <span>
                                                            首次入网: {date(node.enrolled_at)}
                                                        </span>
                                                    </>
                                                ) : null}
                                            </div>
                                        </div>

                                        {/* 操作工具栏 */}
                                        <Space size={8}>
                                            {(node.is_co_located ||
                                                node.node_id === 'local-host') &&
                                            node.status !== 'NODE_STATUS_REVOKED' ? (
                                                <Button
                                                    size="small"
                                                    icon={<ClearOutlined />}
                                                    onClick={() =>
                                                        setPruneModalNodeId(node.node_id)
                                                    }
                                                >
                                                    清理旧安装
                                                </Button>
                                            ) : null}

                                            {node.status === 'NODE_STATUS_READY' ? (
                                                <Popconfirm
                                                    title="确定排空该节点？"
                                                    description="排空后将不再下发新任务，正在运行的任务将平滑执行完毕。"
                                                    onConfirm={() =>
                                                        drainMutation.mutate(node.node_id)
                                                    }
                                                    okText="排空"
                                                    cancelText="取消"
                                                >
                                                    <Button
                                                        size="small"
                                                        icon={<StopOutlined />}
                                                        loading={drainMutation.isPending}
                                                    >
                                                        排空
                                                    </Button>
                                                </Popconfirm>
                                            ) : null}

                                            {node.status !== 'NODE_STATUS_REVOKED' &&
                                            node.status !== 'NODE_STATUS_OFFLINE' ? (
                                                <Popconfirm
                                                    title="确定撤销该节点权限？"
                                                    description="撤销后节点将失去入网凭据，需重新签发令牌才能再次接入。"
                                                    onConfirm={() =>
                                                        revokeMutation.mutate(node.node_id)
                                                    }
                                                    okText="撤销凭据"
                                                    cancelText="取消"
                                                    okButtonProps={{ danger: true }}
                                                >
                                                    <Button
                                                        size="small"
                                                        danger
                                                        icon={<DisconnectOutlined />}
                                                        style={{
                                                            background: '#ffffff',
                                                            borderColor: '#fca5a5',
                                                            color: '#dc2626',
                                                        }}
                                                        loading={revokeMutation.isPending}
                                                    >
                                                        撤销
                                                    </Button>
                                                </Popconfirm>
                                            ) : null}

                                            {node.status === 'NODE_STATUS_REVOKED' ||
                                            node.status === 'NODE_STATUS_OFFLINE' ? (
                                                <Popconfirm
                                                    title="确定彻底删除该节点记录？"
                                                    onConfirm={() =>
                                                        deleteMutation.mutate(node.node_id)
                                                    }
                                                    okText="删除"
                                                    cancelText="取消"
                                                    okButtonProps={{ danger: true }}
                                                >
                                                    <Button
                                                        size="small"
                                                        danger
                                                        type="text"
                                                        icon={<DeleteOutlined />}
                                                        loading={deleteMutation.isPending}
                                                    >
                                                        删除记录
                                                    </Button>
                                                </Popconfirm>
                                            ) : null}
                                        </Space>
                                    </div>

                                    {/* 核心规格 4 联卡片带 */}
                                    <Row gutter={[10, 10]} style={{ marginBottom: 14 }}>
                                        <Col xs={12} sm={6}>
                                            <div className="node-spec-cell">
                                                <div className="node-spec-label">
                                                    <ClusterOutlined
                                                        style={{ marginRight: 4, color: '#2563eb' }}
                                                    />
                                                    CPU 算力
                                                </div>
                                                <div className="node-spec-val">
                                                    {node.capabilities?.cpu_cores || 0} 核 (Cores)
                                                </div>
                                                <div style={{ fontSize: 10, color: '#94a3b8' }}>
                                                    并发槽位:{' '}
                                                    {Math.max(
                                                        2,
                                                        (node.capabilities?.cpu_cores || 0) * 2,
                                                    )}{' '}
                                                    槽
                                                </div>
                                            </div>
                                        </Col>

                                        <Col xs={12} sm={6}>
                                            <div className="node-spec-cell">
                                                <div className="node-spec-label">
                                                    <HddOutlined
                                                        style={{ marginRight: 4, color: '#059669' }}
                                                    />
                                                    物理 / 统一内存
                                                </div>
                                                <div className="node-spec-val">
                                                    {formatBytes(node.capabilities?.memory_bytes)}
                                                </div>
                                                <div style={{ fontSize: 10, color: '#94a3b8' }}>
                                                    {node.capabilities?.unified_memory_bytes
                                                        ? 'Apple Silicon 统一内存'
                                                        : '独立系统内存池'}
                                                </div>
                                            </div>
                                        </Col>

                                        <Col xs={12} sm={6}>
                                            <div className="node-spec-cell">
                                                <div className="node-spec-label">
                                                    <ThunderboltOutlined
                                                        style={{ marginRight: 4, color: '#d97706' }}
                                                    />
                                                    硬件加速引擎
                                                </div>
                                                <div className="node-spec-val">
                                                    {accelerators.length ? (
                                                        <Space size={4} wrap>
                                                            {accelerators.map((a) => {
                                                                const isMetal = a.accelerator
                                                                    .toLowerCase()
                                                                    .includes('metal');
                                                                const label = `${a.accelerator}${a.runtime_version ? ` · ${a.runtime_version}` : ''}`;
                                                                return (
                                                                    <Tag
                                                                        key={a.accelerator}
                                                                        color={
                                                                            isMetal
                                                                                ? 'gold'
                                                                                : 'geekblue'
                                                                        }
                                                                        style={{
                                                                            margin: 0,
                                                                            fontSize: 11,
                                                                            borderRadius: 4,
                                                                        }}
                                                                    >
                                                                        ⚡ {label}
                                                                    </Tag>
                                                                );
                                                            })}
                                                        </Space>
                                                    ) : (
                                                        <span
                                                            style={{
                                                                fontSize: 12,
                                                                color: '#64748b',
                                                                fontWeight: 400,
                                                            }}
                                                        >
                                                            CPU 原生执行
                                                        </span>
                                                    )}
                                                </div>
                                                <div style={{ fontSize: 10, color: '#94a3b8' }}>
                                                    {accelerators.some((a) =>
                                                        a.accelerator
                                                            .toLowerCase()
                                                            .includes('metal'),
                                                    )
                                                        ? 'Apple Silicon 原生加速引擎'
                                                        : accelerators.length
                                                          ? 'GPU / NPU 硬件加速已激活'
                                                          : '无独立硬件加速器'}
                                                </div>
                                            </div>
                                        </Col>

                                        <Col xs={12} sm={6}>
                                            <div className="node-spec-cell">
                                                <div className="node-spec-label">
                                                    <ApiOutlined
                                                        style={{ marginRight: 4, color: '#0891b2' }}
                                                    />
                                                    数据交互模式
                                                </div>
                                                <div
                                                    className="node-spec-val"
                                                    style={{
                                                        color: node.is_co_located
                                                            ? '#0891b2'
                                                            : '#2563eb',
                                                    }}
                                                >
                                                    {node.is_co_located
                                                        ? '共享内存零拷贝'
                                                        : 'gRPC / mTLS 双向流'}
                                                </div>
                                                <div style={{ fontSize: 10, color: '#94a3b8' }}>
                                                    {node.is_co_located
                                                        ? 'LeaseBuffer 内存租约'
                                                        : 'TCP 网络数据面'}
                                                </div>
                                            </div>
                                        </Col>
                                    </Row>

                                    {/* 已部署模型插件实例展示 */}
                                    <div
                                        style={{
                                            paddingTop: 12,
                                            borderTop: '1px solid #f1f5f9',
                                        }}
                                    >
                                        <div
                                            style={{
                                                display: 'flex',
                                                alignItems: 'center',
                                                justifyContent: 'space-between',
                                                marginBottom: 8,
                                            }}
                                        >
                                            <Space size={6}>
                                                <AppstoreOutlined
                                                    style={{ color: '#2563eb', fontSize: 13 }}
                                                />
                                                <Text
                                                    strong
                                                    style={{ fontSize: 13, color: '#1e293b' }}
                                                >
                                                    已部署模型实例 ({node.instances?.length || 0})
                                                </Text>
                                            </Space>
                                            <Text type="secondary" style={{ fontSize: 11 }}>
                                                受控进程独立托管 · 具备自动健康探活
                                            </Text>
                                        </div>

                                        {node.instances?.length ? (
                                            <div
                                                style={{
                                                    display: 'flex',
                                                    flexWrap: 'wrap',
                                                    gap: 8,
                                                }}
                                            >
                                                {node.instances.map((inst) => {
                                                    const meta = getPluginMeta(inst.plugin_id);
                                                    const stateMeta = getInstanceStateMeta(
                                                        inst.actual_state,
                                                    );

                                                    return (
                                                        <Tooltip
                                                            key={inst.instance_id}
                                                            title={
                                                                <div
                                                                    style={{
                                                                        fontSize: 11,
                                                                        lineHeight: 1.5,
                                                                    }}
                                                                >
                                                                    <div>
                                                                        <strong>完整标识:</strong>{' '}
                                                                        {inst.plugin_id}
                                                                    </div>
                                                                    <div>
                                                                        <strong>实例 ID:</strong>{' '}
                                                                        {inst.instance_id}
                                                                    </div>
                                                                    <div>
                                                                        <strong>当前状态:</strong>{' '}
                                                                        {inst.actual_state}
                                                                    </div>
                                                                    {inst.endpoint ? (
                                                                        <div>
                                                                            <strong>
                                                                                服务端口:
                                                                            </strong>{' '}
                                                                            {inst.endpoint}
                                                                        </div>
                                                                    ) : null}
                                                                    {inst.artifact_digest ? (
                                                                        <div>
                                                                            <strong>
                                                                                制品签名:
                                                                            </strong>{' '}
                                                                            {inst.artifact_digest.slice(
                                                                                0,
                                                                                16,
                                                                            )}
                                                                            …
                                                                        </div>
                                                                    ) : null}
                                                                </div>
                                                            }
                                                        >
                                                            <div className="plugin-chip">
                                                                <Tag
                                                                    color={meta.color}
                                                                    style={{
                                                                        margin: 0,
                                                                        fontSize: 10,
                                                                        padding: '0 5px',
                                                                        borderRadius: 3,
                                                                    }}
                                                                >
                                                                    {meta.icon} {meta.modality}
                                                                </Tag>
                                                                <span
                                                                    style={{
                                                                        fontWeight: 600,
                                                                        color: '#1e293b',
                                                                    }}
                                                                >
                                                                    {meta.short}
                                                                </span>
                                                                {inst.plugin_version ? (
                                                                    <span
                                                                        style={{
                                                                            color: '#64748b',
                                                                            fontSize: 11,
                                                                        }}
                                                                    >
                                                                        v{inst.plugin_version}
                                                                    </span>
                                                                ) : null}
                                                                <span
                                                                    style={{
                                                                        display: 'inline-flex',
                                                                        alignItems: 'center',
                                                                        gap: 4,
                                                                        padding: '1px 6px',
                                                                        borderRadius: 10,
                                                                        background: stateMeta.bg,
                                                                        border: `1px solid ${stateMeta.border}`,
                                                                        color: stateMeta.color,
                                                                        fontSize: 10.5,
                                                                        fontWeight: 500,
                                                                    }}
                                                                >
                                                                    <span
                                                                        style={{
                                                                            width: 5,
                                                                            height: 5,
                                                                            borderRadius: '50%',
                                                                            background:
                                                                                stateMeta.color,
                                                                        }}
                                                                    />
                                                                    {stateMeta.label}
                                                                </span>
                                                            </div>
                                                        </Tooltip>
                                                    );
                                                })}
                                            </div>
                                        ) : (
                                            <div
                                                style={{
                                                    padding: '10px 14px',
                                                    background: '#f8fafc',
                                                    borderRadius: 6,
                                                    border: '1px solid #f1f5f9',
                                                    color: '#94a3b8',
                                                    fontSize: 12,
                                                }}
                                            >
                                                当前节点暂未部署模型插件实例。前往【插件中心】可向该节点分发并安装算法模型。
                                            </div>
                                        )}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                ) : (
                    <Empty title="未找到匹配的算力节点">
                        <div style={{ marginTop: 8, color: '#64748b', fontSize: 13 }}>
                            {searchQuery
                                ? '请尝试更换搜索关键字或重置状态过滤条件。'
                                : '当前集群中暂无注册节点，请先生成接入令牌并拉起 Worker 进程。'}
                        </div>
                    </Empty>
                )}
            </Card>

            {/* 生成接入令牌弹窗 */}
            {tokenModalOpen ? (
                <Modal
                    title="生成 Worker 接入令牌 (Enrollment Token)"
                    onClose={() => setTokenModalOpen(false)}
                    width={560}
                >
                    {createdToken ? (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                            <Alert
                                type="success"
                                showIcon
                                message="接入令牌生成成功！"
                                description="该令牌为一次性接入凭证，Worker 节点首次接入集群后将自动换取长效安全双向凭据。"
                            />

                            <div>
                                <Text
                                    strong
                                    style={{ fontSize: 13, display: 'block', marginBottom: 4 }}
                                >
                                    接入令牌 (Token):
                                </Text>
                                <div
                                    style={{
                                        display: 'flex',
                                        alignItems: 'center',
                                        justifyContent: 'space-between',
                                        background: '#f1f5f9',
                                        border: '1px solid #cbd5e1',
                                        borderRadius: 6,
                                        padding: '8px 12px',
                                        gap: 8,
                                    }}
                                >
                                    <Text
                                        code
                                        style={{
                                            wordBreak: 'break-all',
                                            fontSize: 12,
                                            background: 'transparent',
                                            border: 'none',
                                            padding: 0,
                                        }}
                                    >
                                        {createdToken.token}
                                    </Text>
                                    <Button
                                        size="small"
                                        type="primary"
                                        icon={copiedToken ? <CheckOutlined /> : <CopyOutlined />}
                                        onClick={() => copyText(createdToken.token, 'token')}
                                    >
                                        {copiedToken ? '已复制' : '复制令牌'}
                                    </Button>
                                </div>
                            </div>

                            <div>
                                <div
                                    style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        alignItems: 'center',
                                        marginBottom: 4,
                                    }}
                                >
                                    <Text strong style={{ fontSize: 13 }}>
                                        快捷启动命令示例:
                                    </Text>
                                    <Button
                                        type="link"
                                        size="small"
                                        icon={
                                            copiedCmd ? (
                                                <CheckOutlined style={{ color: '#059669' }} />
                                            ) : (
                                                <CopyOutlined />
                                            )
                                        }
                                        onClick={() =>
                                            copyText(
                                                `SENSORYPLEX_ENROLL_TOKEN="${createdToken.token}" cargo run -p sensoryplex-runtime -- worker --node-id "${createdToken.node_id}"`,
                                                'cmd',
                                            )
                                        }
                                        style={{ padding: 0 }}
                                    >
                                        {copiedCmd ? '已复制命令' : '复制命令'}
                                    </Button>
                                </div>
                                <pre
                                    style={{
                                        margin: 0,
                                        padding: 12,
                                        borderRadius: 6,
                                        background: '#0f172a',
                                        color: '#38bdf8',
                                        fontSize: 11,
                                        overflow: 'auto',
                                        lineHeight: 1.5,
                                    }}
                                >
                                    {`SENSORYPLEX_ENROLL_TOKEN="${createdToken.token}" cargo run -p sensoryplex-runtime -- worker --node-id "${createdToken.node_id}"`}
                                </pre>
                            </div>

                            <div style={{ fontSize: 12, color: '#64748b' }}>
                                令牌有效期至: <Text strong>{date(createdToken.expires_at)}</Text>
                            </div>

                            <div
                                style={{
                                    display: 'flex',
                                    justifyContent: 'flex-end',
                                    marginTop: 10,
                                }}
                            >
                                <Button type="primary" onClick={() => setTokenModalOpen(false)}>
                                    完成
                                </Button>
                            </div>
                        </div>
                    ) : (
                        <Form
                            form={form}
                            layout="vertical"
                            onFinish={(values) => createTokenMutation.mutate(values)}
                        >
                            <Form.Item
                                name="node_id"
                                label={
                                    <span style={{ fontWeight: 500 }}>
                                        目标节点唯一标识 (node_id)
                                    </span>
                                }
                                rules={[
                                    { required: true, message: '请输入节点标识' },
                                    {
                                        pattern: /^[a-zA-Z0-9_.-]{3,64}$/,
                                        message: '格式必须为 3~64 位的字母、数字、点或下划线',
                                    },
                                ]}
                            >
                                <Input
                                    prefix={<ClusterOutlined style={{ color: '#94a3b8' }} />}
                                    placeholder="例如：node-gpu-4090 或 worker-mac-studio"
                                />
                            </Form.Item>

                            <Form.Item
                                name="expires_in_minutes"
                                label={<span style={{ fontWeight: 500 }}>有效时长（分钟）</span>}
                                initialValue={60}
                            >
                                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                                    <InputNumber min={5} max={1440} style={{ width: 140 }} />
                                    <Space size={6}>
                                        <Button
                                            size="small"
                                            onClick={() =>
                                                form.setFieldValue('expires_in_minutes', 60)
                                            }
                                        >
                                            1 小时
                                        </Button>
                                        <Button
                                            size="small"
                                            onClick={() =>
                                                form.setFieldValue('expires_in_minutes', 360)
                                            }
                                        >
                                            6 小时
                                        </Button>
                                        <Button
                                            size="small"
                                            onClick={() =>
                                                form.setFieldValue('expires_in_minutes', 1440)
                                            }
                                        >
                                            24 小时
                                        </Button>
                                    </Space>
                                </div>
                            </Form.Item>

                            <div
                                style={{
                                    fontSize: 12,
                                    color: '#64748b',
                                    marginBottom: 16,
                                    padding: '8px 12px',
                                    background: '#f8fafc',
                                    borderRadius: 6,
                                    border: '1px solid #f1f5f9',
                                }}
                            >
                                💡 令牌为一次性准入密钥，Worker
                                进程启动入网后，将通过安全的双向认证通道换取长效会话凭据。
                            </div>

                            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                                <Button onClick={() => setTokenModalOpen(false)}>取消</Button>
                                <Button
                                    type="primary"
                                    htmlType="submit"
                                    loading={createTokenMutation.isPending}
                                >
                                    生成接入令牌
                                </Button>
                            </div>
                        </Form>
                    )}
                </Modal>
            ) : null}

            {pruneModalNodeId ? (
                <PluginPruneModal
                    nodeId={pruneModalNodeId}
                    onClose={() => setPruneModalNodeId(null)}
                />
            ) : null}
        </div>
    );
}

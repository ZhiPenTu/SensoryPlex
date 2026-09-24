import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Alert,
    Button,
    Card,
    Col,
    Descriptions,
    Form,
    Input,
    InputNumber,
    Popconfirm,
    Row,
    Space,
    Tag,
    Typography,
    message,
} from 'antd';
import {
    ClusterOutlined,
    CheckCircleOutlined,
    HddOutlined,
    AppstoreOutlined,
    KeyOutlined,
    ClearOutlined,
    CopyOutlined,
    CheckOutlined,
    ThunderboltOutlined,
    StopOutlined,
    DeleteOutlined,
    SafetyCertificateOutlined,
    ApiOutlined,
} from '@ant-design/icons';
import { api, post } from '../api/client';
import type { EnrollmentToken, NodeInfo, NodeList } from '../api/contracts';
import {
    Badge,
    date,
    Empty,
    ErrorNotice,
    Heading,
    Loading,
    Modal,
    Notice,
    StatSummary,
} from '../components';

const { Text } = Typography;

export default function Nodes() {
    const cache = useQueryClient();
    const [tokenModalOpen, setTokenModalOpen] = useState(false);
    const [createdToken, setCreatedToken] = useState<EnrollmentToken | null>(null);
    const [copied, setCopied] = useState(false);
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

    const copyToken = (text: string) => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        message.success('已复制到剪贴板');
        setTimeout(() => setCopied(false), 2000);
    };

    const formatBytes = (bytesStr?: string | number) => {
        const n = Number(bytesStr || 0);
        if (!n) return '0 GB';
        return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
    };

    const acceleratorText = (node: NodeInfo) =>
        node.capabilities?.accelerators?.length
            ? node.capabilities.accelerators
                  .map((a) => `${a.accelerator} (${a.runtime_version || '可用'})`)
                  .join(', ')
            : 'CPU';

    const candidates =
        nodesQuery.data?.items?.filter((n) => n.status === 'NODE_STATUS_CANDIDATE') || [];
    const activeNodes =
        nodesQuery.data?.items?.filter((n) => n.status !== 'NODE_STATUS_CANDIDATE') || [];
    const readyCount = activeNodes.filter((node) => node.status === 'NODE_STATUS_READY').length;
    const coLocatedCount = activeNodes.filter((node) => node.is_co_located).length;
    const instanceCount = activeNodes.reduce(
        (total, node) => total + (node.instances?.length || 0),
        0,
    );

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

            <Row gutter={[16, 16]} style={{ marginBottom: 20 }}>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="活跃计算节点"
                        value={activeNodes.length}
                        prefix={<ClusterOutlined style={{ color: '#1668dc' }} />}
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="可调度节点 (Ready)"
                        value={readyCount}
                        prefix={<CheckCircleOutlined style={{ color: '#10b981' }} />}
                        color="#10b981"
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="同机数据面节点"
                        value={coLocatedCount}
                        prefix={<HddOutlined style={{ color: '#08979c' }} />}
                        color="#08979c"
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="部署模型实例"
                        value={instanceCount}
                        prefix={<AppstoreOutlined style={{ color: '#8b5cf6' }} />}
                        color="#8b5cf6"
                    />
                </Col>
            </Row>

            <Notice>
                管理面 (Control Plane) 与执行面 (Worker)
                物理隔离：通过双向心跳检测与动态准入控制，任务仅下发至可调度的同机或受控局域网节点。
            </Notice>

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

            {/* 候选待接纳节点专区 */}
            {candidates.length > 0 ? (
                <Card
                    title={
                        <Space size={8}>
                            <SafetyCertificateOutlined style={{ color: '#f59e0b' }} />
                            <span style={{ fontWeight: 650, fontSize: 15 }}>
                                待准入审批候选节点
                            </span>
                            <Tag color="warning">{candidates.length} 台待审批</Tag>
                        </Space>
                    }
                    style={{
                        marginBottom: 20,
                        borderColor: '#fde68a',
                        background: '#fffbeb',
                    }}
                >
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        {candidates.map((node) => (
                            <div
                                key={node.node_id}
                                style={{
                                    background: '#ffffff',
                                    padding: '14px 18px',
                                    borderRadius: 8,
                                    border: '1px solid #fef3c7',
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    flexWrap: 'wrap',
                                    gap: 12,
                                }}
                            >
                                <Space size={12}>
                                    <ClusterOutlined style={{ fontSize: 20, color: '#f59e0b' }} />
                                    <div>
                                        <Space size={8}>
                                            <Text strong style={{ fontSize: 13 }}>
                                                {node.node_id}
                                            </Text>
                                            <Tag color="warning">待准入审核</Tag>
                                            {node.is_co_located ? (
                                                <Tag color="blue">同机节点</Tag>
                                            ) : null}
                                        </Space>
                                        <div
                                            style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}
                                        >
                                            算力: {acceleratorText(node)} · 内存:{' '}
                                            {formatBytes(node.capabilities?.memory_bytes)}
                                        </div>
                                    </div>
                                </Space>

                                <Space size={8}>
                                    <Button
                                        type="primary"
                                        size="small"
                                        style={{ background: '#10b981', borderColor: '#10b981' }}
                                        loading={acceptMutation.isPending}
                                        onClick={() => acceptMutation.mutate(node.node_id)}
                                    >
                                        一键接纳
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
                title={
                    <div
                        style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                        }}
                    >
                        <Space size={8}>
                            <ApiOutlined style={{ color: '#1668dc' }} />
                            <span style={{ fontWeight: 650, fontSize: 15 }}>集群算力拓扑视图</span>
                        </Space>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                            心跳每 4 秒实时同步
                        </Text>
                    </div>
                }
            >
                {nodesQuery.isPending ? (
                    <Loading tip="正在扫描网络节点拓扑…" />
                ) : activeNodes.length ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                        {activeNodes.map((node) => (
                            <Card
                                key={node.node_id}
                                size="small"
                                style={{
                                    borderRadius: 8,
                                    border: '1px solid #e2e8f0',
                                    background:
                                        node.status === 'NODE_STATUS_READY' ? '#ffffff' : '#f8fafc',
                                }}
                            >
                                <div
                                    style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        alignItems: 'flex-start',
                                        flexWrap: 'wrap',
                                        gap: 12,
                                        marginBottom: 12,
                                    }}
                                >
                                    <div>
                                        <Space size={8} align="center">
                                            <ClusterOutlined
                                                style={{ fontSize: 16, color: '#1668dc' }}
                                            />
                                            <Text strong style={{ fontSize: 14 }}>
                                                {node.node_id}
                                            </Text>
                                            <Badge state={node.status} />
                                            {node.is_co_located ? (
                                                <Tag color="geekblue" style={{ margin: 0 }}>
                                                    同机执行面
                                                </Tag>
                                            ) : (
                                                <Tag color="default" style={{ margin: 0 }}>
                                                    局域网节点
                                                </Tag>
                                            )}
                                        </Space>
                                        <div
                                            style={{ fontSize: 12, color: '#64748b', marginTop: 4 }}
                                        >
                                            节点名称: {node.display_name || node.node_id} ·
                                            最后心跳: {date(node.last_heartbeat_at)}
                                        </div>
                                    </div>

                                    <Space size={8}>
                                        {node.status === 'NODE_STATUS_READY' ? (
                                            <Popconfirm
                                                title="确定排空该节点？排空后将不再接收新任务。"
                                                onConfirm={() => drainMutation.mutate(node.node_id)}
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
                                                title="确定撤销该节点权限凭据？"
                                                onConfirm={() =>
                                                    revokeMutation.mutate(node.node_id)
                                                }
                                                okText="撤销"
                                                cancelText="取消"
                                            >
                                                <Button
                                                    size="small"
                                                    danger
                                                    loading={revokeMutation.isPending}
                                                >
                                                    撤销
                                                </Button>
                                            </Popconfirm>
                                        ) : null}

                                        {node.status === 'NODE_STATUS_REVOKED' ||
                                        node.status === 'NODE_STATUS_OFFLINE' ? (
                                            <Popconfirm
                                                title="确定从拓扑中彻底删除该节点记录？"
                                                onConfirm={() =>
                                                    deleteMutation.mutate(node.node_id)
                                                }
                                                okText="删除"
                                                cancelText="取消"
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

                                <Descriptions
                                    bordered
                                    size="small"
                                    column={{ xs: 1, sm: 3 }}
                                    style={{ marginBottom: 12 }}
                                    labelStyle={{ background: '#f8fafc', width: 90 }}
                                >
                                    <Descriptions.Item label="CPU 核心">
                                        <Text strong>{node.capabilities?.cpu_cores || 0} 核</Text>
                                    </Descriptions.Item>
                                    <Descriptions.Item label="系统内存">
                                        <Text strong>
                                            {formatBytes(node.capabilities?.memory_bytes)}
                                        </Text>
                                    </Descriptions.Item>
                                    <Descriptions.Item label="算力加速">
                                        <Space size={4}>
                                            <ThunderboltOutlined style={{ color: '#f59e0b' }} />
                                            <span>{acceleratorText(node)}</span>
                                        </Space>
                                    </Descriptions.Item>
                                </Descriptions>

                                <div>
                                    <Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>
                                        部署实例:
                                    </Text>
                                    {node.instances?.length ? (
                                        <Space size={[6, 6]} wrap>
                                            {node.instances.map((inst) => (
                                                <Tag
                                                    key={inst.instance_id}
                                                    color="blue"
                                                    style={{ margin: 0, fontSize: 11 }}
                                                >
                                                    {inst.plugin_id} [{inst.actual_state}]
                                                </Tag>
                                            ))}
                                        </Space>
                                    ) : (
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            暂未部署插件实例
                                        </Text>
                                    )}
                                </div>
                            </Card>
                        ))}
                    </div>
                ) : !nodesQuery.error ? (
                    <Empty title="暂无可用计算节点">
                        请使用下方接入令牌或启动本地 <code>./deploy/up.sh</code> 进行节点注册。
                    </Empty>
                ) : null}
            </Card>

            {/* 接入令牌生成弹窗 */}
            {tokenModalOpen ? (
                <Modal
                    title="生成 Worker 接入令牌"
                    onClose={() => setTokenModalOpen(false)}
                    width={560}
                >
                    {createdToken ? (
                        <div>
                            <Alert
                                type="success"
                                showIcon
                                message="接入令牌生成成功"
                                description="请使用该凭据在目标计算节点上执行接入命令，节点随后将在上方候选列表中出现。"
                                style={{ marginBottom: 16 }}
                            />

                            <div style={{ marginBottom: 16 }}>
                                <Text
                                    strong
                                    style={{ fontSize: 12, display: 'block', marginBottom: 4 }}
                                >
                                    Token 密钥:
                                </Text>
                                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                                    <code
                                        className="mono"
                                        style={{ flex: 1, padding: '6px 10px', fontSize: 12 }}
                                    >
                                        {createdToken.token}
                                    </code>
                                    <Button
                                        icon={copied ? <CheckOutlined /> : <CopyOutlined />}
                                        onClick={() => copyToken(createdToken.token)}
                                    >
                                        {copied ? '已复制' : '复制'}
                                    </Button>
                                </div>
                            </div>

                            <div style={{ marginBottom: 16 }}>
                                <Text
                                    strong
                                    style={{ fontSize: 12, display: 'block', marginBottom: 4 }}
                                >
                                    快捷启动命令示例:
                                </Text>
                                <pre
                                    style={{
                                        margin: 0,
                                        padding: 12,
                                        borderRadius: 6,
                                        background: '#0f172a',
                                        color: '#38bdf8',
                                        fontSize: 11,
                                        overflow: 'auto',
                                    }}
                                >
                                    {`SENSORYPLEX_ENROLL_TOKEN="${createdToken.token}" cargo run -p sensoryplex-runtime -- worker --node-id "${createdToken.node_id}"`}
                                </pre>
                            </div>

                            <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                                令牌有效期至: {date(createdToken.expires_at)}
                            </Text>

                            <div
                                style={{
                                    display: 'flex',
                                    justifyContent: 'flex-end',
                                    marginTop: 20,
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
                                <Input placeholder="例如：node-gpu-4090 或 worker-mac-studio" />
                            </Form.Item>

                            <Form.Item
                                name="expires_in_minutes"
                                label={<span style={{ fontWeight: 500 }}>有效时长（分钟）</span>}
                                initialValue={60}
                            >
                                <InputNumber min={5} max={1440} style={{ width: '100%' }} />
                            </Form.Item>

                            <div
                                style={{
                                    fontSize: 12,
                                    color: '#64748b',
                                    marginBottom: 20,
                                    padding: '8px 12px',
                                    background: '#f8fafc',
                                    borderRadius: 6,
                                }}
                            >
                                令牌为一次性接入凭据，Worker 启动首次入网后将自动换取安全长效会话。
                            </div>

                            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
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
        </div>
    );
}

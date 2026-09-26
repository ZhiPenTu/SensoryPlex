import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Alert,
    Button,
    Card,
    Col,
    Divider,
    Form,
    Input,
    Popconfirm,
    Row,
    Select,
    Space,
    Table,
    Tabs,
    Tag,
    Typography,
    message,
} from 'antd';
import {
    AppstoreOutlined,
    SettingOutlined,
    CloudDownloadOutlined,
    ThunderboltOutlined,
    CheckCircleOutlined,
    ClusterOutlined,
} from '@ant-design/icons';
import { PluginFields, readConfig } from './PluginFields';
import PluginDeployments from './PluginDeployments';
import { api, post } from '../api/client';
import type {
    NodeList,
    PluginConfigList,
    PluginEntry,
    PluginList,
    PreflightResponse,
} from '../api/contracts';
import {
    Badge,
    date,
    Empty,
    ErrorNotice,
    Heading,
    Loading,
    Modal,
    StatSummary,
} from '../components';

const { Text, Paragraph } = Typography;

export default function Plugins() {
    const cache = useQueryClient();
    const [selected, setSelected] = useState<PluginEntry | null>(null);
    const [installingPlugin, setInstallingPlugin] = useState<PluginEntry | null>(null);
    const [targetNodeId, setTargetNodeId] = useState<string>('');
    const [selectedConfigId, setSelectedConfigId] = useState<string>('');
    const [tab, setTab] = useState('catalog');
    const [configForm] = Form.useForm();

    const catalog = useQuery({
        queryKey: ['catalog'],
        queryFn: ({ signal }) => api<PluginList>('/admin/v1/catalog', { signal }),
    });

    const configs = useQuery({
        queryKey: ['configs'],
        queryFn: ({ signal }) =>
            api<PluginConfigList>('/admin/v1/plugin-configurations?limit=100', { signal }),
    });

    const nodes = useQuery({
        queryKey: ['nodes'],
        queryFn: ({ signal }) => api<NodeList>('/admin/v1/nodes?limit=100', { signal }),
    });

    const preflight = useQuery({
        queryKey: ['preflight', targetNodeId, installingPlugin?.id, selectedConfigId],
        queryFn: () =>
            post<PreflightResponse>(`/admin/v1/nodes/${targetNodeId}/preflight`, {
                node_id: targetNodeId,
                plugin_id: installingPlugin!.id,
                config_id: selectedConfigId || undefined,
            }),
        enabled: !!targetNodeId && !!installingPlugin,
        retry: false,
    });

    const deploy = useMutation({
        mutationFn: () =>
            post(`/admin/v1/nodes/${targetNodeId}/plugins/${installingPlugin!.id}:deploy`, {
                config_id: selectedConfigId || undefined,
            }),
        onSuccess: () => {
            message.success('已成功下发插件部署指令至计算节点');
            void cache.invalidateQueries({ queryKey: ['nodes'] });
            setInstallingPlugin(null);
            setTargetNodeId('');
            setSelectedConfigId('');
        },
    });

    const [batchNotice, setBatchNotice] = useState<string>('');
    const batchDeploy = useMutation({
        mutationFn: () =>
            post<{ node_id: string; deployed: string[]; rejected: any[] }>(
                '/admin/v1/nodes/local-host/plugins:batch-deploy',
                {},
            ),
        onSuccess: (data) => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
            message.success(`已向 ${data.node_id} 下发 ${data.deployed.length} 个插件安装意图！`);
            setBatchNotice(
                `已向 ${data.node_id} 下发 ${data.deployed.length} 个插件安装意图！` +
                    (data.rejected.length
                        ? ` (另有 ${data.rejected.length} 个受预检限制未部署)`
                        : ''),
            );
            setTimeout(() => setBatchNotice(''), 6000);
        },
    });

    const save = useMutation({
        mutationFn: (form: FormData) =>
            post('/admin/v1/plugin-configurations', {
                plugin_id: selected!.id,
                name: form.get('name'),
                config: readConfig(form, selected!.config_schema),
            }),
        onSuccess: () => {
            message.success('插件配置方案已成功保存');
            void cache.invalidateQueries({ queryKey: ['configs'] });
            setSelected(null);
            setTab('configs');
        },
    });

    const catalogItems = catalog.data?.items || [];
    const configList = configs.data?.items || [];
    const configCount = configs.data?.total ?? configList.length;
    const readyNodes =
        nodes.data?.items?.filter((node) => node.status === 'NODE_STATUS_READY').length || 0;
    const deployedInstances =
        nodes.data?.items?.reduce((total, node) => total + (node.instances?.length || 0), 0) || 0;

    const configColumns = [
        {
            title: '配置名称',
            dataIndex: 'name',
            key: 'name',
            render: (name: string, row: (typeof configList)[0]) => (
                <Space align="center" size={10}>
                    <SettingOutlined style={{ color: '#1668dc' }} />
                    <Text strong style={{ fontSize: 13 }}>
                        {name}
                    </Text>
                    <span className="mono" style={{ fontSize: 11, color: '#94a3b8' }}>
                        ({row.id.slice(0, 16)}…)
                    </span>
                </Space>
            ),
        },
        {
            title: '绑定插件',
            dataIndex: 'plugin_id',
            key: 'plugin_id',
            render: (pid: string) => (
                <Tag color="geekblue" style={{ fontFamily: 'monospace' }}>
                    {pid}
                </Tag>
            ),
        },
        {
            title: '配置详情',
            dataIndex: 'config',
            key: 'config',
            render: (conf: Record<string, unknown>) => (
                <code
                    style={{
                        maxWidth: 360,
                        display: 'inline-block',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                    }}
                >
                    {JSON.stringify(conf)}
                </code>
            ),
        },
        {
            title: '创建时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 180,
            render: (time: string) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                    {date(time)}
                </Text>
            ),
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Plugin Center"
                title="插件生态与配置中心"
                description="统一接入视觉大模型 (VLM)、语音识别 (ASR)、光学字符 (OCR) 与向量特征嵌入等模型算力插件。"
                action={
                    <Popconfirm
                        title="确定向同机数据面节点 (local-host) 一键装配全部基础处理插件（VLM、ASR、OCR、Embedding）吗？"
                        onConfirm={() => batchDeploy.mutate()}
                        okText="立即装配"
                        cancelText="取消"
                    >
                        <Button
                            type="primary"
                            icon={<ThunderboltOutlined />}
                            loading={batchDeploy.isPending}
                        >
                            一键装配本机基础插件
                        </Button>
                    </Popconfirm>
                }
            />

            <Row gutter={[16, 16]} style={{ marginBottom: 20 }}>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="已登记插件规范"
                        value={catalogItems.length}
                        prefix={<AppstoreOutlined style={{ color: '#1668dc' }} />}
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="自定义配置方案"
                        value={configCount}
                        prefix={<SettingOutlined style={{ color: '#08979c' }} />}
                        color="#08979c"
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="可部署就绪节点"
                        value={readyNodes}
                        prefix={<ClusterOutlined style={{ color: '#10b981' }} />}
                        color="#10b981"
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="活跃运行实例"
                        value={deployedInstances}
                        prefix={<CheckCircleOutlined style={{ color: '#8b5cf6' }} />}
                        color="#8b5cf6"
                    />
                </Col>
            </Row>

            {batchNotice ? (
                <Alert type="success" showIcon message={batchNotice} style={{ marginBottom: 16 }} />
            ) : null}

            <ErrorNotice
                error={
                    catalog.error ||
                    configs.error ||
                    nodes.error ||
                    deploy.error ||
                    batchDeploy.error
                }
            />

            <Card bodyStyle={{ padding: '0 20px 20px' }}>
                <Tabs
                    activeKey={tab}
                    onChange={setTab}
                    items={[
                        {
                            key: 'catalog',
                            label: (
                                <Space size={6}>
                                    <AppstoreOutlined />
                                    <span>插件规范目录 ({catalogItems.length})</span>
                                </Space>
                            ),
                            children: (
                                <div>
                                    {catalog.isPending ? (
                                        <Loading tip="正在载入插件目录…" />
                                    ) : (
                                        <Row gutter={[16, 16]}>
                                            {catalogItems.map((plugin) => (
                                                <Col xs={24} md={12} key={plugin.id}>
                                                    <Card
                                                        size="small"
                                                        style={{
                                                            borderRadius: 8,
                                                            border: '1px solid #e2e8f0',
                                                            height: '100%',
                                                            display: 'flex',
                                                            flexDirection: 'column',
                                                        }}
                                                        bodyStyle={{
                                                            padding: 18,
                                                            flex: 1,
                                                            display: 'flex',
                                                            flexDirection: 'column',
                                                        }}
                                                    >
                                                        <div
                                                            style={{
                                                                display: 'flex',
                                                                justifyContent: 'space-between',
                                                                alignItems: 'flex-start',
                                                                marginBottom: 10,
                                                            }}
                                                        >
                                                            <div>
                                                                <Text
                                                                    strong
                                                                    style={{ fontSize: 15 }}
                                                                >
                                                                    {plugin.name}
                                                                </Text>
                                                                <div style={{ marginTop: 2 }}>
                                                                    <span
                                                                        className="mono"
                                                                        style={{
                                                                            fontSize: 11,
                                                                            color: '#64748b',
                                                                        }}
                                                                    >
                                                                        {plugin.id}
                                                                    </span>
                                                                </div>
                                                            </div>
                                                            <Space size={4}>
                                                                <Badge state={plugin.state} />
                                                                <Tag
                                                                    color={
                                                                        plugin.trust === 'official'
                                                                            ? 'green'
                                                                            : 'orange'
                                                                    }
                                                                >
                                                                    {plugin.trust || 'unverified'}
                                                                </Tag>
                                                            </Space>
                                                        </div>

                                                        <Paragraph
                                                            type="secondary"
                                                            style={{
                                                                fontSize: 13,
                                                                marginBottom: 12,
                                                                flex: 1,
                                                            }}
                                                        >
                                                            {plugin.description ||
                                                                '按官方契约提供的高性能模型处理组件。'}
                                                        </Paragraph>

                                                        <div style={{ marginBottom: 16 }}>
                                                            <Space size={[4, 4]} wrap>
                                                                <Tag color="cyan">
                                                                    版本: {plugin.version}
                                                                </Tag>
                                                                <Tag color="purple">
                                                                    摘要:{' '}
                                                                    {plugin.digest
                                                                        ? plugin.digest.slice(
                                                                              0,
                                                                              12,
                                                                          ) + '…'
                                                                        : '官方契约'}
                                                                </Tag>
                                                                {plugin.consumes?.map((c) => (
                                                                    <Tag key={c} color="blue">
                                                                        输入: {c}
                                                                    </Tag>
                                                                ))}
                                                                {plugin.produces?.map((p) => (
                                                                    <Tag key={p} color="green">
                                                                        产出: {p}
                                                                    </Tag>
                                                                ))}
                                                            </Space>
                                                        </div>

                                                        <div
                                                            style={{
                                                                marginTop: 'auto',
                                                                paddingTop: 12,
                                                                borderTop: '1px solid #f1f5f9',
                                                                display: 'flex',
                                                                justifyContent: 'flex-end',
                                                                gap: 8,
                                                            }}
                                                        >
                                                            <Button
                                                                size="small"
                                                                icon={<SettingOutlined />}
                                                                onClick={() => {
                                                                    setSelected(plugin);
                                                                    configForm.resetFields();
                                                                }}
                                                            >
                                                                配置参数
                                                            </Button>
                                                            <Button
                                                                size="small"
                                                                type="primary"
                                                                icon={<CloudDownloadOutlined />}
                                                                onClick={() => {
                                                                    setInstallingPlugin(plugin);
                                                                    setTargetNodeId(
                                                                        readyNodes > 0
                                                                            ? nodes.data?.items?.find(
                                                                                  (n) =>
                                                                                      n.status ===
                                                                                      'NODE_STATUS_READY',
                                                                              )?.node_id || ''
                                                                            : '',
                                                                    );
                                                                    setSelectedConfigId('');
                                                                }}
                                                            >
                                                                部署意图
                                                            </Button>
                                                        </div>
                                                    </Card>
                                                </Col>
                                            ))}
                                        </Row>
                                    )}
                                </div>
                            ),
                        },
                        {
                            key: 'deployments',
                            label: (
                                <Space size={6}>
                                    <CloudDownloadOutlined />
                                    <span>热部署（ADR-030）</span>
                                </Space>
                            ),
                            children: <PluginDeployments />,
                        },
                        {
                            key: 'configs',
                            label: (
                                <Space size={6}>
                                    <SettingOutlined />
                                    <span>参数方案配置 ({configList.length})</span>
                                </Space>
                            ),
                            children: (
                                <div>
                                    {configs.isPending ? (
                                        <Loading tip="正在载入方案配置…" />
                                    ) : configList.length ? (
                                        <Table
                                            columns={configColumns}
                                            dataSource={configList}
                                            rowKey="id"
                                            pagination={{ pageSize: 15 }}
                                        />
                                    ) : (
                                        <Empty title="暂无自定义配置方案">
                                            在插件目录中点击【配置参数】可根据插件 Manifest
                                            生成并保存可复用的运行参数。
                                        </Empty>
                                    )}
                                </div>
                            ),
                        },
                    ]}
                />
            </Card>

            {/* 新建/保存插件配置 Modal */}
            {selected ? (
                <Modal
                    title={`新建插件运行配置 · ${selected.name}`}
                    onClose={() => setSelected(null)}
                    width={600}
                >
                    <ErrorNotice error={save.error} />
                    <form
                        onSubmit={(e) => {
                            e.preventDefault();
                            save.mutate(new FormData(e.currentTarget));
                        }}
                    >
                        <div style={{ marginBottom: 14 }}>
                            <Text
                                strong
                                style={{ fontSize: 13, display: 'block', marginBottom: 4 }}
                            >
                                配置方案名称
                            </Text>
                            <Input
                                autoFocus
                                required
                                name="name"
                                placeholder={`例如：${selected.name} 默认高精度配置`}
                            />
                        </div>

                        <Divider style={{ margin: '14px 0' }} />

                        <PluginFields schema={selected.config_schema} />

                        <div
                            style={{
                                marginTop: 24,
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                            }}
                        >
                            <Button onClick={() => setSelected(null)}>取消</Button>
                            <Button type="primary" htmlType="submit" loading={save.isPending}>
                                保存方案
                            </Button>
                        </div>
                    </form>
                </Modal>
            ) : null}

            {/* 部署插件至目标节点 Modal */}
            {installingPlugin ? (
                <Modal
                    title={`下发插件部署意图 · ${installingPlugin.name}`}
                    onClose={() => setInstallingPlugin(null)}
                    width={580}
                >
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                        <div>
                            <Text
                                strong
                                style={{ fontSize: 13, display: 'block', marginBottom: 6 }}
                            >
                                目标计算节点:
                            </Text>
                            <Select
                                value={targetNodeId || undefined}
                                onChange={setTargetNodeId}
                                placeholder="选择接入集群的计算节点"
                                style={{ width: '100%' }}
                                options={nodes.data?.items?.map((n) => ({
                                    label: `${n.node_id} [${n.status === 'NODE_STATUS_READY' ? '可调度' : n.status}]`,
                                    value: n.node_id,
                                    disabled: n.status !== 'NODE_STATUS_READY',
                                }))}
                            />
                        </div>

                        <div>
                            <Text
                                strong
                                style={{ fontSize: 13, display: 'block', marginBottom: 6 }}
                            >
                                运行配置方案 (可选):
                            </Text>
                            <Select
                                value={selectedConfigId || undefined}
                                onChange={setSelectedConfigId}
                                placeholder="选择适用的插件配置方案 (默认使用标准参数)"
                                style={{ width: '100%' }}
                                allowClear
                                options={configList
                                    .filter((c) => c.plugin_id === installingPlugin.id)
                                    .map((c) => ({
                                        label: `${c.name} (${c.id.slice(0, 16)}…)`,
                                        value: c.id,
                                    }))}
                            />
                        </div>

                        {targetNodeId ? (
                            <div>
                                <Text
                                    strong
                                    style={{ fontSize: 13, display: 'block', marginBottom: 6 }}
                                >
                                    ADR-026 控制面能力预检 (Preflight):
                                </Text>
                                {preflight.isPending ? (
                                    <div
                                        style={{
                                            padding: 12,
                                            background: '#f8fafc',
                                            borderRadius: 6,
                                        }}
                                    >
                                        <Text type="secondary">
                                            正在执行硬件能力与数据本地性预检…
                                        </Text>
                                    </div>
                                ) : preflight.data ? (
                                    preflight.data.eligible ? (
                                        <Alert
                                            type="success"
                                            showIcon
                                            message="预检通过"
                                            description="目标节点的芯片架构、加速卡驱动、显存及数据本地性完全满足该插件运行要求。"
                                        />
                                    ) : (
                                        <Alert
                                            type="error"
                                            showIcon
                                            message={`预检未通过 (${preflight.data.reason_code})`}
                                            description={preflight.data.detail}
                                        />
                                    )
                                ) : (
                                    <Alert type="warning" message="无法获取节点预检状态" />
                                )}
                            </div>
                        ) : null}

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 12,
                            }}
                        >
                            <Button onClick={() => setInstallingPlugin(null)}>取消</Button>
                            <Button
                                type="primary"
                                icon={<CloudDownloadOutlined />}
                                disabled={
                                    !targetNodeId || !preflight.data?.eligible || deploy.isPending
                                }
                                loading={deploy.isPending}
                                onClick={() => deploy.mutate()}
                            >
                                下发部署意图（旧通路）
                            </Button>
                        </div>
                    </div>
                </Modal>
            ) : null}
        </div>
    );
}

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
import PluginFleetMatrix, { compareSemver } from './PluginFleetMatrix';
import PluginTrust from './PluginTrust';
import { api, post, RequestError } from '../api/client';
import type {
    NodeList,
    PluginConfigList,
    PluginEntry,
    PluginList,
    PluginReleaseList,
    PreflightResponse,
    BatchDeployPluginsResponse,
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
    const [tab, setTab] = useState('matrix');
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
        refetchInterval: 2500,
    });

    const releases = useQuery({
        queryKey: ['plugin-releases'],
        queryFn: ({ signal }) =>
            api<PluginReleaseList>('/admin/v1/plugin-releases?limit=100', { signal }),
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
        mutationFn: () => {
            const targetNode = nodes.data?.items?.find((n) => n.node_id === targetNodeId);
            const inst = targetNode?.instances?.find((i) => i.plugin_id === installingPlugin!.id);
            const isUpgrade = !!(inst && inst.active_runtime_instance_id);

            let releaseId: string | undefined = installingPlugin!.release_id;
            if (!releaseId && targetNode) {
                const nodePlatform = targetNode.capabilities?.platform || 'macos';
                const nodeArch = targetNode.capabilities?.arch || 'aarch64';
                const candidateReleases = (releases.data?.items || []).filter(
                    (r) =>
                        r.plugin_id === installingPlugin!.id &&
                        r.platform === nodePlatform &&
                        r.arch === nodeArch &&
                        r.authenticated &&
                        r.trust === 'first_party',
                );
                candidateReleases.sort((a, b) => {
                    const cmp = compareSemver(b.plugin_version, a.plugin_version);
                    if (cmp !== 0) return cmp;
                    return new Date(b.published_at).getTime() - new Date(a.published_at).getTime();
                });
                releaseId = candidateReleases[0]?.release_id;
            }

            const action = isUpgrade ? 'upgrade' : 'provision';
            return post(
                `/admin/v1/nodes/${targetNodeId}/plugins/${installingPlugin!.id}:${action}`,
                {
                    config_id: selectedConfigId || undefined,
                    release_id: releaseId || undefined,
                },
            );
        },
        onSuccess: () => {
            message.success('已成功下发插件部署指令至计算节点');
            void cache.invalidateQueries({ queryKey: ['nodes'] });
            void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
            setInstallingPlugin(null);
            setTargetNodeId('');
            setSelectedConfigId('');
            setTab('deployments');
        },
    });

    const batchDeploy = useMutation({
        mutationFn: async () => {
            await post('/admin/v1/plugin-releases:sync', {});
            return post<BatchDeployPluginsResponse>(
                '/admin/v1/nodes/local-host/plugins:batch-deploy',
                {},
            );
        },
        onSuccess: (data) => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
            void cache.invalidateQueries({ queryKey: ['configs'] });
            void cache.invalidateQueries({ queryKey: ['plugin-releases'] });
            void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
            if (data.operations.length) setTab('deployments');
        },
    });

    const save = useMutation({
        mutationFn: (form: FormData) =>
            post('/admin/v1/plugin-configurations', {
                plugin_id: selected!.id,
                release_id: selected!.release_id || undefined,
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
        nodes.data?.items?.reduce(
            (total, node) =>
                total +
                (node.instances?.filter(
                    (instance) =>
                        instance.actual_state === 'ready' &&
                        Boolean(instance.active_runtime_instance_id && instance.endpoint),
                ).length || 0),
            0,
        ) || 0;
    const localInstances =
        nodes.data?.items?.find((node) => node.node_id === 'local-host')?.instances || [];
    const isInstalled = (plugin: PluginEntry) =>
        localInstances.some(
            (instance) =>
                instance.plugin_id === plugin.id &&
                instance.actual_state === 'ready' &&
                instance.artifact_digest === plugin.digest &&
                Boolean(instance.active_runtime_instance_id && instance.endpoint),
        );

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
                description="管理模型与确定性处理插件，配置参数并安装已验证的发布版本。"
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

            <Row gutter={[8, 8]} style={{ marginBottom: 10 }}>
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

            {batchDeploy.data ? (
                <Alert
                    type={batchDeploy.data.rejected.length ? 'warning' : 'info'}
                    showIcon
                    message={`本次装配：${batchDeploy.data.operations.length} 个部署操作，${batchDeploy.data.already_ready.length} 个已就绪，${batchDeploy.data.rejected.length} 个未受理`}
                    description={
                        <div>
                            <div>
                                安装进度见下方热部署记录；“已受理”表示排队，“已完成”才表示安装并验证成功。
                            </div>
                            {batchDeploy.data.rejected.map((item) => (
                                <div key={item.plugin_id}>
                                    {catalogItems.find((plugin) => plugin.id === item.plugin_id)
                                        ?.name || item.plugin_id}
                                    ：{new RequestError(422, item.reason).message}
                                </div>
                            ))}
                        </div>
                    }
                    style={{ marginBottom: 10 }}
                />
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

            <Card
                styles={{ body: { padding: '0 12px 12px' } }}
                bodyStyle={{ padding: '0 12px 12px' }}
            >
                <Tabs
                    activeKey={tab}
                    onChange={setTab}
                    items={[
                        {
                            key: 'matrix',
                            label: (
                                <Space size={6}>
                                    <ClusterOutlined />
                                    <span>集群拓扑矩阵 (Fleet Matrix)</span>
                                </Space>
                            ),
                            children: (
                                <PluginFleetMatrix
                                    onSelectDeploy={(nId, plugin) => {
                                        setInstallingPlugin(plugin);
                                        setTargetNodeId(nId);
                                        setSelectedConfigId('');
                                    }}
                                    onSwitchToDeployments={() => setTab('deployments')}
                                />
                            ),
                        },
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
                                        <Row gutter={[8, 8]}>
                                            {catalogItems.map((plugin) => (
                                                <Col
                                                    xs={24}
                                                    md={12}
                                                    key={plugin.release_id || plugin.id}
                                                >
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
                                                            padding: 12,
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
                                                                marginBottom: 6,
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
                                                                        isInstalled(plugin) ||
                                                                        plugin.trust === 'official'
                                                                            ? 'green'
                                                                            : 'orange'
                                                                    }
                                                                >
                                                                    {plugin.trust === 'trusted_publisher' ? '受信发布者' : plugin.trust || '未验证'}
                                                                </Tag>
                                                                {isInstalled(plugin) ? <Tag color="green">本机已安装运行</Tag> : null}
                                                            </Space>
                                                        </div>

                                                        <Paragraph
                                                            type="secondary"
                                                            style={{
                                                                fontSize: 12,
                                                                marginBottom: 8,
                                                                flex: 1,
                                                            }}
                                                        >
                                                            {plugin.description ||
                                                                '按标准契约提供的处理插件。'}
                                                        </Paragraph>

                                                        <div style={{ marginBottom: 8 }}>
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
                                                                paddingTop: 8,
                                                                borderTop: '1px solid #f1f5f9',
                                                                display: 'flex',
                                                                justifyContent: 'flex-end',
                                                                gap: 6,
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
                                                                {plugin.release_id ? '安装此版本' : '部署意图'}
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
                        {
                            key: 'trust',
                            label: '发布者与外部制品',
                            children: <PluginTrust />,
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
                    title={`${installingPlugin.release_id ? '安装插件版本' : '下发插件部署意图'} · ${installingPlugin.name}`}
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
                                {installingPlugin.release_id ? '开始安装' : '下发部署意图'}
                            </Button>
                        </div>
                    </div>
                </Modal>
            ) : null}
        </div>
    );
}

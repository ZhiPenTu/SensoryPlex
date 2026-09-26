import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Alert,
    Button,
    Card,
    Checkbox,
    Col,
    Form,
    Input,
    InputNumber,
    Popconfirm,
    Row,
    Select,
    Space,
    Table,
    Tag,
    Typography,
    message,
} from 'antd';
import {
    ApartmentOutlined,
    PlusOutlined,
    FolderOutlined,
    EditOutlined,
    KeyOutlined,
    CopyOutlined,
    CheckOutlined,
    StopOutlined,
    DashboardOutlined,
    CheckCircleOutlined,
    ExclamationCircleOutlined,
    UserOutlined,
} from '@ant-design/icons';
import { api, post } from '../api/client';
import type {
    AccessToken,
    AuditList,
    ConsoleStatus,
    PipelineList,
    PluginConfigList,
    TokenList,
    UserList,
    User,
} from '../api/contracts';
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
import { usePermission, useSession } from '../session';

const { Text } = Typography;

const MULTIMODAL_PLUGIN_IDS = {
    ocr: 'org.sensoryplex.ocr-rapidocr',
    asr: 'org.sensoryplex.asr-whisper-mlx',
    vlm: 'org.sensoryplex.vlm-moondream',
} as const;

const multimodalInitialValues = {
    policy: {
        // Coverage 是不允许被调低的事实契约；其余参数进入不可变 Revision。
        window_ms: 1000,
        sample_interval_ms: 1000,
        audio_segment_ms: 6000,
        // Runtime 尚未实现 overlap，首期将其锁为 0，避免把未生效的配置写进 Revision。
    audio_overlap_ms: 500,
        vlm_sample_interval_ms: 5000,
    },
};

type MultimodalFormValues = {
    name: string;
    description?: string;
    ocr_config_id: string;
    asr_config_id: string;
    vlm_config_id?: string;
    policy: {
        window_ms: number;
        sample_interval_ms: number;
        audio_segment_ms: number;
        audio_overlap_ms: number;
        vlm_sample_interval_ms: number;
    };
};

type MultimodalValidation = {
    valid: boolean;
    errors: string[];
    graph_digest: string;
    topological_order: string[];
    fingerprint?: string;
};

function multimodalPayload(values: MultimodalFormValues) {
    return {
        name: values.name,
        description: values.description || '',
        components: [
            { node_id: 'ocr_fast', config_id: values.ocr_config_id },
            { node_id: 'asr_fast', config_id: values.asr_config_id },
            ...(values.vlm_config_id
                ? [{ node_id: 'vlm_enrich', config_id: values.vlm_config_id, required: false }]
                : []),
        ],
        policy: values.policy,
    };
}

/**
 * 处理方案管理 (Pipelines)
 */
export function Pipelines() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [open, setOpen] = useState(false);
    const [form] = Form.useForm();
    const [validation, setValidation] = useState<MultimodalValidation | null>(null);

    const listing = useQuery({
        queryKey: ['pipelines', offset],
        queryFn: ({ signal }) =>
            api<PipelineList>(`/v1/pipelines?limit=20&offset=${offset}`, { signal }),
    });

    const configs = useQuery({
        queryKey: ['configs'],
        queryFn: ({ signal }) =>
            api<PluginConfigList>('/admin/v1/plugin-configurations?limit=100', { signal }),
    });

    const validate = useMutation({
        mutationFn: (body: ReturnType<typeof multimodalPayload>) =>
            post<MultimodalValidation>('/admin/v1/multimodal-pipelines:validate', body),
        onSuccess: (result, body) => {
            setValidation({ ...result, fingerprint: JSON.stringify(body) });
            if (result.valid) message.success('多模态编排校验通过，已锁定本次图摘要。');
        },
    });

    const save = useMutation({
        mutationFn: (body: ReturnType<typeof multimodalPayload>) =>
            post('/admin/v1/multimodal-pipelines', body),
        onSuccess: () => {
            message.success('多模态处理方案已保存为不可变编排版本');
            setOpen(false);
            form.resetFields();
            setValidation(null);
            void cache.invalidateQueries({ queryKey: ['pipelines'] });
        },
    });

    const archive = useMutation({
        mutationFn: (id: string) => post(`/admin/v1/pipelines/${id}:archive`),
        onSuccess: () => {
            message.success('方案已归档');
            void cache.invalidateQueries({ queryKey: ['pipelines'] });
        },
    });

    const items = listing.data?.items || [];
    const totalCount = listing.data?.total || 0;
    const configsByPlugin = (pluginId: string) =>
        configs.data?.items.filter((item) => item.plugin_id === pluginId) || [];

    const validateForm = async () => {
        try {
            const values = (await form.validateFields()) as MultimodalFormValues;
            validate.mutate(multimodalPayload(values));
        } catch {
            // Form 已在字段旁展示校验错误，避免再额外弹出同一条提示。
        }
    };

    const saveForm = (values: MultimodalFormValues) => {
        const body = multimodalPayload(values);
        if (!validation?.valid || validation.fingerprint !== JSON.stringify(body)) {
            message.warning('当前配置尚未校验，或校验后已被修改；请先重新校验。');
            return;
        }
        save.mutate(body);
    };

    const columns = [
        {
            title: '方案名称',
            dataIndex: 'name',
            key: 'name',
            render: (name: string, row: (typeof items)[0]) => (
                <Space size={10} align="start">
                    <div
                        style={{
                            width: 34,
                            height: 34,
                            borderRadius: 6,
                            background: '#eff6ff',
                            color: '#1668dc',
                            display: 'grid',
                            placeItems: 'center',
                            fontSize: 16,
                            flexShrink: 0,
                        }}
                    >
                        <ApartmentOutlined />
                    </div>
                    <div>
                        <Text strong style={{ fontSize: 13 }}>
                            {name}
                        </Text>
                        <div style={{ fontSize: 12, color: '#64748b' }}>
                            {row.description || `插件: ${row.plugin_id}`}
                        </div>
                    </div>
                </Space>
            ),
        },
        {
            title: '版本',
            dataIndex: 'revision',
            key: 'revision',
            width: 100,
            render: (rev: number) => <Tag color="purple">v{rev}</Tag>,
        },
        {
            title: '执行语义',
            key: 'execution',
            width: 180,
            render: (_: unknown, row: (typeof items)[0]) =>
                row.execution_mode === 'orchestrated_v2' ? (
                    <Space size={[4, 4]} wrap>
                        <Tag color="blue">多模态编排 v2</Tag>
                        <Text type="secondary" style={{ fontSize: 11 }}>
                            图 v{row.orchestration_revision}
                        </Text>
                    </Space>
                ) : (
                    <Tag>兼容草稿</Tag>
                ),
        },
        {
            title: '状态',
            dataIndex: 'state',
            key: 'state',
            width: 120,
            render: (state: string) => <Badge state={state} />,
        },
        {
            title: '保存时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 180,
            render: (time: string) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                    {date(time)}
                </Text>
            ),
        },
        {
            title: '操作',
            key: 'actions',
            width: 140,
            align: 'right' as const,
            render: (_: unknown, row: (typeof items)[0]) => (
                <Space size={8}>
                    {row.state === 'draft' ? (
                        <Popconfirm
                            title="确定归档此方案草稿？"
                            onConfirm={() => archive.mutate(row.id)}
                            okText="归档"
                            cancelText="取消"
                        >
                            <Button
                                size="small"
                                danger
                                type="text"
                                icon={<FolderOutlined />}
                                loading={archive.isPending}
                            >
                                归档
                            </Button>
                        </Popconfirm>
                    ) : null}
                </Space>
            ),
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Pipeline Templates"
                title="处理方案管理"
                description="将基础插件、模型配置参数与时序编排固化为可追溯、可审计的方案版本。"
                action={
                    <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={() => {
                            save.reset();
                            form.resetFields();
                            setOpen(true);
                        }}
                    >
                        新建方案
                    </Button>
                }
            />

            <Notice>
                方案以 OCR 与 ASR 为必需快路径，VLM 为可选慢路径；每秒 coverage 是固定事实网格，
                采样与分段策略会写入不可变 Revision，而不是依赖全局 YAML。
            </Notice>

            <ErrorNotice
                error={listing.error || configs.error || archive.error || validate.error || save.error}
            />

            <Card bodyStyle={{ padding: 0 }}>
                {listing.isPending ? (
                    <Loading tip="正在载入处理方案…" />
                ) : items.length ? (
                    <Table
                        columns={columns}
                        dataSource={items}
                        rowKey="id"
                        pagination={{
                            current: Math.floor(offset / 20) + 1,
                            pageSize: 20,
                            total: totalCount,
                            showTotal: (total) => `共 ${total} 个方案`,
                            onChange: (page) => setOffset((page - 1) * 20),
                        }}
                    />
                ) : !listing.error ? (
                    <Empty title="暂无处理方案">
                        配置好插件参数后，可以在此处新建方案并指定关联的参数模版。
                    </Empty>
                ) : null}
            </Card>

            {open ? (
                <Modal title="新建多模态处理方案" onClose={() => setOpen(false)} width={680}>
                    <Form
                        form={form}
                        layout="vertical"
                        initialValues={multimodalInitialValues}
                        onValuesChange={() => setValidation(null)}
                        onFinish={saveForm}
                    >
                        <Form.Item
                            name="name"
                            label={<span style={{ fontWeight: 500 }}>方案名称</span>}
                            rules={[{ required: true, message: '请输入方案名称' }]}
                        >
                            <Input placeholder="例如：全模态标准分析流水线" maxLength={120} />
                        </Form.Item>

                        <Form.Item
                            name="description"
                            label={<span style={{ fontWeight: 500 }}>方案描述 (可选)</span>}
                        >
                            <Input.TextArea
                                rows={2}
                                placeholder="输入方案的应用场景或说明"
                                maxLength={400}
                            />
                        </Form.Item>

                        <Row gutter={12}>
                            <Col xs={24} md={12}>
                                <Form.Item
                                    name="ocr_config_id"
                                    label={<span style={{ fontWeight: 500 }}>OCR 快路径配置</span>}
                                    rules={[{ required: true, message: '请选择 OCR 配置' }]}
                                >
                                    <Select
                                        loading={configs.isPending}
                                        placeholder="选择 RapidOCR 配置"
                                        options={configsByPlugin(MULTIMODAL_PLUGIN_IDS.ocr).map((item) => ({
                                            label: `${item.name} · v${item.revision}`,
                                            value: item.id,
                                        }))}
                                    />
                                </Form.Item>
                            </Col>
                            <Col xs={24} md={12}>
                                <Form.Item
                                    name="asr_config_id"
                                    label={<span style={{ fontWeight: 500 }}>ASR 快路径配置</span>}
                                    rules={[{ required: true, message: '请选择 ASR 配置' }]}
                                >
                                    <Select
                                        loading={configs.isPending}
                                        placeholder="选择 Whisper 配置"
                                        options={configsByPlugin(MULTIMODAL_PLUGIN_IDS.asr).map((item) => ({
                                            label: `${item.name} · v${item.revision}`,
                                            value: item.id,
                                        }))}
                                    />
                                </Form.Item>
                            </Col>
                        </Row>

                        <Form.Item
                            name="vlm_config_id"
                            label={<span style={{ fontWeight: 500 }}>VLM 慢路径配置（可选）</span>}
                            extra="不选择 VLM 时，执行仍会产生完整 coverage；画面描述会明确显示为未配置，而不会伪造结果。"
                        >
                            <Select
                                allowClear
                                loading={configs.isPending}
                                placeholder="可选：选择 Moondream 画面描述配置"
                                options={configsByPlugin(MULTIMODAL_PLUGIN_IDS.vlm).map((item) => ({
                                    label: `${item.name} · v${item.revision}`,
                                    value: item.id,
                                }))}
                            />
                        </Form.Item>

                        <Card size="small" title="时间轴与采样策略" style={{ marginBottom: 16 }}>
                            <Row gutter={12}>
                                <Col xs={24} md={8}>
                                    <Form.Item name={['policy', 'window_ms']} label="Coverage 窗口（毫秒）">
                                        <InputNumber disabled style={{ width: '100%' }} />
                                    </Form.Item>
                                </Col>
                                <Col xs={24} md={8}>
                                    <Form.Item
                                        name={['policy', 'sample_interval_ms']}
                                        label="视频采样最小间隔（毫秒）"
                                        rules={[{ required: true, type: 'number', min: 250, max: 60000 }]}
                                    >
                                        <InputNumber min={250} max={60000} step={250} style={{ width: '100%' }} />
                                    </Form.Item>
                                </Col>
                                <Col xs={24} md={8}>
                                    <Form.Item
                                        name={['policy', 'audio_segment_ms']}
                                        label="ASR 音频切段（毫秒）"
                                        rules={[{ required: true, type: 'number', min: 1000, max: 60000 }]}
                                    >
                                        <InputNumber min={1000} max={60000} step={1000} style={{ width: '100%' }} />
                                    </Form.Item>
                                </Col>
                                <Col xs={24} md={12}>
                                    <Form.Item name={['policy', 'audio_overlap_ms']} label="ASR 重叠（毫秒）">
                                        <InputNumber disabled style={{ width: '100%' }} />
                                    </Form.Item>
                                </Col>
                                <Col xs={24} md={12}>
                                    <Form.Item
                                        name={['policy', 'vlm_sample_interval_ms']}
                                        label="VLM 采样间隔（毫秒）"
                                        rules={[{ required: true, type: 'number', min: 1000, max: 60000 }]}
                                    >
                                        <InputNumber min={1000} max={60000} step={1000} style={{ width: '100%' }} />
                                    </Form.Item>
                                </Col>
                            </Row>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                1 秒窗口是不可变协议；音频 overlap 尚未接入 Runtime，当前强制为 0，避免配置看似生效而实际无效。
                            </Text>
                        </Card>

                        {validation ? (
                            <Alert
                                type={validation.valid ? 'success' : 'error'}
                                showIcon
                                message={validation.valid ? '编排校验通过' : '编排校验未通过'}
                                description={
                                    validation.valid
                                        ? `图摘要：${validation.graph_digest}；执行顺序：${validation.topological_order.join(' → ')}`
                                        : validation.errors.join('；')
                                }
                                style={{ marginBottom: 16 }}
                            />
                        ) : null}

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 24,
                            }}
                        >
                            <Button onClick={() => setOpen(false)}>取消</Button>
                            <Button loading={validate.isPending} onClick={() => void validateForm()}>
                                校验方案
                            </Button>
                            <Button
                                type="primary"
                                htmlType="submit"
                                loading={save.isPending}
                                disabled={!validation?.valid}
                            >
                                保存不可变方案
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}
        </div>
    );
}

/**
 * 访问凭据管理 (Access Tokens)
 */
export function Access() {
    const cache = useQueryClient();
    const session = useSession();
    const [open, setOpen] = useState(false);
    const [secret, setSecret] = useState('');
    const [copied, setCopied] = useState(false);
    const [form] = Form.useForm();

    const query = useQuery({
        queryKey: ['tokens'],
        queryFn: ({ signal }) => api<TokenList>('/auth/v1/access-tokens', { signal }),
    });

    const save = useMutation({
        mutationFn: (values: { name: string; days: number; scopes: string[] }) =>
            post<AccessToken>('/auth/v1/access-tokens', {
                name: values.name,
                expires_in_days: Number(values.days || 30),
                scopes: values.scopes || [],
            }),
        onSuccess: (result) => {
            message.success('访问凭据创建成功');
            setSecret(result.token);
            setOpen(false);
            form.resetFields();
            void cache.invalidateQueries({ queryKey: ['tokens'] });
        },
    });

    const revoke = useMutation({
        mutationFn: (id: string) => post(`/auth/v1/access-tokens/${id}:revoke`),
        onSuccess: () => {
            message.success('已撤销该访问凭据');
            void cache.invalidateQueries({ queryKey: ['tokens'] });
        },
    });

    const availableScopes = session.permissions.filter((x) => /^(assets|materials|jobs):/.test(x));

    const copySecret = () => {
        void navigator.clipboard.writeText(secret);
        setCopied(true);
        message.success('密钥已复制到剪贴板');
        setTimeout(() => setCopied(false), 2000);
    };

    const tokenList = query.data?.items || [];

    const columns = [
        {
            title: '凭据名称',
            dataIndex: 'name',
            key: 'name',
            render: (name: string, row: (typeof tokenList)[0]) => (
                <Space size={8}>
                    <KeyOutlined style={{ color: '#1668dc' }} />
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
            title: '授权范围 (Scopes)',
            dataIndex: 'scopes',
            key: 'scopes',
            render: (scopes: string[]) => (
                <Space size={[4, 4]} wrap>
                    {scopes?.map((s) => (
                        <Tag key={s} color="blue" style={{ fontSize: 11 }}>
                            {s}
                        </Tag>
                    ))}
                </Space>
            ),
        },
        {
            title: '过期时间',
            dataIndex: 'expires_at',
            key: 'expires_at',
            width: 180,
            render: (time: string) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                    {date(time)}
                </Text>
            ),
        },
        {
            title: '操作',
            key: 'actions',
            width: 100,
            align: 'right' as const,
            render: (_: unknown, row: (typeof tokenList)[0]) => (
                <Popconfirm
                    title="确定撤销此访问凭据？撤销后外部应用将无法再使用该密钥调用 API。"
                    onConfirm={() => revoke.mutate(row.id)}
                    okText="撤销"
                    cancelText="取消"
                >
                    <Button
                        size="small"
                        danger
                        type="text"
                        icon={<StopOutlined />}
                        loading={revoke.isPending}
                    >
                        撤销
                    </Button>
                </Popconfirm>
            ),
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Access Control"
                title="访问凭据 (API Tokens)"
                description="为 Agent 智能体、自动化脚本及外部客户端颁发可细粒度撤销的访问令牌。"
                action={
                    <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        disabled={!availableScopes.length}
                        onClick={() => {
                            save.reset();
                            form.resetFields();
                            setOpen(true);
                        }}
                    >
                        创建访问凭据
                    </Button>
                }
            />

            <ErrorNotice error={query.error || revoke.error} />

            {secret ? (
                <Alert
                    type="warning"
                    showIcon
                    message="请立即保存凭据密钥，它仅在此刻展示一次！"
                    description={
                        <div style={{ marginTop: 8 }}>
                            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                                <code
                                    className="mono"
                                    style={{
                                        flex: 1,
                                        padding: '8px 12px',
                                        fontSize: 13,
                                        background: '#ffffff',
                                    }}
                                >
                                    {secret}
                                </code>
                                <Button
                                    type="primary"
                                    icon={copied ? <CheckOutlined /> : <CopyOutlined />}
                                    onClick={copySecret}
                                >
                                    {copied ? '已复制' : '复制密钥'}
                                </Button>
                            </div>
                        </div>
                    }
                    style={{ marginBottom: 10 }}
                />
            ) : null}

            <Card bodyStyle={{ padding: 0 }}>
                {query.isPending ? (
                    <Loading tip="正在载入凭据列表…" />
                ) : tokenList.length ? (
                    <Table
                        columns={columns}
                        dataSource={tokenList}
                        rowKey="id"
                        pagination={{ pageSize: 15 }}
                    />
                ) : (
                    <Empty title="暂无生效的访问凭据">
                        点击右上角【创建访问凭据】为外部程序或自动化任务生成 API Token。
                    </Empty>
                )}
            </Card>

            {open ? (
                <Modal title="创建新访问凭据" onClose={() => setOpen(false)} width={520}>
                    <Form form={form} layout="vertical" onFinish={(values) => save.mutate(values)}>
                        <Form.Item
                            name="name"
                            label={<span style={{ fontWeight: 500 }}>凭据名称 / 客户端说明</span>}
                            rules={[{ required: true, message: '请输入凭据名称' }]}
                        >
                            <Input placeholder="例如：CI 任务调用、外部分析 Agent" maxLength={64} />
                        </Form.Item>

                        <Form.Item
                            name="days"
                            label={<span style={{ fontWeight: 500 }}>有效天数</span>}
                            initialValue={30}
                        >
                            <InputNumber min={1} max={365} style={{ width: '100%' }} />
                        </Form.Item>

                        <Form.Item
                            name="scopes"
                            label={<span style={{ fontWeight: 500 }}>授权权限范围 (Scopes)</span>}
                            rules={[{ required: true, message: '请至少勾选一项权限' }]}
                            initialValue={availableScopes.slice(0, 1)}
                        >
                            <Checkbox.Group
                                style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
                            >
                                {availableScopes.map((scope) => (
                                    <Checkbox key={scope} value={scope}>
                                        <span className="mono">{scope}</span>
                                    </Checkbox>
                                ))}
                            </Checkbox.Group>
                        </Form.Item>

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 24,
                            }}
                        >
                            <Button onClick={() => setOpen(false)}>取消</Button>
                            <Button type="primary" htmlType="submit" loading={save.isPending}>
                                确定生成
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}
        </div>
    );
}

/**
 * 用户与权限管理 (Users)
 */
export function Users() {
    const cache = useQueryClient();
    const canManage = usePermission('users:manage');
    const [open, setOpen] = useState(false);
    const [editing, setEditing] = useState<User | null>(null);
    const [addForm] = Form.useForm();
    const [editForm] = Form.useForm();

    const query = useQuery({
        queryKey: ['users'],
        queryFn: ({ signal }) => api<UserList>('/admin/v1/users', { signal }),
    });

    const save = useMutation({
        mutationFn: (values: {
            username: string;
            display_name: string;
            password?: string;
            roles: string[];
        }) =>
            post('/admin/v1/users', {
                username: values.username,
                display_name: values.display_name,
                password: values.password,
                roles: values.roles || ['viewer'],
            }),
        onSuccess: () => {
            message.success('用户创建成功');
            setOpen(false);
            addForm.resetFields();
            void cache.invalidateQueries({ queryKey: ['users'] });
        },
    });

    const update = useMutation({
        mutationFn: (values: {
            display_name: string;
            password?: string;
            roles: string[];
            disabled?: boolean;
        }) =>
            post(`/admin/v1/users/${editing!.username}`, {
                display_name: values.display_name,
                password: values.password || undefined,
                roles: values.roles || ['viewer'],
                disabled: !!values.disabled,
            }),
        onSuccess: () => {
            message.success('用户配置已更新');
            setEditing(null);
            editForm.resetFields();
            void cache.invalidateQueries({ queryKey: ['users'] });
        },
    });

    const userList = query.data?.items || [];

    const roleNameMap: Record<string, string> = {
        admin: '平台管理员',
        operator: '业务操作员',
        viewer: '素材查看者',
    };

    const columns = [
        {
            title: '用户',
            dataIndex: 'display_name',
            key: 'display_name',
            render: (name: string, row: User) => (
                <Space size={10} align="center">
                    <div
                        style={{
                            width: 34,
                            height: 34,
                            borderRadius: '50%',
                            background: '#eff6ff',
                            color: '#1668dc',
                            display: 'grid',
                            placeItems: 'center',
                            fontSize: 15,
                        }}
                    >
                        <UserOutlined />
                    </div>
                    <div>
                        <Text strong style={{ fontSize: 13 }}>
                            {name}
                        </Text>
                        <div style={{ fontSize: 12, color: '#64748b' }}>@{row.username}</div>
                    </div>
                </Space>
            ),
        },
        {
            title: '角色与权限组',
            dataIndex: 'roles',
            key: 'roles',
            render: (roles: string[]) => (
                <Space size={[4, 4]} wrap>
                    {roles.map((r) => (
                        <Tag
                            key={r}
                            color={
                                r === 'admin' ? 'magenta' : r === 'operator' ? 'blue' : 'default'
                            }
                        >
                            {roleNameMap[r] || r}
                        </Tag>
                    ))}
                </Space>
            ),
        },
        {
            title: '账号状态',
            dataIndex: 'disabled',
            key: 'disabled',
            width: 120,
            render: (disabled: boolean) => (
                <Tag color={disabled ? 'error' : 'success'}>{disabled ? '已停用' : '正常'}</Tag>
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
        {
            title: '操作',
            key: 'actions',
            width: 100,
            align: 'right' as const,
            render: (_: unknown, row: User) =>
                canManage ? (
                    <Button
                        size="small"
                        type="link"
                        icon={<EditOutlined />}
                        onClick={() => {
                            setEditing(row);
                            editForm.setFieldsValue({
                                display_name: row.display_name,
                                roles: row.roles,
                                disabled: row.disabled,
                            });
                        }}
                    >
                        编辑
                    </Button>
                ) : null,
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="User & Permissions"
                title="用户与权限管理"
                description="节点用户 RBAC 角色分配与认证安全，支持管理员、操作员与审计人员的多角色权限隔离。"
                action={
                    canManage ? (
                        <Button
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => {
                                save.reset();
                                addForm.resetFields();
                                setOpen(true);
                            }}
                        >
                            创建新用户
                        </Button>
                    ) : null
                }
            />

            <ErrorNotice error={query.error || save.error || update.error} />

            <Card bodyStyle={{ padding: 0 }}>
                {query.isPending ? (
                    <Loading tip="正在载入系统用户…" />
                ) : userList.length ? (
                    <Table
                        columns={columns}
                        dataSource={userList}
                        rowKey="username"
                        pagination={false}
                    />
                ) : null}
            </Card>

            {/* 新建用户 Modal */}
            {open ? (
                <Modal title="创建新系统用户" onClose={() => setOpen(false)} width={500}>
                    <Form
                        form={addForm}
                        layout="vertical"
                        onFinish={(values) => save.mutate(values)}
                    >
                        <Form.Item
                            name="display_name"
                            label={<span style={{ fontWeight: 500 }}>显示姓名</span>}
                            rules={[{ required: true, message: '请输入显示姓名' }]}
                        >
                            <Input placeholder="例如：张工" maxLength={120} />
                        </Form.Item>

                        <Form.Item
                            name="username"
                            label={<span style={{ fontWeight: 500 }}>账户登录名</span>}
                            rules={[
                                { required: true, message: '请输入用户名' },
                                {
                                    pattern: /^[a-zA-Z0-9_.-]{3,64}$/,
                                    message: '3–64 位字母、数字或 . _ -',
                                },
                            ]}
                        >
                            <Input placeholder="例如：zhang_san" />
                        </Form.Item>

                        <Form.Item
                            name="password"
                            label={<span style={{ fontWeight: 500 }}>初始登录密码</span>}
                            rules={[
                                { required: true, message: '请输入初始密码' },
                                { min: 12, message: '初始密码至少 12 位' },
                            ]}
                        >
                            <Input.Password placeholder="至少 12 位高强度密码" />
                        </Form.Item>

                        <Form.Item
                            name="roles"
                            label={<span style={{ fontWeight: 500 }}>分配系统角色</span>}
                            initialValue={['viewer']}
                        >
                            <Checkbox.Group
                                style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
                            >
                                <Checkbox value="viewer">素材查看者 (只读查看视频与素材)</Checkbox>
                                <Checkbox value="operator">
                                    业务操作员 (上传视频、下发处理任务)
                                </Checkbox>
                                <Checkbox value="admin">
                                    平台管理员 (纳管计算节点、插件与系统配置)
                                </Checkbox>
                            </Checkbox.Group>
                        </Form.Item>

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 20,
                            }}
                        >
                            <Button onClick={() => setOpen(false)}>取消</Button>
                            <Button type="primary" htmlType="submit" loading={save.isPending}>
                                创建用户
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}

            {/* 编辑用户 Modal */}
            {editing ? (
                <Modal
                    title={`编辑用户 · @${editing.username}`}
                    onClose={() => setEditing(null)}
                    width={500}
                >
                    <Form
                        form={editForm}
                        layout="vertical"
                        onFinish={(values) => update.mutate(values)}
                    >
                        <Form.Item
                            name="display_name"
                            label={<span style={{ fontWeight: 500 }}>显示姓名</span>}
                            rules={[{ required: true, message: '请输入显示姓名' }]}
                        >
                            <Input maxLength={120} />
                        </Form.Item>

                        <Form.Item
                            name="password"
                            label={<span style={{ fontWeight: 500 }}>重设密码 (可选)</span>}
                        >
                            <Input.Password placeholder="留空则保持当前登录密码不变" />
                        </Form.Item>

                        <Form.Item
                            name="roles"
                            label={<span style={{ fontWeight: 500 }}>角色与权限</span>}
                        >
                            <Checkbox.Group
                                style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
                            >
                                <Checkbox value="viewer">素材查看者</Checkbox>
                                <Checkbox value="operator">业务操作员</Checkbox>
                                <Checkbox value="admin">平台管理员</Checkbox>
                            </Checkbox.Group>
                        </Form.Item>

                        <Form.Item name="disabled" valuePropName="checked">
                            <Checkbox>
                                <span style={{ color: '#ef4444', fontWeight: 500 }}>
                                    停用此账户 (立即撤销所有会话与访问凭据)
                                </span>
                            </Checkbox>
                        </Form.Item>

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 20,
                            }}
                        >
                            <Button onClick={() => setEditing(null)}>取消</Button>
                            <Button type="primary" htmlType="submit" loading={update.isPending}>
                                保存修改
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}
        </div>
    );
}

/**
 * 操作审计 (Audit Trail)
 */
export function Audit() {
    const [offset, setOffset] = useState(0);

    const query = useQuery({
        queryKey: ['audit', offset],
        queryFn: ({ signal }) =>
            api<AuditList>(`/admin/v1/audit-events?limit=20&offset=${offset}`, { signal }),
    });

    const items = query.data?.items || [];
    const totalCount = query.data?.total || 0;

    const columns = [
        {
            title: '时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 180,
            render: (time: string) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                    {date(time)}
                </Text>
            ),
        },
        {
            title: '操作者',
            dataIndex: 'actor',
            key: 'actor',
            width: 160,
            render: (actor: string) => <Text strong>{actor}</Text>,
        },
        {
            title: '动作类型',
            dataIndex: 'action',
            key: 'action',
            width: 200,
            render: (action: string) => <Tag color="blue">{action}</Tag>,
        },
        {
            title: '操作目标 / 资源',
            dataIndex: 'target',
            key: 'target',
            render: (target: string) => <span className="mono">{target}</span>,
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Audit Trail"
                title="操作审计日志"
                description="全量留痕配置下发、身份授权与数据资产流转操作，严格脱敏保证零业务介质与密钥泄漏。"
            />

            <ErrorNotice error={query.error} />

            <Card bodyStyle={{ padding: 0 }}>
                {query.isPending ? (
                    <Loading tip="正在载入审计事件…" />
                ) : items.length ? (
                    <Table
                        columns={columns}
                        dataSource={items}
                        rowKey="id"
                        pagination={{
                            current: Math.floor(offset / 20) + 1,
                            pageSize: 20,
                            total: totalCount,
                            showTotal: (total) => `共 ${total} 条审计日志`,
                            onChange: (page) => setOffset((page - 1) * 20),
                        }}
                    />
                ) : !query.error ? (
                    <Empty title="暂无操作审计日志" />
                ) : null}
            </Card>
        </div>
    );
}

/**
 * 系统能力 (System Capabilities)
 */
export function System() {
    const query = useQuery({
        queryKey: ['capabilities'],
        queryFn: ({ signal }) => api<ConsoleStatus>('/v1/capabilities', { signal }),
        staleTime: 30000,
    });

    const names: Record<string, string> = {
        console_metadata: '工作台与配置存储',
        keyword_search: '素材关键词查询',
        file_storage: '原视频母带存储',
        media_admission: '媒体格式转码准入',
        task_execution: '分布式处理任务执行',
        plugin_installation: '插件热部署执行器',
        pipeline_publish: '处理方案版本发布',
        semantic_search: '多模态向量语义检索',
    };

    const caps = query.data?.capabilities || [];
    const availableCount = caps.filter((c) => c.available).length;

    return (
        <div>
            <Heading
                eyebrow="System Capabilities"
                title="系统能力与服务接线"
                description="各模块能力状态透明度报告，基础进程在线不代表业务闭环已达生产 Golden Path。"
            />

            <Row gutter={[8, 8]} style={{ marginBottom: 10 }}>
                <Col xs={24} sm={8}>
                    <StatSummary
                        title="契约 Schema 版本"
                        value={query.data?.schema_version || 'v0.1.0'}
                        prefix={<DashboardOutlined style={{ color: '#1668dc' }} />}
                    />
                </Col>
                <Col xs={24} sm={8}>
                    <StatSummary
                        title="已就绪核心接口"
                        value={availableCount}
                        prefix={<CheckCircleOutlined style={{ color: '#10b981' }} />}
                        color="#10b981"
                    />
                </Col>
                <Col xs={24} sm={8}>
                    <StatSummary
                        title="待接入链路能力"
                        value={caps.length - availableCount}
                        prefix={<ExclamationCircleOutlined style={{ color: '#f59e0b' }} />}
                        color="#f59e0b"
                    />
                </Col>
            </Row>

            <ErrorNotice error={query.error} />

            <Card
                title={
                    <Space size={8}>
                        <CheckCircleOutlined style={{ color: '#1668dc' }} />
                        <span style={{ fontWeight: 650, fontSize: 15 }}>端侧多模态能力矩阵</span>
                    </Space>
                }
                bodyStyle={{ padding: 10 }}
            >
                {query.isPending ? (
                    <Loading tip="正在检测系统能力接口…" />
                ) : caps.length ? (
                    <Row gutter={[8, 8]}>
                        {caps.map((c) => (
                            <Col xs={24} sm={12} md={8} key={c.name}>
                                <Card
                                    size="small"
                                    style={{
                                        borderRadius: 8,
                                        border: `1px solid ${c.available ? '#bbf7d0' : '#e2e8f0'}`,
                                        background: c.available ? '#f0fdf4' : '#f8fafc',
                                    }}
                                >
                                    <div
                                        style={{
                                            display: 'flex',
                                            justifyContent: 'space-between',
                                            alignItems: 'center',
                                            marginBottom: 6,
                                        }}
                                    >
                                        <Text strong style={{ fontSize: 13 }}>
                                            {names[c.name] || c.name}
                                        </Text>
                                        <Tag
                                            color={c.available ? 'success' : 'default'}
                                            style={{ margin: 0 }}
                                        >
                                            {c.available ? '接口可用' : '待接入'}
                                        </Tag>
                                    </div>
                                    <div
                                        style={{
                                            fontSize: 12,
                                            color: c.available ? '#15803d' : '#64748b',
                                        }}
                                    >
                                        {c.available
                                            ? '已实现并接通服务 API'
                                            : c.reason || '待实现链路'}
                                    </div>
                                    <div style={{ marginTop: 8 }}>
                                        <span
                                            className="mono"
                                            style={{ fontSize: 10, color: '#94a3b8' }}
                                        >
                                            {c.name}
                                        </span>
                                    </div>
                                </Card>
                            </Col>
                        ))}
                    </Row>
                ) : null}
            </Card>
        </div>
    );
}

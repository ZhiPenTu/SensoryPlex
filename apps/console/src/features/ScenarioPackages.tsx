import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Button,
    Card,
    Col,
    Descriptions,
    Form,
    Input,
    Popconfirm,
    Row,
    Select,
    Space,
    Table,
    Tag,
    Tooltip,
    Typography,
    message,
} from 'antd';
import {
    ApartmentOutlined,
    CheckCircleOutlined,
    CopyOutlined,
    DownloadOutlined,
    EyeOutlined,
    FileTextOutlined,
    FolderOutlined,
    PlusOutlined,
    ThunderboltOutlined,
    UploadOutlined,
} from '@ant-design/icons';
import { api, post } from '../api/client';
import type { PipelineList } from '../api/contracts';
import { Badge, date, Empty, ErrorNotice, Heading, Loading, Modal, Notice } from '../components';

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

const DEFAULT_CONFIG_SCHEMA = JSON.stringify(
    {
        type: 'object',
        properties: {
            min_confidence: { type: 'number', default: 0.6 },
        },
    },
    null,
    2,
);

const DEFAULT_SCHEDULING_POLICY = JSON.stringify(
    {
        placement: 'data_plane_local',
        max_retries: 3,
        required_accelerators: [],
    },
    null,
    2,
);


export interface ScenarioPackage {
    package_id: string;
    name: string;
    description: string;
    version: string;
    pipeline_id: string;
    pipeline_revision: number;
    graph_digest: string;
    config_schema: Record<string, unknown>;
    rbac_scopes: string[];
    scheduling_policy: Record<string, unknown>;
    state: 'draft' | 'published' | 'active' | 'archived';
    created_by: string;
    created_at: string;
    updated_at: string;
    pipeline_definition?: Record<string, unknown>;
}

export interface ScenarioPackageList {
    items: ScenarioPackage[];
    total: number;
}

export interface ScenarioManifest {
    manifest_version: string;
    package_id: string;
    name: string;
    description: string;
    version: string;
    graph_digest: string;
    pipeline: {
        pipeline_id: string;
        revision: number;
        definition: Record<string, unknown>;
    };
    config_schema: Record<string, unknown>;
    rbac_scopes: string[];
    scheduling_policy: Record<string, unknown>;
    exported_at: string;
}

export default function ScenarioPackages() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [stateFilter, setStateFilter] = useState<string>('');
    const [createModalOpen, setCreateModalOpen] = useState(false);
    const [importModalOpen, setImportModalOpen] = useState(false);
    const [detailPackage, setDetailPackage] = useState<ScenarioPackage | null>(null);
    const [manifestModal, setManifestModal] = useState<ScenarioManifest | null>(null);
    const [importJsonText, setImportJsonText] = useState('');

    const [form] = Form.useForm();

    const listing = useQuery({
        queryKey: ['scenario-packages', offset, stateFilter],
        queryFn: ({ signal }) => {
            const params = new URLSearchParams({
                limit: '20',
                offset: String(offset),
            });
            if (stateFilter) params.append('state', stateFilter);
            return api<ScenarioPackageList>(`/v1/scenario-packages?${params.toString()}`, { signal });
        },
    });

    const pipelinesQuery = useQuery({
        queryKey: ['pipelines-for-packages'],
        queryFn: ({ signal }) => api<PipelineList>('/v1/pipelines?limit=100', { signal }),
    });

    const createMutation = useMutation({
        mutationFn: (body: unknown) => post<ScenarioPackage>('/admin/v1/scenario-packages', body),
        onSuccess: () => {
            message.success('场景产品包草稿已成功创建');
            setCreateModalOpen(false);
            form.resetFields();
            void cache.invalidateQueries({ queryKey: ['scenario-packages'] });
        },
    });

    const publishMutation = useMutation({
        mutationFn: (packageId: string) => post<ScenarioPackage>(`/admin/v1/scenario-packages/${encodeURIComponent(packageId)}:publish`),
        onSuccess: () => {
            message.success('场景产品包已正式发布并锁定版本');
            void cache.invalidateQueries({ queryKey: ['scenario-packages'] });
        },
    });

    const activateMutation = useMutation({
        mutationFn: (packageId: string) => post<ScenarioPackage>(`/admin/v1/scenario-packages/${encodeURIComponent(packageId)}:activate`),
        onSuccess: () => {
            message.success('场景产品包已激活为工作区方案');
            void cache.invalidateQueries({ queryKey: ['scenario-packages'] });
        },
    });

    const archiveMutation = useMutation({
        mutationFn: (packageId: string) => post<ScenarioPackage>(`/admin/v1/scenario-packages/${encodeURIComponent(packageId)}:archive`),
        onSuccess: () => {
            message.success('场景产品包已归档');
            void cache.invalidateQueries({ queryKey: ['scenario-packages'] });
        },
    });

    const importMutation = useMutation({
        mutationFn: (body: unknown) => post<ScenarioPackage>('/admin/v1/scenario-packages:import', body),
        onSuccess: () => {
            message.success('Manifest 已成功导入并创建产品包');
            setImportModalOpen(false);
            setImportJsonText('');
            void cache.invalidateQueries({ queryKey: ['scenario-packages'] });
        },
    });

    const exportManifest = async (packageId: string) => {
        try {
            const manifest = await api<ScenarioManifest>(`/v1/scenario-packages/${encodeURIComponent(packageId)}/manifest`);
            setManifestModal(manifest);
        } catch (e: unknown) {
            const err = e as { message?: string };
            message.error(err.message || '导出 Manifest 失败');
        }
    };

    const handleCreateSubmit = async () => {
        try {
            const values = await form.validateFields();
            let configSchema = {};
            let schedulingPolicy = {};
            try {
                configSchema = values.config_schema ? JSON.parse(values.config_schema) : {};
            } catch {
                message.error('配置 Schema 必须为有效的 JSON 格式');
                return;
            }
            try {
                schedulingPolicy = values.scheduling_policy ? JSON.parse(values.scheduling_policy) : {};
            } catch {
                message.error('调度策略必须为有效的 JSON 格式');
                return;
            }

            const [pId, pRev] = values.pipeline_pair.split('::');
            createMutation.mutate({
                name: values.name,
                description: values.description || '',
                version: values.version || '1.0.0',
                pipeline_id: pId,
                pipeline_revision: Number(pRev),
                config_schema: configSchema,
                rbac_scopes: values.rbac_scopes || ['jobs:read', 'jobs:write', 'materials:read'],
                scheduling_policy: schedulingPolicy,
            });
        } catch {
            // Form validation error
        }
    };

    const handleImportSubmit = () => {
        try {
            const parsed = JSON.parse(importJsonText);
            importMutation.mutate(parsed);
        } catch {
            message.error('导入内容不是有效的 JSON');
        }
    };

    const copyManifestText = () => {
        if (!manifestModal) return;
        void navigator.clipboard.writeText(JSON.stringify(manifestModal, null, 2));
        message.success('Manifest 已复制到剪贴板');
    };

    const downloadManifestJson = () => {
        if (!manifestModal) return;
        const blob = new Blob([JSON.stringify(manifestModal, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `scenario-package-${manifestModal.package_id}-v${manifestModal.version}.json`;
        a.click();
        URL.revokeObjectURL(url);
    };

    const items = listing.data?.items || [];
    const totalCount = listing.data?.total || 0;

    const columns = [
        {
            title: '产品包名称',
            dataIndex: 'name',
            key: 'name',
            render: (name: string, row: ScenarioPackage) => (
                <Space size={10} align="start">
                    <div
                        style={{
                            width: 34,
                            height: 34,
                            borderRadius: 6,
                            background: '#f0fdf4',
                            color: '#15803d',
                            display: 'grid',
                            placeItems: 'center',
                            fontSize: 16,
                            flexShrink: 0,
                        }}
                    >
                        <ApartmentOutlined />
                    </div>
                    <div>
                        <Button
                            type="link"
                            style={{
                                padding: 0,
                                height: 'auto',
                                fontSize: 13,
                                fontWeight: 600,
                                color: '#0f172a',
                                textAlign: 'left',
                            }}
                            onClick={() => setDetailPackage(row)}
                        >
                            {name}
                        </Button>
                        <div style={{ fontSize: 12, color: '#64748b' }}>
                            {row.description || `ID: ${row.package_id}`}
                        </div>
                    </div>
                </Space>
            ),
        },
        {
            title: '方案版本',
            dataIndex: 'version',
            key: 'version',
            width: 100,
            render: (ver: string) => <Tag color="purple">v{ver}</Tag>,
        },
        {
            title: '绑定编排',
            key: 'pipeline',
            width: 170,
            render: (_: unknown, row: ScenarioPackage) => (
                <div>
                    <Text strong style={{ fontSize: 12 }}>{row.pipeline_id}</Text>
                    <div>
                        <Tag color="blue" style={{ fontSize: 10, marginTop: 2 }}>图 v{row.pipeline_revision}</Tag>
                    </div>
                </div>
            ),
        },
        {
            title: '不可变图摘要',
            dataIndex: 'graph_digest',
            key: 'graph_digest',
            width: 160,
            render: (digest: string) => (
                <Tooltip title={digest}>
                    <span className="mono" style={{ fontSize: 11, color: '#475569' }}>
                        {digest.slice(0, 15)}…
                    </span>
                </Tooltip>
            ),
        },
        {
            title: '权限要求',
            dataIndex: 'rbac_scopes',
            key: 'rbac_scopes',
            width: 160,
            render: (scopes: string[]) => (
                <Space size={[2, 2]} wrap>
                    {(scopes || []).map((s) => (
                        <Tag key={s} style={{ fontSize: 10 }}>{s}</Tag>
                    ))}
                </Space>
            ),
        },
        {
            title: '生命周期',
            dataIndex: 'state',
            key: 'state',
            width: 110,
            render: (state: string) => <Badge state={state} />,
        },
        {
            title: '创建者 / 时间',
            key: 'created',
            width: 150,
            render: (_: unknown, row: ScenarioPackage) => (
                <div>
                    <div style={{ fontSize: 12, fontWeight: 500 }}>{row.created_by}</div>
                    <Text type="secondary" style={{ fontSize: 11 }}>{date(row.created_at)}</Text>
                </div>
            ),
        },
        {
            title: '操作',
            key: 'actions',
            width: 200,
            align: 'right' as const,
            render: (_: unknown, row: ScenarioPackage) => (
                <Space size={6}>
                    <Button
                        size="small"
                        type="link"
                        icon={<EyeOutlined />}
                        onClick={() => setDetailPackage(row)}
                        style={{ padding: 0 }}
                    >
                        详情
                    </Button>
                    <Button
                        size="small"
                        type="link"
                        icon={<DownloadOutlined />}
                        onClick={() => void exportManifest(row.package_id)}
                        style={{ padding: 0 }}
                    >
                        导出
                    </Button>
                    {row.state === 'draft' ? (
                        <Popconfirm
                            title="确定发布此产品包？发布后配置与 DAG 不可原地篡改。"
                            onConfirm={() => publishMutation.mutate(row.package_id)}
                            okText="发布"
                            cancelText="取消"
                        >
                            <Button
                                size="small"
                                type="link"
                                icon={<CheckCircleOutlined />}
                                loading={publishMutation.isPending}
                                style={{ padding: 0 }}
                            >
                                发布
                            </Button>
                        </Popconfirm>
                    ) : null}
                    {row.state === 'published' ? (
                        <Popconfirm
                            title="确定将此产品包设为活跃状态？"
                            onConfirm={() => activateMutation.mutate(row.package_id)}
                            okText="激活"
                            cancelText="取消"
                        >
                            <Button
                                size="small"
                                type="link"
                                icon={<ThunderboltOutlined />}
                                loading={activateMutation.isPending}
                                style={{ padding: 0, color: '#10b981' }}
                            >
                                激活
                            </Button>
                        </Popconfirm>
                    ) : null}
                    {row.state !== 'archived' ? (
                        <Popconfirm
                            title="确定归档此方案产品包？"
                            onConfirm={() => archiveMutation.mutate(row.package_id)}
                            okText="归档"
                            cancelText="取消"
                        >
                            <Button
                                size="small"
                                danger
                                type="text"
                                icon={<FolderOutlined />}
                                loading={archiveMutation.isPending}
                                style={{ padding: 0 }}
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
                eyebrow="ADR-029 P3 Scenario Packages"
                title="场景化产品包"
                description="将不可变 Pipeline revision、配置 JSON Schema、RBAC 授权要求与节点调度策略封装为可交付产品包实体。"
                action={
                    <Space size={8}>
                        <Button
                            icon={<UploadOutlined />}
                            onClick={() => {
                                setImportJsonText('');
                                setImportModalOpen(true);
                            }}
                        >
                            导入 Manifest
                        </Button>
                        <Button
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => {
                                form.resetFields();
                                setCreateModalOpen(true);
                            }}
                        >
                            新建产品包
                        </Button>
                    </Space>
                }
            />

            <Notice>
                产品包在发布（Published）后锁定图摘要与不可变版本，严格保证生产环境交付一致性。
                支持一键导出包含拓扑与配置定义的便携式 JSON Manifest，实现跨环境快速交付。
            </Notice>

            <ErrorNotice
                error={
                    listing.error ||
                    createMutation.error ||
                    publishMutation.error ||
                    activateMutation.error ||
                    archiveMutation.error ||
                    importMutation.error
                }
            />

            {/* 状态过滤选择器 */}
            <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <Space size={8}>
                    <Text type="secondary" style={{ fontSize: 13 }}>状态筛选:</Text>
                    <Select
                        size="small"
                        value={stateFilter}
                        onChange={(v) => {
                            setStateFilter(v);
                            setOffset(0);
                        }}
                        style={{ width: 120 }}
                        options={[
                            { value: '', label: '全部状态' },
                            { value: 'draft', label: '草稿 (Draft)' },
                            { value: 'published', label: '已发布 (Published)' },
                            { value: 'active', label: '已激活 (Active)' },
                            { value: 'archived', label: '已归档 (Archived)' },
                        ]}
                    />
                </Space>
            </div>

            <Card bodyStyle={{ padding: 0 }}>
                {listing.isPending ? (
                    <Loading tip="正在载入场景产品包列表…" />
                ) : items.length ? (
                    <Table
                        columns={columns}
                        dataSource={items}
                        rowKey="package_id"
                        pagination={{
                            current: Math.floor(offset / 20) + 1,
                            pageSize: 20,
                            total: totalCount,
                            showTotal: (total) => `共 ${total} 个产品包`,
                            onChange: (page) => setOffset((page - 1) * 20),
                        }}
                    />
                ) : !listing.error ? (
                    <Empty title="暂无场景产品包记录">
                        <Button type="primary" onClick={() => setCreateModalOpen(true)}>
                            新建首个产品包
                        </Button>
                    </Empty>
                ) : null}
            </Card>

            {/* 新建产品包 Modal */}
            {createModalOpen ? (
                <Modal
                    title="新建场景化产品包"
                    onClose={() => setCreateModalOpen(false)}
                    width={680}
                >
                    <Form
                        form={form}
                        layout="vertical"
                        initialValues={{
                            version: '1.0.0',
                            rbac_scopes: ['jobs:read', 'jobs:write', 'materials:read'],
                            config_schema: DEFAULT_CONFIG_SCHEMA,
                            scheduling_policy: DEFAULT_SCHEDULING_POLICY,
                        }}
                        onFinish={() => void handleCreateSubmit()}
                    >
                        <Row gutter={12}>
                            <Col span={16}>
                                <Form.Item
                                    name="name"
                                    label="产品包名称"
                                    rules={[{ required: true, message: '请输入产品包名称' }]}
                                >
                                    <Input placeholder="例如：全模态视频智能审计包 (Full Spectrum Audit)" />
                                </Form.Item>
                            </Col>
                            <Col span={8}>
                                <Form.Item
                                    name="version"
                                    label="方案版本"
                                    rules={[{ required: true, message: '请输入版本号' }]}
                                >
                                    <Input placeholder="1.0.0" />
                                </Form.Item>
                            </Col>
                        </Row>

                        <Form.Item name="description" label="产品包描述">
                            <Input placeholder="简要描述本产品包的应用业务场景与能力目标" />
                        </Form.Item>

                        <Form.Item
                            name="pipeline_pair"
                            label="绑定底层编排 (Pipeline & Revision)"
                            rules={[{ required: true, message: '请选择绑定的底层 Pipeline 编排' }]}
                        >
                            <Select
                                placeholder="选择已发布且图摘要固化的 Pipeline"
                                options={(pipelinesQuery.data?.items || []).map((p) => ({
                                    label: `${p.name} (ID: ${p.id} · v${p.revision} · 状态: ${p.state})`,
                                    value: `${p.id}::${p.revision}`,
                                }))}
                            />
                        </Form.Item>

                        <Form.Item name="rbac_scopes" label="授权角色与 Scope 约束">
                            <Select
                                mode="tags"
                                placeholder="添加 RBAC Scope 声明"
                                options={[
                                    { value: 'jobs:read', label: 'jobs:read' },
                                    { value: 'jobs:write', label: 'jobs:write' },
                                    { value: 'materials:read', label: 'materials:read' },
                                    { value: 'pipelines:manage', label: 'pipelines:manage' },
                                ]}
                            />
                        </Form.Item>

                        <Form.Item
                            name="config_schema"
                            label="业务配置 JSON Schema"
                            tooltip="声明场景参数的校验规则，供任务派发时进行参数校验"
                        >
                            <TextArea rows={4} className="mono" style={{ fontSize: 12 }} />
                        </Form.Item>

                        <Form.Item
                            name="scheduling_policy"
                            label="节点调度与资源策略"
                            tooltip="指定算力节点数据本地性、硬件加速器要求与重试退避上限"
                        >
                            <TextArea rows={3} className="mono" style={{ fontSize: 12 }} />
                        </Form.Item>

                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
                            <Button onClick={() => setCreateModalOpen(false)}>取消</Button>
                            <Button type="primary" htmlType="submit" loading={createMutation.isPending}>
                                创建草稿
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}

            {/* 导入 Manifest Modal */}
            {importModalOpen ? (
                <Modal
                    title="导入场景产品包 Manifest"
                    onClose={() => setImportModalOpen(false)}
                    width={640}
                >
                    <Paragraph type="secondary" style={{ fontSize: 13 }}>
                        粘贴或载入导出的场景产品包 JSON Manifest。导入后将自动核对不可变图摘要并在本地注册产品包。
                    </Paragraph>

                    <TextArea
                        rows={12}
                        className="mono"
                        style={{ fontSize: 12, marginBottom: 14 }}
                        placeholder="在此粘贴完整 Manifest JSON 内容…"
                        value={importJsonText}
                        onChange={(e) => setImportJsonText(e.target.value)}
                    />

                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <Button
                            icon={<FileTextOutlined />}
                            onClick={() => {
                                const input = document.createElement('input');
                                input.type = 'file';
                                input.accept = '.json';
                                input.onchange = (e) => {
                                    const file = (e.target as HTMLInputElement).files?.[0];
                                    if (!file) return;
                                    const reader = new FileReader();
                                    reader.onload = (ev) => {
                                        setImportJsonText(String(ev.target?.result || ''));
                                    };
                                    reader.readAsText(file);
                                };
                                input.click();
                            }}
                        >
                            从本地文件读取
                        </Button>
                        <Space>
                            <Button onClick={() => setImportModalOpen(false)}>取消</Button>
                            <Button
                                type="primary"
                                loading={importMutation.isPending}
                                disabled={!importJsonText.trim()}
                                onClick={handleImportSubmit}
                            >
                                导入并校验
                            </Button>
                        </Space>
                    </div>
                </Modal>
            ) : null}

            {/* 详情 Modal */}
            {detailPackage ? (
                <Modal
                    title={`场景产品包详情 · ${detailPackage.name}`}
                    onClose={() => setDetailPackage(null)}
                    width={720}
                >
                    <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
                        <Descriptions.Item label="产品包 ID">{detailPackage.package_id}</Descriptions.Item>
                        <Descriptions.Item label="方案版本">v{detailPackage.version}</Descriptions.Item>
                        <Descriptions.Item label="绑定 Pipeline">{detailPackage.pipeline_id}</Descriptions.Item>
                        <Descriptions.Item label="编排 Revision">v{detailPackage.pipeline_revision}</Descriptions.Item>
                        <Descriptions.Item label="状态">
                            <Badge state={detailPackage.state} />
                        </Descriptions.Item>
                        <Descriptions.Item label="创建者">{detailPackage.created_by}</Descriptions.Item>
                        <Descriptions.Item label="图摘要" span={2}>
                            <span className="mono" style={{ fontSize: 11, wordBreak: 'break-all' }}>
                                {detailPackage.graph_digest}
                            </span>
                        </Descriptions.Item>
                    </Descriptions>

                    <Card size="small" title="业务配置 JSON Schema" style={{ marginBottom: 12 }}>
                        <pre className="mono" style={{ fontSize: 11, margin: 0, maxHeight: 160, overflow: 'auto' }}>
                            {JSON.stringify(detailPackage.config_schema, null, 2)}
                        </pre>
                    </Card>

                    <Card size="small" title="调度策略与拓扑要求" style={{ marginBottom: 12 }}>
                        <pre className="mono" style={{ fontSize: 11, margin: 0, maxHeight: 140, overflow: 'auto' }}>
                            {JSON.stringify(detailPackage.scheduling_policy, null, 2)}
                        </pre>
                    </Card>

                    <Card size="small" title="RBAC 访问控制 Scopes">
                        <Space size={[4, 4]} wrap>
                            {(detailPackage.rbac_scopes || []).map((s) => (
                                <Tag key={s} color="blue">{s}</Tag>
                            ))}
                        </Space>
                    </Card>

                    <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
                        <Button onClick={() => setDetailPackage(null)}>关闭</Button>
                    </div>
                </Modal>
            ) : null}

            {/* 导出 Manifest Modal */}
            {manifestModal ? (
                <Modal
                    title={`产品包便携式 Manifest · ${manifestModal.name} (v${manifestModal.version})`}
                    onClose={() => setManifestModal(null)}
                    width={720}
                >
                    <Notice>
                        本 Manifest 包含底层编排 DAG 的完整不可变拓扑定义、图摘要、配置 Schema 与调度策略，可直接导入至其他 SensoryPlex 集群。
                    </Notice>

                    <pre
                        className="mono"
                        style={{
                            fontSize: 11,
                            background: '#0f172a',
                            color: '#38bdf8',
                            padding: 12,
                            borderRadius: 6,
                            maxHeight: 380,
                            overflow: 'auto',
                        }}
                    >
                        {JSON.stringify(manifestModal, null, 2)}
                    </pre>

                    <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 14 }}>
                        <Space>
                            <Button icon={<CopyOutlined />} onClick={copyManifestText}>
                                复制 JSON
                            </Button>
                            <Button icon={<DownloadOutlined />} type="primary" onClick={downloadManifestJson}>
                                下载 .json 文件
                            </Button>
                        </Space>
                        <Button onClick={() => setManifestModal(null)}>关闭</Button>
                    </div>
                </Modal>
            ) : null}
        </div>
    );
}
